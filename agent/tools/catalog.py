from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import CalibrationCommit, Dataset, DatasetAnnotation, DatasetFile, DatasetIdentity


SERIAL_RE = re.compile(r"\b(?:MDF|CAL)-\d{4}\b", re.IGNORECASE)


def find_datasets(
    session: Session,
    *,
    kind: str | None = None,
    measurement_date: str | None = None,
    experiment: str | None = None,
    keyword: str | None = None,
    limit: int = 10,
) -> list[Dataset]:
    query = select(Dataset).join(DatasetIdentity).order_by(Dataset.created_at.desc()).limit(min(max(limit, 1), 20))
    if kind:
        query = query.where(Dataset.kind == kind)
    if measurement_date:
        query = query.where(Dataset.annotation.has(measurement_date=measurement_date))
    if experiment:
        query = query.where(Dataset.annotation.has(experiment=experiment))
    if keyword:
        value = f"%{keyword}%"
        query = query.where(
            Dataset.name.ilike(value)
            | Dataset.description.ilike(value)
            | Dataset.annotation.has(DatasetAnnotation.experiment.ilike(value))
            | Dataset.annotation.has(DatasetAnnotation.a2l.ilike(value))
            | Dataset.annotation.has(DatasetAnnotation.hex.ilike(value))
            | Dataset.files.any(DatasetFile.original_name.ilike(value))
        )
    return list(session.scalars(query).all())


def get_dataset(session: Session, serial: str) -> Dataset | None:
    return session.scalar(select(Dataset).join(DatasetIdentity).where(DatasetIdentity.serial == serial.upper()))


def _file_line(item: DatasetFile) -> str:
    return f"{item.original_name}（{item.role}，{item.size} B，SHA256 {item.sha256}）"


def _dataset_hit(session: Session, dataset: Dataset, *, detail: bool) -> dict:
    serial = dataset.identity.serial
    annotation = dataset.annotation
    fields = [f"编号：{serial}", f"名称：{dataset.name}", f"类型：{dataset.kind}"]
    if dataset.description:
        fields.append(f"备注：{dataset.description}")
    if annotation:
        fields.extend(value for value in (
            f"日期：{annotation.measurement_date}" if annotation.measurement_date else "",
            f"Experiment：{annotation.experiment}" if annotation.experiment else "",
            f"A2L：{annotation.a2l}" if annotation.a2l else "",
            f"HEX：{annotation.hex}" if annotation.hex else "",
        ) if value)
    fields.append(f"创建时间：{dataset.created_at.isoformat()}")
    if detail:
        fields.append("文件：" + ("；".join(_file_line(item) for item in dataset.files) or "无"))
        if dataset.kind == "calibration":
            commits = session.scalars(
                select(CalibrationCommit).where(CalibrationCommit.dataset_id == dataset.id).order_by(CalibrationCommit.created_at.desc()).limit(20)
            ).all()
            history = []
            for commit in commits:
                a2l = session.get(DatasetFile, commit.a2l_file_id)
                hex_file = session.get(DatasetFile, commit.hex_file_id)
                history.append(
                    f"{commit.id[:8]} parent={commit.parent_id[:8] if commit.parent_id else '-'} "
                    f"A2L={a2l.original_name if a2l else '-'} HEX={hex_file.original_name if hex_file else '-'} {commit.message}"
                )
            fields.append("提交历史：" + ("；".join(history) or "无"))
    return {
        "kind": "database",
        "id": dataset.id,
        "title": serial,
        "content": "\n".join(fields),
        "score": 1.0,
        "path": f"database:datasets/{serial}",
    }


def query_catalog(session: Session, question: str, limit: int = 10) -> list[dict]:
    serial_match = SERIAL_RE.search(question)
    if serial_match:
        dataset = get_dataset(session, serial_match.group())
        return [_dataset_hit(session, dataset, detail=True)] if dataset else []

    date_match = re.search(r"((?:19|20)\d{2})[-/.]?(\d{2})[-/.]?(\d{2})", question)
    date = "-".join(date_match.groups()) if date_match else None
    lower = question.lower()
    kind = "calibration" if any(term in lower for term in ("标定", "calibration", "commit")) else "mdf" if any(term in lower for term in ("mf4", "mdf", "experiment")) else None
    technical = [
        term for term in re.findall(r"[A-Za-z0-9_./-]{4,}", question)
        if term.lower() not in {"mf4", "mdf", "a2l", "hex", "experiment", "calibration", "commit"}
        and (not date_match or term != date_match.group())
    ]
    datasets = find_datasets(session, kind=kind, measurement_date=date, keyword=max(technical, key=len) if technical else None, limit=limit)
    return [_dataset_hit(session, item, detail=len(datasets) == 1) for item in datasets]
