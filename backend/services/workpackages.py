from __future__ import annotations

import hashlib
import json
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import Workpackage, WorkpackageRevision, WpEvaluation


def _json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("LLM workpackage draft must be a JSON object")
    return value


def draft_with_llm(db: Session, wp_code: str, title: str, prompt: str) -> Workpackage:
    from agent.tools.datasets import CapabilityUnavailable
    from backend.services import llm
    if llm.public_config()["provider"] == "disabled":
        raise CapabilityUnavailable("main LLM is disabled; deterministic matching and evaluation remain available")
    answer = llm.chat_sync([
        {"role": "system", "content": "Create a calibration validation workpackage definition as JSON only. Keys: description, signals (role,a2l_name,match_status=proposed), scenario_gate, criteria (expression,min_duration_s,merge_gap_s,threshold_source=AGENT_PROPOSED), related_functions, calib_params. Never mark a signal confirmed or a threshold HUMAN_APPROVED."},
        {"role": "user", "content": prompt},
    ])
    if not answer:
        raise RuntimeError("LLM returned an empty workpackage draft")
    return create_workpackage(db, wp_code, title, _json_object(answer), "agent")


SCHEMA_VERSION = 1


def _canonical_definition(value: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    if not isinstance(value, dict):
        raise ValueError("workpackage definition must be an object")
    definition = json.loads(json.dumps(value, ensure_ascii=False))
    for field in ("signals", "criteria"):
        items = definition.setdefault(field, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError(f"definition.{field} must be a list of objects")
    text = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return definition, text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _notes(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def revision_payload(item: WorkpackageRevision) -> dict[str, Any]:
    return {
        "id": item.id,
        "workpackage_id": item.workpackage_id,
        "number": item.number,
        "status": item.status,
        "definition": json.loads(item.definition_json),
        "content_sha256": item.content_sha256,
        "review_notes": _notes(item.review_notes),
        "created_by": item.created_by,
        "created_at": item.created_at,
    }


def _revisions(db: Session, workpackage_id: str) -> list[WorkpackageRevision]:
    return list(db.scalars(
        select(WorkpackageRevision)
        .where(WorkpackageRevision.workpackage_id == workpackage_id)
        .order_by(WorkpackageRevision.number.desc())
    ).all())


def workpackage_payload(db: Session, item: Workpackage) -> dict[str, Any]:
    revisions = _revisions(db, item.id)
    return {
        "id": item.id,
        "wp_code": item.wp_code,
        "title": item.title,
        "approved_revision_id": item.approved_revision_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "revisions": [revision_payload(revision) for revision in revisions],
    }


def list_workpackages(db: Session) -> list[dict[str, Any]]:
    items = db.scalars(select(Workpackage).order_by(Workpackage.wp_code)).all()
    return [workpackage_payload(db, item) for item in items]


def delete_workpackage(db: Session, item: Workpackage) -> None:
    for row in db.scalars(select(WpEvaluation).where(WpEvaluation.workpackage_id == item.id)).all():
        db.delete(row)
    db.delete(item)
    db.commit()


def get_workpackage(db: Session, workpackage_id: str) -> Workpackage:
    item = db.get(Workpackage, workpackage_id)
    if not item:
        raise ValueError(f"workpackage not found: {workpackage_id}")
    return item


def _next_number(db: Session, workpackage_id: str) -> int:
    current = db.scalar(
        select(func.max(WorkpackageRevision.number)).where(WorkpackageRevision.workpackage_id == workpackage_id)
    )
    return int(current or 0) + 1


def _new_revision(
    db: Session,
    item: Workpackage,
    definition: dict[str, Any],
    created_by: str,
) -> WorkpackageRevision:
    _, text, digest = _canonical_definition(definition)
    revision = WorkpackageRevision(
        workpackage_id=item.id,
        number=_next_number(db, item.id),
        status="draft",
        definition_json=text,
        content_sha256=digest,
        review_notes="[]",
        created_by=created_by,
    )
    db.add(revision)
    db.flush()
    return revision


def create_workpackage(
    db: Session,
    wp_code: str,
    title: str,
    definition: dict[str, Any],
    created_by: str = "user",
) -> Workpackage:
    code = wp_code.strip()
    if db.scalar(select(Workpackage).where(Workpackage.wp_code == code)):
        raise RuntimeError(f"workpackage code already exists: {code}")
    item = Workpackage(wp_code=code, title=title.strip())
    db.add(item)
    db.flush()
    _new_revision(db, item, definition, created_by)
    db.commit()
    db.refresh(item)
    return item


def create_revision(
    db: Session,
    workpackage_id: str,
    definition: dict[str, Any],
    title: str | None = None,
    created_by: str = "user",
) -> WorkpackageRevision:
    item = get_workpackage(db, workpackage_id)
    if title is not None:
        item.title = title.strip()
    revision = _new_revision(db, item, definition, created_by)
    db.commit()
    db.refresh(revision)
    return revision


def _latest_revision(db: Session, workpackage_id: str) -> WorkpackageRevision:
    get_workpackage(db, workpackage_id)
    revision = db.scalar(
        select(WorkpackageRevision)
        .where(WorkpackageRevision.workpackage_id == workpackage_id)
        .order_by(WorkpackageRevision.number.desc())
        .limit(1)
    )
    if not revision:
        raise ValueError(f"workpackage has no revision: {workpackage_id}")
    return revision


def review_latest(db: Session, workpackage_id: str, notes: list[str]) -> WorkpackageRevision:
    revision = _latest_revision(db, workpackage_id)
    if revision.status != "draft":
        raise RuntimeError("only a draft revision can be reviewed")
    revision.status = "reviewed"
    revision.review_notes = json.dumps(notes, ensure_ascii=False)
    db.commit()
    db.refresh(revision)
    return revision


def _approval_errors(definition: dict[str, Any]) -> list[str]:
    signals = definition.get("signals") or []
    criteria = definition.get("criteria") or []
    errors: list[str] = []
    if not signals:
        errors.append("at least one signal is required")
    elif any(signal.get("match_status") != "confirmed" for signal in signals):
        errors.append("all signals must have match_status=confirmed")
    if not criteria:
        errors.append("at least one criterion is required")
    elif any(criterion.get("threshold_source") != "HUMAN_APPROVED" for criterion in criteria):
        errors.append("all criteria must have threshold_source=HUMAN_APPROVED")
    return errors


def approve_latest(db: Session, workpackage_id: str) -> WorkpackageRevision:
    item = get_workpackage(db, workpackage_id)
    revision = _latest_revision(db, workpackage_id)
    if revision.status != "reviewed":
        raise RuntimeError("only a reviewed revision can be approved")
    errors = _approval_errors(json.loads(revision.definition_json))
    if errors:
        raise RuntimeError("; ".join(errors))
    revision.status = "approved"
    item.approved_revision_id = revision.id
    db.commit()
    db.refresh(revision)
    return revision


def export_yaml(db: Session) -> bytes:
    documents: list[dict[str, Any]] = []
    for item in db.scalars(select(Workpackage).order_by(Workpackage.wp_code)).all():
        revisions = _revisions(db, item.id)
        selected = next((r for r in revisions if r.id == item.approved_revision_id), revisions[0] if revisions else None)
        if selected:
            documents.append({
                "wp_code": item.wp_code,
                "title": item.title,
                "source_status": selected.status,
                "definition": json.loads(selected.definition_json),
            })
    payload = {"schema_version": SCHEMA_VERSION, "workpackages": documents}
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode("utf-8")


def import_yaml(db: Session, content: bytes, created_by: str = "user") -> list[Workpackage]:
    if len(content) > 5 * 1024 * 1024:
        raise ValueError("workpackage YAML exceeds 5 MiB")
    try:
        payload = yaml.safe_load(content.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid workpackage YAML: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported workpackage YAML schema_version (expected {SCHEMA_VERSION})")
    entries = payload.get("workpackages")
    if not isinstance(entries, list):
        raise ValueError("workpackages must be a list")

    changed: list[Workpackage] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each workpackage must be an object")
        code = str(entry.get("wp_code") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not code or not title:
            raise ValueError("each workpackage requires wp_code and title")
        definition = entry.get("definition")
        _canonical_definition(definition)
        item = db.scalar(select(Workpackage).where(Workpackage.wp_code == code))
        if item is None:
            item = Workpackage(wp_code=code, title=title)
            db.add(item)
            db.flush()
        else:
            item.title = title
        _new_revision(db, item, definition, created_by)
        changed.append(item)
    db.commit()
    for item in changed:
        db.refresh(item)
    return changed
