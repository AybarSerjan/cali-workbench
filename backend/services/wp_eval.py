from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.tools.datasets import MDFAdapter
from agent.tools.expression import align_series, evaluate_expression
from agent.tools.signals import find_mask_windows
from backend.config import ROOT
from backend.models import Dataset, DatasetFile, SignalMapping, Workpackage, WorkpackageRevision, WpEvaluation
from backend.services import a2l_index, llm
from backend.services.runs import create_run, update_run, write_agent_artifact
from backend.services.workpackages import get_workpackage, revision_payload


def _paths(dataset: Dataset) -> list[Path]:
    return [ROOT / item.relative_path for item in dataset.files if item.suffix.lower() in {".mf", ".mdf", ".mf4", ".dat"}]


def _base_channel(name: str) -> str:
    return re.sub(r"\\XCP(?::\d+)?$", "", name, flags=re.I).casefold()


def mapping_payload(item: SignalMapping) -> dict[str, Any]:
    return {"id": item.id, "a2l_name": item.a2l_name, "mf4_channel": item.mf4_channel,
            "match_layer": item.match_layer, "match_status": item.match_status, "status": item.status,
            "supersedes_id": item.supersedes_id, "evidence": json.loads(item.evidence_json),
            "created_by": item.created_by, "created_at": item.created_at}


def list_mappings(db: Session, match_status: str | None = None) -> list[dict[str, Any]]:
    query = select(SignalMapping).where(SignalMapping.status == "active")
    if match_status:
        query = query.where(SignalMapping.match_status == match_status)
    return [mapping_payload(item) for item in db.scalars(query.order_by(SignalMapping.created_at.desc())).all()]


def confirm_mapping(db: Session, mapping_id: str) -> SignalMapping:
    item = db.get(SignalMapping, mapping_id)
    if not item or item.status != "active":
        raise ValueError("active signal mapping not found")
    item.match_status = "confirmed"
    db.commit(); db.refresh(item)
    return item


def create_manual_mapping(db: Session, a2l_name: str, mf4_channel: str, evidence: dict[str, Any]) -> SignalMapping:
    previous = db.scalars(select(SignalMapping).where(
        SignalMapping.a2l_name == a2l_name, SignalMapping.status == "active"
    )).all()
    for item in previous: item.status = "superseded"
    newest = SignalMapping(a2l_name=a2l_name, mf4_channel=mf4_channel, match_layer="manual",
                           match_status="confirmed", status="active",
                           supersedes_id=previous[0].id if previous else None,
                           evidence_json=json.dumps(evidence, ensure_ascii=False), created_by="user")
    db.add(newest); db.commit(); db.refresh(newest)
    return newest


def _definition_revision(db: Session, workpackage_id: str) -> WorkpackageRevision:
    wp = get_workpackage(db, workpackage_id)
    if wp.approved_revision_id:
        revision = db.get(WorkpackageRevision, wp.approved_revision_id)
        if revision: return revision
    revision = db.scalar(select(WorkpackageRevision).where(
        WorkpackageRevision.workpackage_id == wp.id).order_by(WorkpackageRevision.number.desc()).limit(1))
    if not revision: raise ValueError("workpackage has no revision")
    return revision


