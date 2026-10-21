from __future__ import annotations

import json
import tempfile
import uuid
from collections import Counter
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from agent.tools.datasets import registry
from backend.config import ROOT
from backend.db import utcnow
from backend.models import ANNOTATION_FIELDS, CalibrationCommit, Dataset, DatasetAnnotation, DatasetFile, DatasetIdentity
from backend.services.files import DATA_SUFFIXES, safe_filename, sha256_stream
from backend.services.llm import chat_sync


ROLES = {".mdf": "measurement", ".mf4": "measurement", ".dat": "measurement", ".a2l": "a2l", ".hex": "hex"}


def ensure_dataset_serials(session: Session) -> None:
    datasets = session.scalars(select(Dataset).where(~Dataset.id.in_(select(DatasetIdentity.dataset_id))).order_by(Dataset.created_at, Dataset.id)).all()
    if not datasets:
        return
    existing = session.scalars(select(DatasetIdentity.serial)).all()
    next_number = {prefix: max((int(value.rsplit("-", 1)[-1]) for value in existing if value.startswith(f"{prefix}-") and value.rsplit("-", 1)[-1].isdigit()), default=0) + 1 for prefix in {"MDF", "CAL"}}
    for dataset in datasets:
        prefix = "MDF" if dataset.kind == "mdf" else "CAL"
        dataset.identity = DatasetIdentity(serial=f"{prefix}-{next_number[prefix]:04d}")
        next_number[prefix] += 1
    session.commit()


def generate_dataset_name(session: Session, kind: str) -> str:
    prefix = kind.upper()
    today = utcnow().strftime("%Y%m%d")
    stem = f"{prefix}-{today}"
    names = session.scalars(select(Dataset.name).where(Dataset.kind == kind, Dataset.name.like(f"{stem}-%"))).all()
    next_number = 1
    for name in names:
        suffix = name.rsplit("-", 1)[-1]
        if suffix.isdigit():
            next_number = max(next_number, int(suffix) + 1)
    return f"{stem}-{next_number:03d}"


