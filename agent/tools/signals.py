"""Deterministic signal-window detection.

Pure numeric helpers — no LLM, no file parsing. They operate on plain
sequences so they are fully unit-testable without asammdf. The MDF adapter
feeds real channel data in; an analysis service turns the output into an
API payload.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Callable

import numpy as np


# Comparison operators the caller may request, kept explicit so an unknown
# operator fails loudly instead of silently doing the wrong thing.
OPS: dict[str, Callable[[float, float], bool]] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def find_windows(
    timestamps: Sequence[float],
    samples: Sequence[float],
    op: str,
    threshold: float,
    min_duration: float = 0.0,
) -> list[dict[str, Any]]:
    """Return contiguous windows where ``sample <op> threshold`` holds.

    A window is a maximal run of consecutive samples that satisfy the
    condition. Runs shorter than ``min_duration`` (seconds) are dropped.
    Each window reports its time bounds and basic stats so the caller does
    not have to re-read the data.
    """
    if op not in OPS:
        raise ValueError(f"不支持的比较操作符: {op}（可用: {', '.join(OPS)}）")
    if len(timestamps) != len(samples):
        raise ValueError("时间戳与采样点数量不一致")
    test = OPS[op]
    windows: list[dict[str, Any]] = []
    n = len(samples)
    run_start: int | None = None

    for i in range(n):
        hit = test(samples[i], threshold)
        if hit and run_start is None:
            run_start = i
        # Close the run when the condition breaks or the series ends.
        ending = (not hit) or (i == n - 1)
        if run_start is not None and ending:
            run_end = i if (hit and i == n - 1) else i - 1
            segment = samples[run_start:run_end + 1]
            t0, t1 = timestamps[run_start], timestamps[run_end]
            if t1 - t0 >= min_duration:
                windows.append({
                    "start": float(t0),
                    "end": float(t1),
                    "duration": float(t1 - t0),
                    "samples": len(segment),
                    "min": float(min(segment)),
                    "max": float(max(segment)),
                    "mean": float(sum(segment) / len(segment)),
                })
            run_start = None

    return windows


def find_mask_windows(
    timestamps: Sequence[float],
    mask: Sequence[bool],
    min_duration: float = 0.0,
    merge_gap: float = 0.0,
) -> list[dict[str, Any]]:
    if len(timestamps) != len(mask):
        raise ValueError("timestamps and mask must have equal lengths")
    raw: list[dict[str, Any]] = []
    start: int | None = None
    for i, hit in enumerate(mask):
        if bool(hit) and start is None:
            start = i
        if start is not None and ((not bool(hit)) or i == len(mask) - 1):
            end = i if bool(hit) and i == len(mask) - 1 else i - 1
            raw.append({"start": float(timestamps[start]), "end": float(timestamps[end])})
            start = None
    merged: list[dict[str, Any]] = []
    for window in raw:
        if merged and window["start"] - merged[-1]["end"] <= merge_gap:
            merged[-1]["end"] = window["end"]
        else:
            merged.append(window)
    return [{**item, "duration": item["end"] - item["start"]} for item in merged
            if item["end"] - item["start"] >= min_duration]


def safe_stats(samples: Sequence[float]) -> dict[str, float | int]:
    values = np.asarray(samples, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return {"samples": 0}
    return {"samples": int(len(values)), "min": float(np.min(values)), "max": float(np.max(values)),
            "mean": float(np.mean(values)), "p5": float(np.percentile(values, 5)), "p95": float(np.percentile(values, 95))}


def tracking_error(setpoint: Sequence[float], actual: Sequence[float]) -> dict[str, float]:
    error = np.abs(np.asarray(setpoint, dtype=float) - np.asarray(actual, dtype=float))
    return {"mean_abs_err": float(np.mean(error)), "max_abs_err": float(np.max(error)),
            "p95_abs_err": float(np.percentile(error, 95))}


def bool_events(timestamps: Sequence[float], samples: Sequence[float]) -> dict[str, float | int | None]:
    time = np.asarray(timestamps, dtype=float)
    values = np.asarray(samples).astype(bool)
    if len(time) != len(values) or not len(time):
        raise ValueError("timestamps and samples must be non-empty and have equal lengths")
    rises = int(np.sum(np.diff(values.astype(int)) == 1))
    active = float(np.sum(values[:-1] * np.diff(time))) if len(time) > 1 else 0.0
    total = float(time[-1] - time[0])
    first = np.flatnonzero(values)
    return {"events": rises, "active_s": active, "active_pct": active / total * 100 if total > 0 else 0.0,
            "first_t_s": float(time[first[0]]) if len(first) else None}


def detect_startup_events(
    timestamps: Sequence[float],
    engine_speed: Sequence[float],
    low_rpm: float = 200,
    running_rpm: float = 400,
) -> list[dict[str, float]]:
    time = np.asarray(timestamps, dtype=float)
    speed = np.asarray(engine_speed, dtype=float)
    if len(time) != len(speed):
        raise ValueError("timestamps and engine speed must have equal lengths")
    events: list[dict[str, float]] = []
    armed = True
    for index in range(1, len(speed)):
        if armed and speed[index - 1] < low_rpm and speed[index] > running_rpm:
            events.append({"t_s": float(time[index]), "nmot_peak_rpm": float(np.max(speed[index:index + 300]))})
            armed = False
        elif speed[index] < low_rpm:
            armed = True
    return events