def match_signals(db: Session, workpackage_id: str, dataset_id: str, a2l_sha256: str = "") -> dict[str, Any]:
    dataset = db.get(Dataset, dataset_id)
    if not dataset or dataset.kind != "mdf": raise ValueError("MDF dataset not found")
    channels = [item["name"] for item in MDFAdapter().list_channels(_paths(dataset))]
    by_base: dict[str, str] = {}
    for name in channels: by_base.setdefault(_base_channel(name), name)
    revision = _definition_revision(db, workpackage_id)
    definition = json.loads(revision.definition_json)
    results: list[dict[str, Any]] = []
    for signal in definition.get("signals", []):
        name = str(signal.get("a2l_name") or "")
        channel = by_base.get(name.casefold())
        if channel:
            results.append({"a2l_name": name, "mf4_channel": channel, "layer": "L1", "status": "confirmed"}); continue
        reused = db.scalar(select(SignalMapping).where(
            SignalMapping.a2l_name == name, SignalMapping.status == "active",
            SignalMapping.match_status == "confirmed").order_by(SignalMapping.created_at.desc()).limit(1))
        if reused and _base_channel(reused.mf4_channel) in by_base:
            results.append({"a2l_name": name, "mf4_channel": by_base[_base_channel(reused.mf4_channel)],
                            "layer": "reuse", "status": "confirmed", "mapping_id": reused.id}); continue
        variant = None
        if a2l_sha256:
            variant = next((item for item in a2l_index.variant_family(a2l_sha256, name)
                            if item["name"].casefold() in by_base), None)
        if variant:
            channel = by_base[variant["name"].casefold()]
            existing = db.scalar(select(SignalMapping).where(
                SignalMapping.a2l_name == name, SignalMapping.mf4_channel == channel,
                SignalMapping.status == "active").limit(1))
            if not existing:
                existing = SignalMapping(a2l_name=name, mf4_channel=channel, match_layer="L2",
                    match_status="proposed", status="active", evidence_json=json.dumps(variant, ensure_ascii=False), created_by="system")
                db.add(existing); db.flush()
            results.append({"a2l_name": name, "mf4_channel": channel, "layer": "L2",
                            "status": existing.match_status, "mapping_id": existing.id}); continue
        semantic = None
        if a2l_sha256 and llm.public_config()["provider"] != "disabled":
            candidates = [channel for channel in channels if a2l_index.get_measurement(a2l_sha256, re.sub(r"\\XCP(?::\d+)?$", "", channel, flags=re.I))]
            if candidates:
                desired = a2l_index.get_measurement(a2l_sha256, name) or {"name": name}
                answer = llm.chat_sync([
                    {"role": "system", "content": "Choose the single best MF4 channel for the requested A2L signal. Reply with one exact candidate string or NONE. Do not invent names."},
                    {"role": "user", "content": json.dumps({"requested": desired, "candidates": candidates[:200]}, ensure_ascii=False)},
                ])
                if answer and answer.strip() in candidates:
                    semantic = answer.strip()
        if semantic:
            proposed = SignalMapping(a2l_name=name, mf4_channel=semantic, match_layer="L3", match_status="proposed",
                status="active", evidence_json=json.dumps({"method": "bounded_llm", "candidates": len(candidates)}), created_by="agent")
            db.add(proposed); db.flush()
            results.append({"a2l_name": name, "mf4_channel": semantic, "layer": "L3", "status": "proposed", "mapping_id": proposed.id})
        else:
            reason = "main LLM disabled" if llm.public_config()["provider"] == "disabled" else "no bounded semantic candidate"
            results.append({"a2l_name": name, "mf4_channel": None, "layer": "L3", "status": "NOT_FOUND", "reason": reason})
    db.commit()
    return {"workpackage_id": workpackage_id, "revision_id": revision.id, "dataset_id": dataset_id,
            "a2l_sha256": a2l_sha256, "matches": results}


def _series(adapter: MDFAdapter, paths: list[Path], matches: list[dict[str, Any]]) -> dict[str, tuple[Any, Any]]:
    output = {}
    for item in matches:
        if not item.get("mf4_channel") or item.get("status") != "confirmed": continue
        data = adapter.read_channel(paths, item["mf4_channel"])
        segment = data["segments"][0]
        output[item["a2l_name"]] = (segment["timestamps"], segment["samples"])
    return output