def _validate_dataset_file(kind: str, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in DATA_SUFFIXES:
        raise ValueError(f"不支持的数据文件类型: {suffix or '(无扩展名)'}")
    if kind == "mdf" and suffix not in {".mdf", ".mf4", ".dat"}:
        raise ValueError("MDF 数据集只接受 MDF、MF4 或 DAT")
    if kind == "calibration" and suffix not in {".a2l", ".hex"}:
        raise ValueError("标定数据集只接受 A2L 或 HEX")
    return suffix


def _store_dataset_file(session: Session, dataset: Dataset, filename: str, suffix: str, digest: str, size: int, content: bytes) -> DatasetFile:
    existing = session.scalar(select(DatasetFile).where(DatasetFile.dataset_id == dataset.id, DatasetFile.sha256 == digest))
    if existing:
        return existing
    relative = Path("data") / "files" / dataset.id / f"{digest}-{safe_filename(filename)}"
    target = ROOT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    item = DatasetFile(
        dataset_id=dataset.id,
        role=ROLES[suffix],
        original_name=filename,
        suffix=suffix,
        sha256=digest,
        relative_path=relative.as_posix(),
        size=size,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def create_dataset(session: Session, name: str, kind: str, description: str = "") -> Dataset:
    registry.get(kind)
    dataset = Dataset(name=name, kind=kind, description=description)
    session.add(dataset)
    session.commit()
    session.refresh(dataset)
    ensure_dataset_serials(session)
    return dataset


def add_file(session: Session, dataset: Dataset, stream: BinaryIO, filename: str) -> DatasetFile:
    suffix = _validate_dataset_file(dataset.kind, filename)
    digest, size, content = sha256_stream(stream)
    return _store_dataset_file(session, dataset, filename, suffix, digest, size, content)


def create_dataset_from_file(session: Session, stream: BinaryIO, filename: str, kind: str = "mdf", description: str = "数据分析") -> tuple[Dataset, bool]:
    registry.get(kind)
    suffix = _validate_dataset_file(kind, filename)
    digest, size, content = sha256_stream(stream)
    existing_file = session.scalar(select(DatasetFile).join(Dataset).where(Dataset.kind == kind, DatasetFile.sha256 == digest))
    if existing_file:
        ensure_dataset_serials(session)
        return existing_file.dataset, True
    dataset = Dataset(name=generate_dataset_name(session, kind), kind=kind, description=description)
    session.add(dataset)
    session.commit()
    session.refresh(dataset)
    _store_dataset_file(session, dataset, filename, suffix, digest, size, content)
    session.refresh(dataset)
    ensure_dataset_serials(session)
    return dataset, False


# ponytail: 合并产物存在 outputs/ 下（与 runs/wiki-revisions 同约定），不进 data/files 数据集目录，避免污染数据集
MERGE_OUTPUT_DIR = ROOT / "outputs" / "merges"
CONVERSION_OUTPUT_DIR = ROOT / "outputs" / "conversions"
_MEASUREMENT_SUFFIXES = {".mdf", ".mf4", ".dat"}


def _resolve_merge_dir(token: str) -> Path:
    # token 只允许 uuid hex，挡住路径穿越
    if not token.isalnum():
        raise ValueError("无效的合并令牌")
    return MERGE_OUTPUT_DIR / token


def _resolve_conversion_dir(token: str) -> Path:
    # token 只允许 uuid hex，挡住路径穿越
    if not token.isalnum():
        raise ValueError("无效的转换令牌")
    return CONVERSION_OUTPUT_DIR / token


def merge_uploaded_mf4(file_a: BinaryIO, name_a: str, file_b: BinaryIO, name_b: str, out_name: str | None = None) -> dict:
    """合并两个上传的 MF4 文件，结果落在 outputs/merges/{token}/，不创建数据集。"""
    from agent.tools.mf4_merge import MF4Merger  # 延迟导入：scipy/asammdf 较重，且仅此处用

    for nm in (name_a, name_b):
        suffix = Path(nm).suffix.lower()
        if suffix not in _MEASUREMENT_SUFFIXES:
            raise ValueError(f"不支持的文件类型: {suffix or '(无扩展名)'}（仅接受 MF4 / MDF / DAT）")

    token = uuid.uuid4().hex
    out_dir = _resolve_merge_dir(token)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mf4_upload_") as tmp:
        path_a = Path(tmp) / safe_filename(name_a)
        path_b = Path(tmp) / safe_filename(name_b)
        path_a.write_bytes(file_a.read())
        path_b.write_bytes(file_b.read())
        merged = MF4Merger().merge_files(str(path_a), str(path_b), output_dir=str(out_dir))

    final_name = out_name.strip() if out_name and out_name.strip() else f"{Path(name_a).stem}__MERGED__"
    if not final_name.lower().endswith(".mf4"):
        final_name += ".mf4"
    final_path = out_dir / safe_filename(final_name)
    Path(merged).replace(final_path)
    return {"token": token, "name": final_path.name, "size": final_path.stat().st_size}


def merge_datasets(
    session: Session,
    dataset_a_id: str,
    dataset_b_id: str,
    name: str = "",
    signal_pairs: list[tuple[str, str]] | None = None,
) -> Dataset:
    """Merge the primary measurement files of two catalogued MDF datasets."""
    from agent.tools.mf4_merge import MF4Merger

    if dataset_a_id == dataset_b_id:
        raise ValueError("不能把同一个数据集与自身合并")
    datasets = [session.get(Dataset, dataset_a_id), session.get(Dataset, dataset_b_id)]
    if any(item is None for item in datasets):
        raise ValueError("待合并的数据集不存在")
    if any(item.kind != "mdf" for item in datasets):
        raise ValueError("merge_mf4 只支持 MDF 数据集")
    files = [next((file for file in item.files if file.role == "measurement"), None) for item in datasets]
    if any(item is None for item in files):
        raise ValueError("待合并的数据集缺少测量文件")

    token = uuid.uuid4().hex
    out_dir = _resolve_merge_dir(token)
    out_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {"signal_pairs": signal_pairs} if signal_pairs else {}
    merged = Path(MF4Merger(**kwargs).merge_files(
        str(ROOT / files[0].relative_path), str(ROOT / files[1].relative_path), output_dir=str(out_dir)
    ))
    final_name = (name.strip() or f"{Path(files[0].original_name).stem}__MERGED__")
    if not final_name.lower().endswith(".mf4"):
        final_name += ".mf4"
    final_path = out_dir / safe_filename(final_name)
    if merged.resolve() != final_path.resolve():
        merged.replace(final_path)
    return save_merge_as_dataset(session, token, final_name)


def merge_output_path(token: str) -> Path:
    out_dir = _resolve_merge_dir(token)
    files = sorted(out_dir.glob("*.mf4")) if out_dir.exists() else []
    if not files:
        raise FileNotFoundError("合并结果不存在或已被清理")
    return files[0]


def save_merge_as_dataset(session: Session, token: str, name: str | None = None) -> Dataset:
    path = merge_output_path(token)
    out_name = name.strip() if name and name.strip() else path.name
    if not out_name.lower().endswith(".mf4"):
        out_name += ".mf4"
    with open(path, "rb") as stream:
        dataset, _ = create_dataset_from_file(session, stream, out_name, kind="mdf", description="MF4 合并")
    return dataset


def convert_uploaded_excel(
    file: BinaryIO,
    filename: str,
    out_name: str | None = None,
    sample_rate: float = 1.0,
    units_row: str = "auto",
) -> dict:
    """把上传的 .xlsx 转换成 MF4，结果落在 outputs/conversions/{token}/，不创建数据集。"""
    from agent.tools.excel_to_mf4 import ExcelToMf4  # 延迟导入：openpyxl/asammdf 较重，且仅此处用

    if Path(filename).suffix.lower() != ".xlsx":
        raise ValueError(f"不支持的文件类型: {Path(filename).suffix or '(无扩展名)'}（仅接受 .xlsx）")

    token = uuid.uuid4().hex
    out_dir = _resolve_conversion_dir(token)
    out_dir.mkdir(parents=True, exist_ok=True)

    final_stem = out_name.strip() if out_name and out_name.strip() else f"{Path(filename).stem}__FROM_XLSX__"

    with tempfile.TemporaryDirectory(prefix="xlsx_upload_") as tmp:
        src = Path(tmp) / safe_filename(filename)
        src.write_bytes(file.read())
        result = ExcelToMf4().convert_file(
            str(src),
            output_dir=str(out_dir),
            out_name=safe_filename(final_stem),
            sample_rate=sample_rate,
            units_row=units_row,
        )

    final_path = Path(result.path)
    return {
        "token": token,
        "name": final_path.name,
        "size": final_path.stat().st_size,
        "channels": result.channels,
        "rows": result.rows,
        "skipped": result.skipped,
        "time_source": result.time_source,
    }


def conversion_output_path(token: str) -> Path:
    out_dir = _resolve_conversion_dir(token)
    files = sorted(out_dir.glob("*.mf4")) if out_dir.exists() else []
    if not files:
        raise FileNotFoundError("转换结果不存在或已被清理")
    return files[0]


def save_conversion_as_dataset(session: Session, token: str, name: str | None = None) -> Dataset:
    path = conversion_output_path(token)
    out_name = name.strip() if name and name.strip() else path.name
    if not out_name.lower().endswith(".mf4"):
        out_name += ".mf4"
    with open(path, "rb") as stream:
        dataset, _ = create_dataset_from_file(session, stream, out_name, kind="mdf", description="Excel 转换")
    return dataset


def delete_dataset(session: Session, dataset: Dataset) -> None:
    paths = [ROOT / item.relative_path for item in dataset.files]
    # Calibration commits reference dataset_files (a2l/hex). Remove commits
    # first so ORM can safely delete dataset_files via Dataset.files cascade.
    session.execute(delete(CalibrationCommit).where(CalibrationCommit.dataset_id == dataset.id))
    session.delete(dataset)
    session.commit()
    for path in paths:
        path.unlink(missing_ok=True)


def dataset_payload(dataset: Dataset) -> dict:
    paths = [ROOT / item.relative_path for item in dataset.files]
    adapter = registry.get(dataset.kind)
    files = [
        {"id": item.id, "name": item.original_name, "role": item.role, "suffix": item.suffix, "sha256": item.sha256, "size": item.size}
        for item in dataset.files
    ]
    primary = files[0] if files else None
    total_size = sum(item["size"] for item in files)
    summary = {
        "display_name": dataset.name,
        "source_file": primary["name"] if primary else "",
        "file_ext": primary["suffix"] if primary else "",
        "file_count": len(files),
        "size": total_size,
        "sha256_short": primary["sha256"][:12] if primary else "",
        "label": " · ".join(value for value in [dataset.name, dataset.description, primary["name"] if primary else ""] if value),
    }
    annotation = dataset.annotation
    serial = dataset.identity.serial if dataset.identity else dataset.id[:8]
    return {
        "id": dataset.id,
        "serial": serial,
        "name": dataset.name,
        "kind": dataset.kind,
        "description": dataset.description,
        "created_at": dataset.created_at,
        "files": files,
        "summary": summary,
        "metadata": {
            key: getattr(annotation, key) if annotation else ""
            for key in ANNOTATION_FIELDS
        },
        "inspection": adapter.inspect(paths),
    }


def update_dataset(session: Session, dataset: Dataset, name: str | None = None, description: str | None = None, **metadata: str | None) -> Dataset:
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("数据集名称不能为空")
        if len(clean_name) > 80:
            raise ValueError("数据集名称不能超过 80 个字符")
        dataset.name = clean_name
    if description is not None:
        clean_description = description.strip()
        if len(clean_description) > 500:
            raise ValueError("数据集备注不能超过 500 个字符")
        dataset.description = clean_description
    if any(value is not None for value in metadata.values()):
        annotation = dataset.annotation or DatasetAnnotation(dataset_id=dataset.id)
        for key, value in metadata.items():
            if value is not None:
                setattr(annotation, key, value.strip())
        session.add(annotation)
    session.add(dataset)
    session.commit()
    session.refresh(dataset)
    return dataset


def _line_changes(old: DatasetFile | None, new: DatasetFile) -> dict:
    # 内容寻址：sha 相同即文件相同，无需读文件。否则用多重集差 O(n) 统计增删行数，
    # 避免 SequenceMatcher 在几十万~上百万行的 HEX/A2L 上退化为 O(n·m)。
    if not old or old.sha256 == new.sha256:
        return {"added": 0, "removed": 0}
    old_c = Counter((ROOT / old.relative_path).read_text(encoding="utf-8", errors="replace").splitlines())
    new_c = Counter((ROOT / new.relative_path).read_text(encoding="utf-8", errors="replace").splitlines())
    return {"added": sum((new_c - old_c).values()), "removed": sum((old_c - new_c).values())}


def _file_diff(old: DatasetFile | None, new: DatasetFile) -> dict:
    return {
        "from_sha256": old.sha256 if old else None,
        "to_sha256": new.sha256,
        "from_size": old.size if old else None,
        "to_size": new.size,
        "changed": not old or old.sha256 != new.sha256,
        "lines": _line_changes(old, new),
    }


def create_calibration_commit(session: Session, dataset: Dataset, parent_id: str | None, a2l_file_id: str | None, hex_file_id: str) -> CalibrationCommit:
    if dataset.kind != "calibration":
        raise ValueError("只有 A2L/HEX 数据集可以创建 commit")
    parent = session.get(CalibrationCommit, parent_id) if parent_id else None
    if parent_id and (not parent or parent.dataset_id != dataset.id):
        raise ValueError("父 commit 不属于当前数据集")
    a2l = session.get(DatasetFile, a2l_file_id) if a2l_file_id else (session.get(DatasetFile, parent.a2l_file_id) if parent else None)
    hex_file = session.get(DatasetFile, hex_file_id)
    if not a2l:
        raise ValueError("首个 commit 必须选择 A2L 文件")
    if a2l.dataset_id != dataset.id or a2l.suffix != ".a2l":
        raise ValueError("A2L 文件不属于当前数据集或类型不正确")
    if not hex_file or hex_file.dataset_id != dataset.id or hex_file.suffix != ".hex":
        raise ValueError("HEX 文件不属于当前数据集或类型不正确")
    old_a2l = session.get(DatasetFile, parent.a2l_file_id) if parent else None
    old_hex = session.get(DatasetFile, parent.hex_file_id) if parent else None
    diff = {"a2l": _file_diff(old_a2l, a2l), "hex": _file_diff(old_hex, hex_file)}
    fallback = f"更新 {hex_file.original_name} ({(old_hex.sha256[:8] if old_hex else 'initial')} → {hex_file.sha256[:8]})"
    try:
        message = chat_sync([
            {"role": "system", "content": "根据差异生成一句简短中文 commit message，不要补充未知事实。"},
            {"role": "user", "content": json.dumps(diff, ensure_ascii=False)},
        ]) or fallback
    except Exception:
        message = fallback
    commit = CalibrationCommit(
        dataset_id=dataset.id,
        parent_id=parent.id if parent else None,
        a2l_file_id=a2l.id,
        hex_file_id=hex_file.id,
        message=message[:512],
        diff_json=json.dumps(diff, ensure_ascii=False),
    )
    session.add(commit)
    session.commit()
    session.refresh(commit)
    return commit


def commit_payload(session: Session, commit: CalibrationCommit) -> dict:
    a2l = session.get(DatasetFile, commit.a2l_file_id)
    hex_file = session.get(DatasetFile, commit.hex_file_id)
    return {
        "id": commit.id,
        "parent_id": commit.parent_id,
        "message": commit.message,
        "diff": json.loads(commit.diff_json),
        "a2l": {"id": a2l.id, "name": a2l.original_name, "sha256": a2l.sha256},
        "hex": {"id": hex_file.id, "name": hex_file.original_name, "sha256": hex_file.sha256},
        "created_at": commit.created_at,
    }
