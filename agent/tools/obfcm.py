"""Deterministic OBFCM (On-Board Fuel Consumption Monitoring) analysis.

Pure numeric helper — no LLM, no file parsing. It takes already-aligned signal
arrays (one value per sample, all the same length) and returns:

* the OBFCM result: per-sample reference fuel from carbon balance vs. the
  OBFCM-reported fuel, and the OBFCM accuracy, with summary stats;
* the K factor: the fuel correction factor per sample, plus a K-factor map
  binned by engine speed × engine load (the measured average per cell — no
  machine-learning fill, so every cell is real evidence or empty).

Ported from Inno_OBFCM_AutoTool's fuel_consumption_analysis / k_factor_analysis.
The dataset adapter feeds real channels in; backend.services.analysis aligns
them and turns this output into an API payload.
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np

# Default operating-region grid (engine speed rpm × relative injection load %).
# Matches the reference tool's IntervalConfig defaults.
DEFAULT_RPM_INTERVALS = [700, 1200, 2500, 4000, 5500, 7000]
DEFAULT_LOAD_INTERVALS = [20, 50, 80, 110, 140, 170]

# Channel roles → default MDF channel names (override per dataset if needed).
DEFAULT_CHANNEL_MAP = {
    "rpm": "Epm_nEng",
    "load": "rkme_w_msg",
    "obfcm_fuel": "InjSys_dvolFu",
    "fuel_sum": "rksum_w",
    "conv_ratio": "InjSys_facCnvnRatFuM",
    "fuel_density": "InjSys_rhoFuStd",
    # Optional — only needed for OBFCM accuracy against the carbon-balance reference.
    "thc_s": "THC_g_s",
    "co_s": "CO_g_s",
    "co2_s": "CO2_g_s",
}

# Carbon-balance fuel from g/s emissions → L/s. Constants per the reference.
def _fc_l_s(thc: np.ndarray, co: np.ndarray, co2: np.ndarray) -> np.ndarray:
    return (0.0012 / 0.75) * (0.829 * thc + 0.429 * co + 0.273 * co2)


def _stats(values: np.ndarray) -> dict[str, float] | None:
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    return {
        "mean": float(np.mean(values)),
        "std": float(np.std(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "count": int(values.size),
    }


def compute_obfcm(
    *,
    rpm: Sequence[float],
    load: Sequence[float],
    obfcm_fuel: Sequence[float],
    fuel_sum: Sequence[float],
    conv_ratio: Sequence[float],
    fuel_density: Sequence[float],
    thc_s: Sequence[float] | None = None,
    co_s: Sequence[float] | None = None,
    co2_s: Sequence[float] | None = None,
    rpm_intervals: Sequence[float] = DEFAULT_RPM_INTERVALS,
    load_intervals: Sequence[float] = DEFAULT_LOAD_INTERVALS,
) -> dict[str, Any]:
    """Compute OBFCM result and K factor from aligned signal arrays.

    All required arrays must have the same length. Emission arrays are optional;
    without them only the K factor is produced (accuracy needs a reference fuel).
    """
    rpm = np.asarray(rpm, dtype=float)
    load = np.asarray(load, dtype=float)
    obfcm_fuel = np.asarray(obfcm_fuel, dtype=float)
    fuel_sum = np.asarray(fuel_sum, dtype=float)
    conv_ratio = np.asarray(conv_ratio, dtype=float)
    fuel_density = np.asarray(fuel_density, dtype=float)

    n = len(rpm)
    for name, arr in (("load", load), ("obfcm_fuel", obfcm_fuel), ("fuel_sum", fuel_sum),
                      ("conv_ratio", conv_ratio), ("fuel_density", fuel_density)):
        if len(arr) != n:
            raise ValueError(f"信号长度不一致：rpm={n}, {name}={len(arr)}")
    if n == 0:
        raise ValueError("没有可分析的采样点")

    # base_uproml = conv_ratio*1000 / density; theoretical fuel = sum*base/1000.
    with np.errstate(divide="ignore", invalid="ignore"):
        base_uproml = (conv_ratio * 1000.0) / fuel_density
        denom = fuel_sum * base_uproml
        k = np.where((denom != 0) & np.isfinite(denom), (obfcm_fuel * 1000.0) / denom, np.nan)
        # K is only meaningful for genuine operating points.
        k = np.where((fuel_sum > 0) & (base_uproml > 0) & (obfcm_fuel > 0), k, np.nan)

    result: dict[str, Any] = {
        "samples": n,
        "k_factor": _stats(k),
        "k_matrix": _k_matrix(rpm, load, k, rpm_intervals, load_intervals),
        "rpm_intervals": list(rpm_intervals),
        "load_intervals": list(load_intervals),
        "obfcm_accuracy": None,
        "reference_fuel_available": False,
    }

    if thc_s is not None and co_s is not None and co2_s is not None:
        thc, co, co2 = (np.asarray(x, dtype=float) for x in (thc_s, co_s, co2_s))
        with np.errstate(divide="ignore", invalid="ignore"):
            fc_l_s = _fc_l_s(thc, co, co2)
            valid = (fc_l_s > 0) & (obfcm_fuel > 0)
            accuracy = np.where(valid, (fc_l_s - obfcm_fuel) / fc_l_s, np.nan)
        result["reference_fuel_available"] = True
        result["obfcm_accuracy"] = _stats(accuracy)
        # The headline OBFCM metric is mean absolute accuracy (deviation from reference).
        abs_stats = _stats(np.abs(accuracy))
        result["obfcm_abs_accuracy_mean"] = abs_stats["mean"] if abs_stats else None

    return result


def _k_matrix(
    rpm: np.ndarray,
    load: np.ndarray,
    k: np.ndarray,
    rpm_intervals: Sequence[float],
    load_intervals: Sequence[float],
) -> list[list[dict[str, Any] | None]]:
    """Average K per (load row × rpm column) cell. None where no valid sample."""
    rpm_edges = np.asarray(rpm_intervals, dtype=float)
    load_edges = np.asarray(load_intervals, dtype=float)
    n_rpm = len(rpm_edges) - 1
    n_load = len(load_edges) - 1

    # digitize → 1..len-1 inside the range; 0 / len fall outside and are dropped.
    rpm_bin = np.digitize(rpm, rpm_edges)
    load_bin = np.digitize(load, load_edges)

    matrix: list[list[dict[str, Any] | None]] = []
    for li in range(1, n_load + 1):
        row: list[dict[str, Any] | None] = []
        for ri in range(1, n_rpm + 1):
            cell = k[(rpm_bin == ri) & (load_bin == li)]
            cell = cell[np.isfinite(cell)]
            if cell.size == 0:
                row.append(None)
            else:
                row.append({
                    "k_avg": float(np.mean(cell)),
                    "k_std": float(np.std(cell)),
                    "count": int(cell.size),
                })
        matrix.append(row)
    return matrix


if __name__ == "__main__":
    # Self-check: a constant operating point with known K, plus a reference fuel.
    # base_uproml = 0.00037*1000/740 = 0.0005; denom = sum*base = 1000*0.0005 = 0.5.
    # K = obfcm_fuel*1000/denom, so obfcm_fuel=0.0005 → K=1.0.
    out = compute_obfcm(
        rpm=[3000, 3000, 3000],
        load=[95, 95, 95],
        obfcm_fuel=[0.0005, 0.0005, 0.0005],
        fuel_sum=[1000, 1000, 1000],
        conv_ratio=[0.00037, 0.00037, 0.00037],
        fuel_density=[740, 740, 740],
        thc_s=[0, 0, 0], co_s=[0, 0, 0], co2_s=[100, 100, 100],
    )
    assert abs(out["k_factor"]["mean"] - 1.0) < 1e-9, out["k_factor"]
    # rpm 3000 → 3rd rpm bin (2500-4000); load 95 → 3rd load bin (80-110).
    cell = out["k_matrix"][2][2]
    assert cell is not None and abs(cell["k_avg"] - 1.0) < 1e-9, out["k_matrix"]
    # FC_L_S = (0.0012/0.75)*0.273*100 = 0.04368; accuracy = (0.04368-0.5)/0.04368.
    assert out["reference_fuel_available"] and out["obfcm_accuracy"] is not None
    assert out["k_matrix"][0][0] is None  # no samples in the low-rpm/low-load cell
    print("obfcm self-check ok:", out["k_factor"], out["obfcm_accuracy"])