def evaluate(db: Session, workpackage_id: str, dataset_id: str, a2l_file_id: str | None = None, hex_file_id: str | None = None) -> WpEvaluation:
    dataset = db.get(Dataset, dataset_id)
    if not dataset or dataset.kind != "mdf": raise ValueError("MDF dataset not found")
    a2l_sha = ""
    if a2l_file_id:
        a2l_file = db.get(DatasetFile, a2l_file_id)
        if not a2l_file or a2l_file.suffix.lower() != ".a2l": raise ValueError("A2L dataset file not found")
        a2l_sha = a2l_index.build_index(db, a2l_file_id)["sha256"]
    revision = _definition_revision(db, workpackage_id)
    definition = json.loads(revision.definition_json)
    match = match_signals(db, workpackage_id, dataset_id, a2l_sha)
    missing = [item["a2l_name"] for item in match["matches"]
               if not item.get("mf4_channel") or item.get("status") != "confirmed"]
    paths, adapter = _paths(dataset), MDFAdapter()
    criteria_results: list[dict[str, Any]] = []
    overall = "OBSERVATION" if revision.status != "approved" else "PASS"
    if missing:
        overall = "NOT_EVALUABLE"
    else:
        timestamps, values = align_series(_series(adapter, paths, match["matches"]))
        gate = np.ones(len(timestamps), dtype=bool)
        if definition.get("scenario_gate"):
            gate = np.asarray(evaluate_expression(definition["scenario_gate"], values), dtype=bool)
        if not np.any(gate): overall = "NOT_APPLICABLE_IN_FILE"
        else:
            for index, criterion in enumerate(definition.get("criteria", []), 1):
                violation = np.asarray(evaluate_expression(criterion["expression"], values), dtype=bool) & gate
                if criterion.get("condition"):
                    violation &= np.asarray(evaluate_expression(criterion["condition"], values), dtype=bool)
                windows = find_mask_windows(timestamps, violation, float(criterion.get("min_duration_s", 0)),
                                            float(criterion.get("merge_gap_s", 0)))
                verdict = "OBSERVATION" if revision.status != "approved" else ("FAIL" if windows else "PASS")
                if verdict == "FAIL": overall = "FAIL"
                snapshots = []
                for window in windows:
                    snapshots.append({"window": window, "signals": {
                        item["a2l_name"]: adapter.read_window(paths, item["mf4_channel"], window["start"], window["end"])
                        for item in match["matches"] if item.get("mf4_channel")
                    }})
                criteria_results.append({"id": criterion.get("id", f"criterion-{index}"), "expression": criterion["expression"],
                                         "verdict": verdict, "windows": windows, "snapshots": snapshots})
    hex_sha = ""
    if hex_file_id:
        hex_file = db.get(DatasetFile, hex_file_id)
        if not hex_file or hex_file.suffix.lower() != ".hex": raise ValueError("HEX dataset file not found")
        hex_sha = hex_file.sha256
    summary = {"overall": overall, "revision_status": revision.status, "missing_signals": missing,
               "criteria": criteria_results, "signal_match": match}
    if llm.public_config()["provider"] == "disabled":
        summary["explanation"] = {"status": "unavailable", "reason": "main LLM disabled", "text": ""}
    else:
        factual = {"overall": overall, "missing_signals": missing,
                   "criteria": [{"id": item["id"], "verdict": item["verdict"], "windows": item["windows"]} for item in criteria_results]}
        try:
            text = llm.chat_sync([
                {"role": "system", "content": "Summarize these deterministic WP evaluation facts concisely. Do not infer causes, calibration quality, or recommendations not present in the JSON."},
                {"role": "user", "content": json.dumps(factual, ensure_ascii=False)},
            ]) or ""
            summary["explanation"] = {"status": "available" if text else "unavailable", "reason": "" if text else "empty LLM response", "text": text}
        except Exception as exc:
            summary["explanation"] = {"status": "unavailable", "reason": str(exc), "text": ""}
    input_digest = hashlib.sha256(json.dumps({"revision": revision.content_sha256, "dataset": dataset_id,
        "a2l": a2l_sha, "hex": hex_sha}, sort_keys=True).encode()).hexdigest()
    run = create_run(db, "wp_eval", input_digest)
    artifact1 = write_agent_artifact(run.id, "wp-eval", "wp_eval.json", summary)
    artifact2 = write_agent_artifact(run.id, "wp-eval", "signal_match.json", match)
    row = WpEvaluation(run_id=run.id, workpackage_id=workpackage_id, revision_id=revision.id,
                       dataset_id=dataset_id, a2l_sha256=a2l_sha, hex_sha256=hex_sha,
                       summary_json=json.dumps(summary, ensure_ascii=False))
    db.add(row); db.commit(); db.refresh(row)
    update_run(db, run, status="completed", node="wp_eval", artifacts=[artifact1["path"], artifact2["path"]])
    return row


