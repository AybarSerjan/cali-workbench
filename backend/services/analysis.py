"""Signal-window analysis: deterministic, evidence-first.

This is the numeric half of the signal-analysis feature. It reads a channel
via the dataset adapter and applies :func:`find_windows`. It does NOT explain
causes — that belongs to a later LLM specialist node and must cite the windows
produced here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from agent.tools.datasets import registry
from agent.tools.obfcm import DEFAULT_CHANNEL_MAP, compute_obfcm
from agent.tools.signals import find_windows
from backend.config import ROOT
from backend.models import Dataset


def find_signal_windows(
    dataset: Dataset,
    signal: str,
    op: str,
    threshold: float,
    min_duration: float = 0.0,
) -> dict[str, Any]:
    if dataset.kind != "mdf":
        raise ValueError("信号窗口分析仅支持 MDF 数据集")
    if not dataset.files:
        raise ValueError("数据集为空，请先上传 MF4/DAT 文件")
    adapter = registry.get(dataset.kind)
    paths = [ROOT / item.relative_path for item in dataset.files]
    channel = adapter.read_channel(paths, signal)

    # The adapter sees content-addressed on-disk names; report the original
    # upload name so windows are meaningful to the user.
    original_name = {Path(item.relative_path).name: item.original_name for item in dataset.files}

    # Each MF4/DAT file is its own time base, so window detection runs per
    # segment and every window is tagged with its source file.
    windows: list[dict[str, Any]] = []
    for segment in channel["segments"]:
        label = original_name.get(segment["file"], segment["file"])
        for window in find_windows(segment["timestamps"], segment["samples"], op, threshold, min_duration):
            windows.append({"file": label, **window})

    return {
        "dataset_id": dataset.id,
        "signal": signal,
        "unit": channel["unit"],
        "op": op,
        "threshold": threshold,
        "min_duration": min_duration,
        "window_count": len(windows),
        "windows": windows,
    }


# Roles whose absence makes the K factor uncomputable; emission roles are optional.
_OBFCM_REQUIRED = ("rpm", "load", "obfcm_fuel", "fuel_sum", "conv_ratio", "fuel_density")
_OBFCM_OPTIONAL = ("thc_s", "co_s", "co2_s")


def compute_obfcm_result(dataset: Dataset, channel_map: dict[str, str] | None = None) -> dict[str, Any]:
    """Read the OBFCM signals from a dataset, align them, and compute result + factor.

    Channels live on independent time bases, so each role is interpolated onto the
    engine-speed (``rpm``) timestamps per file and pooled across files — mirroring
    the reference tool, which analyses one merged, row-aligned table.
    """
    if dataset.kind != "mdf":
        raise ValueError("OBFCM 分析仅支持 MDF 数据集")
    if not dataset.files:
        raise ValueError("数据集为空，请先上传 MF4/DAT 文件")

    names = {**DEFAULT_CHANNEL_MAP, **(channel_map or {})}
    adapter = registry.get(dataset.kind)
    paths = [ROOT / item.relative_path for item in dataset.files]

    # Read each role's channel once; missing required channels fail loudly.
    raw: dict[str, dict[str, Any]] = {}
    missing_required = []
    for role in (*_OBFCM_REQUIRED, *_OBFCM_OPTIONAL):
        try:
            raw[role] = adapter.read_channel(paths, names[role])
        except ValueError:
            if role in _OBFCM_REQUIRED:
                missing_required.append(names[role])
    if missing_required:
        raise ValueError(f"数据集缺少 OBFCM 必需通道: {', '.join(missing_required)}")

    has_emissions = all(role in raw for role in _OBFCM_OPTIONAL)

    # Per file: align every role onto the rpm channel's timestamps, then pool.
    pooled: dict[str, list[np.ndarray]] = {role: [] for role in raw}
    rpm_segments = {seg["file"]: seg for seg in raw["rpm"]["segments"]}
    for file_name, rpm_seg in rpm_segments.items():
        base_t = np.asarray(rpm_seg["timestamps"], dtype=float)
        if base_t.size == 0:
            continue
        for role, data in raw.items():
            seg = next((s for s in data["segments"] if s["file"] == file_name), None)
            if seg is None or not seg["timestamps"]:
                pooled[role].append(np.full(base_t.shape, np.nan))
                continue
            t = np.asarray(seg["timestamps"], dtype=float)
            v = np.asarray(seg["samples"], dtype=float)
            pooled[role].append(v if role == "rpm" else np.interp(base_t, t, v))

    aligned = {role: np.concatenate(parts) if parts else np.array([]) for role, parts in pooled.items()}

    kwargs = {role: aligned[role] for role in _OBFCM_REQUIRED}
    if has_emissions:
        kwargs.update({role: aligned[role] for role in _OBFCM_OPTIONAL})

    result = compute_obfcm(**kwargs)
    result["dataset_id"] = dataset.id
    result["channel_map"] = {role: names[role] for role in (*_OBFCM_REQUIRED, *_OBFCM_OPTIONAL)}
    return result
