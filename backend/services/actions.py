"""Executable actions for chat task/agent mode.

The task agent (backend.services.chat) lets the LLM gather parameters through
clarifying questions, then proposes a structured ``cali-action`` block. Only the
actions registered here can actually run; every executor is deterministic and
reuses the existing dataset services so the audit/immutability contract holds.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.tools.catalog import get_dataset
from backend.config import ROOT
from backend.models import Dataset, DatasetIdentity
from backend.services.datasets import merge_uploaded_mf4, save_merge_as_dataset


def available_mdf_datasets(session: Session, limit: int = 20) -> list[dict[str, str]]:
    rows = session.scalars(
        select(Dataset)
        .join(DatasetIdentity)
        .where(Dataset.kind == "mdf")
        .order_by(Dataset.created_at.desc())
        .limit(limit)
    ).all()
    return [{"serial": item.identity.serial, "name": item.name} for item in rows if item.identity]


def catalog_brief(session: Session) -> str:
    """One-line MDF catalog injected into the task system prompt so the LLM can
    offer real, selectable datasets instead of inventing file names."""
    items = available_mdf_datasets(session)
    if not items:
        return "（当前没有可用的 MDF 数据集）"
    return "；".join(f"{item['serial']}={item['name']}" for item in items)


def _resolve_mdf_file(session: Session, ref: str):
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("数据集编号不能为空")
    dataset = get_dataset(session, ref) or session.get(Dataset, ref)
    if not dataset:
        raise ValueError(f"找不到数据集：{ref}")
    if dataset.kind != "mdf":
        raise ValueError(f"数据集 {ref} 不是 MDF 类型，无法合并")
    measurement = next((item for item in dataset.files if item.role == "measurement"), None)
    if not measurement:
        raise ValueError(f"数据集 {ref} 没有 MDF/MF4 测量文件")
    return dataset, measurement


def _action_merge_mf4(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    ref_a = params.get("file_a") or params.get("a")
    ref_b = params.get("file_b") or params.get("b")
    name = str(params.get("name") or "").strip()
    if not ref_a or not ref_b:
        raise ValueError("merge_mf4 需要 file_a 与 file_b 两个数据集编号")
    if str(ref_a).strip() == str(ref_b).strip():
        raise ValueError("不能把同一个数据集与自身合并")

    dataset_a, file_a = _resolve_mdf_file(session, str(ref_a))
    dataset_b, file_b = _resolve_mdf_file(session, str(ref_b))
    path_a = ROOT / file_a.relative_path
    path_b = ROOT / file_b.relative_path
    with open(path_a, "rb") as stream_a, open(path_b, "rb") as stream_b:
        merged = merge_uploaded_mf4(
            stream_a, file_a.original_name, stream_b, file_b.original_name, name or None
        )
    dataset = save_merge_as_dataset(session, merged["token"], name or None)
    session.refresh(dataset)
    serial = dataset.identity.serial if dataset.identity else dataset.id[:8]
    return {
        "summary": (
            f"已合并 {dataset_a.identity.serial} 与 {dataset_b.identity.serial} → "
            f"新 MDF 数据集 {serial}（{dataset.name}）。"
        ),
        "dataset_id": dataset.id,
        "serial": serial,
    }


# action name -> executor(session, params) -> result dict (must contain "summary")
REGISTRY: dict[str, Callable[[Session, dict[str, Any]], dict[str, Any]]] = {
    "merge_mf4": _action_merge_mf4,
}


def describe_actions() -> str:
    """Human-readable list of executable actions for the system prompt."""
    return "merge_mf4(file_a, file_b, name?) — 合并两个 MDF 数据集为一个新 MDF 数据集"


def run_action(session: Session, action: str, params: dict[str, Any]) -> dict[str, Any]:
    executor = REGISTRY.get(action)
    if executor is None:
        raise ValueError(f"不支持的操作：{action}")
    return executor(session, params or {})