def evaluate_all(db: Session, dataset_id: str, a2l_file_id: str | None = None, hex_file_id: str | None = None) -> dict[str, Any]:
    """Run every approved workpackage against one MF4 dataset; per-WP failures
    become result rows instead of aborting the batch."""
    results: list[dict[str, Any]] = []
    for wp in db.scalars(select(Workpackage).order_by(Workpackage.wp_code)).all():
        base = {"workpackage_id": wp.id, "wp_code": wp.wp_code, "title": wp.title}
        if not wp.approved_revision_id:
            results.append({**base, "overall": "NOT_APPROVED"}); continue
        try:
            row = evaluate(db, wp.id, dataset_id, a2l_file_id, hex_file_id)
            summary = json.loads(row.summary_json)
            results.append({**base, "evaluation_id": row.id, "overall": summary["overall"],
                            "missing_signals": summary["missing_signals"],
                            "criteria": [{"id": item["id"], "verdict": item["verdict"],
                                          "windows": len(item["windows"])} for item in summary["criteria"]]})
        except Exception as exc:
            db.rollback()
            results.append({**base, "overall": "ERROR", "error": str(exc)})
    counts: dict[str, int] = {}
    for item in results:
        counts[item["overall"]] = counts.get(item["overall"], 0) + 1
    output = {"dataset_id": dataset_id, "a2l_file_id": a2l_file_id, "counts": counts, "results": results}
    if llm.public_config()["provider"] == "disabled":
        output["explanation"] = {"status": "unavailable", "reason": "main LLM disabled", "text": ""}
    else:
        facts = {"counts": counts, "workpackages": [{"wp_code": item["wp_code"], "overall": item["overall"],
                 "missing_signals": item.get("missing_signals", [])} for item in results]}
        try:
            text = llm.chat_sync([
                {"role": "system", "content": "Summarize the completion status of these workpackage evaluations: how many passed, failed, were not covered by this measurement, or could not be evaluated, and what is still missing. State facts only; do not infer causes or recommendations beyond the JSON."},
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
            ]) or ""
            output["explanation"] = {"status": "available" if text else "unavailable", "reason": "" if text else "empty LLM response", "text": text}
        except Exception as exc:
            output["explanation"] = {"status": "unavailable", "reason": str(exc), "text": ""}
    return output


def completion_overview(db: Session) -> list[dict[str, Any]]:
    """Latest evaluation per workpackage, for the completion table."""
    latest: dict[str, WpEvaluation] = {}
    for row in db.scalars(select(WpEvaluation).order_by(WpEvaluation.created_at.desc())).all():
        latest.setdefault(row.workpackage_id, row)
    output = []
    for wp in db.scalars(select(Workpackage).order_by(Workpackage.wp_code)).all():
        row = latest.get(wp.id)
        summary = json.loads(row.summary_json) if row else {}
        output.append({"workpackage_id": wp.id, "wp_code": wp.wp_code, "title": wp.title,
                       "approved": bool(wp.approved_revision_id),
                       "overall": summary.get("overall") if row else None,
                       "missing_signals": summary.get("missing_signals", []),
                       "evaluation_id": row.id if row else None,
                       "dataset_id": row.dataset_id if row else None,
                       "evaluated_at": row.created_at if row else None})
    return output


def evaluation_payload(item: WpEvaluation) -> dict[str, Any]:
    return {"id": item.id, "run_id": item.run_id, "workpackage_id": item.workpackage_id,
            "revision_id": item.revision_id, "dataset_id": item.dataset_id, "a2l_sha256": item.a2l_sha256,
            "hex_sha256": item.hex_sha256, "summary": json.loads(item.summary_json), "created_at": item.created_at}
