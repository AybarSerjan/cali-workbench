from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.config import ROOT
from backend.models import Run


def json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temp, path)


def write_agent_artifact(run_id: str, agent: str, name: str, value: Any) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9_-]+", agent) or not re.fullmatch(r"[a-z0-9_.-]+", name):
        raise ValueError("无效的 Agent artifact 名称")
    relative = Path("outputs") / "runs" / run_id / "agents" / agent / name
    path = ROOT / relative
    json_write(path, value)
    return {"path": relative.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def create_run(session: Session, kind: str, input_sha256: str = "", conversation_id: str | None = None) -> Run:
    run = Run(kind=kind, input_sha256=input_sha256, conversation_id=conversation_id)
    session.add(run)
    session.flush()
    relative = Path("outputs") / "runs" / run.id / "manifest.json"
    run.manifest_path = relative.as_posix()
    session.commit()
    update_run(session, run, status="running", node="created")
    return run


def append_event(run: Run, node: str, status: str, detail: dict[str, Any] | None = None) -> None:
    path = ROOT / "outputs" / "runs" / run.id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "at": datetime.now(timezone.utc).isoformat(),
        "node": node,
        "status": status,
        "detail": detail or {},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def update_run(session: Session, run: Run, *, status: str, node: str, error: str = "", artifacts: list[str] | None = None) -> None:
    run.status = status
    run.current_node = node
    run.error = error
    session.commit()
    append_event(run, node, status, {"error": error} if error else {})
    artifact_data: dict[str, dict[str, Any]] = {}
    manifest_path = ROOT / run.manifest_path
    if manifest_path.exists():
        try:
            for item in json.loads(manifest_path.read_text(encoding="utf-8")).get("artifacts", []):
                if item.get("path"):
                    artifact_data[item["path"]] = item
        except (OSError, ValueError, TypeError):
            pass
    for relative in artifacts or []:
        path = ROOT / relative
        artifact_data[relative] = {
            "path": relative,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None,
        }
    manifest = {
        "schema_version": 1,
        "run_id": run.id,
        "kind": run.kind,
        "status": run.status,
        "current_node": run.current_node,
        "input_sha256": run.input_sha256,
        "conversation_id": run.conversation_id,
        "error": run.error,
        "artifacts": [artifact_data[key] for key in sorted(artifact_data)],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    json_write(ROOT / run.manifest_path, manifest)


def record_agent_reports(session: Session, run: Run, reports: list[dict[str, Any]]) -> None:
    artifacts: list[str] = []
    for report in sorted(reports, key=lambda item: item["agent"]):
        relative = Path("outputs") / "runs" / run.id / "agents" / f"{report['agent']}.json"
        json_write(ROOT / relative, report)
        artifacts.append(relative.as_posix())
        artifacts.extend(
            item["path"] for item in report.get("artifacts", []) if item.get("path")
        )
    update_run(session, run, status="running", node="merge", artifacts=artifacts)
