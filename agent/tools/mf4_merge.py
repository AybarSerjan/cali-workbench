# -*- coding: utf-8 -*-
"""
MF4 Merger (MDA-safe)
=====================
Signal-pair alignment and MF4 merging for independent recording clocks.
The alignment stage estimates a time offset before stacking measurements.

# ponytail: 领域调好的车速互相关时间对齐——台架/dyno(A) 与 ECU(B) 用不同时钟
# 录制，靠车速信号对 (DEFAULT_SIGNAL_PAIRS) 做 FFT 互相关估计 lag，再
# MDF.stack(sync=True)。不要用 MDF.concatenate 替代，也不要简化互相关逻辑。
# 相对原文件只去掉了 CLI/argparse、Heartbeat 线程与 tqdm 进度。

- 对齐方式：根据优先候选通道做互相关估计滞后，仅**平移时间戳**写出（不插零）。
- 兼容策略：
  * 默认 **version=4.10**，每个通道**独立 Channel Group**。
  * 字符串通道默认 **丢弃**。
  * 避免设置字符串 encoding 属性，避免共享 timebase。

需要：asammdf>=7.4, numpy；可选 scipy（信号对匹配的最佳路径）
"""
from __future__ import annotations

import gc
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np

from agent.tools.datasets import CapabilityUnavailable

try:
    from scipy import signal as sp_signal
    _SCIPY_OK = True
except ImportError:
    sp_signal = None  # type: ignore
    _SCIPY_OK = False

try:
    from asammdf import MDF, Signal  # type: ignore
    _ASAMMDF_OK = True
except ImportError:
    MDF = Signal = None  # type: ignore
    _ASAMMDF_OK = False


logger = logging.getLogger("mf4_merger")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
# 抑制 asammdf 内部重复名等噪声日志
try:
    logging.getLogger("asammdf").setLevel(logging.CRITICAL)
except Exception:
    pass


class _Stage:
    """轻量阶段日志（替换原 Heartbeat 线程，仅打一条开始日志）。"""

    def __init__(self, label: str, **_: object) -> None:
        self.label = label

    def __enter__(self) -> "_Stage":
        logger.info("▶ %s…", self.label)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc:
            logger.error("✖ %s 失败: %s", self.label, exc)
        return False


def iter_progress(iterable: Iterable, desc: str, enable: bool) -> Iterable:
    return iterable


def _finite_mask(*arrs: np.ndarray) -> np.ndarray:
    mask = np.ones_like(arrs[0], dtype=bool)
    for a in arrs:
        mask &= np.isfinite(a)
    return mask


def _unique_preserve_order(names: Iterable[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for n in names:
        if n not in seen:
            out.append(n)
            seen.add(n)
    return out


# 默认信号对：车速优先 (A侧台架/dyno信号, B侧ECU信号)。支持 fuzzy（子串）匹配。
DEFAULT_SIGNAL_PAIRS: List[Tuple[str, str]] = [
    ("TSPEED", "VehV_v"),
    ("TSPEED", "vfzf_w"),
    ("TSPEED", "ABS_ESP_1_VehicleSpeedVSOSig"),
    ("TSPEED", "Vehicle_Speed"),
    ("TSPEED", "VehicleSpeed"),
    ("TSPEED", "Speed"),
    ("车速", "VehV_v"),
    ("车速", "vfzf_w"),
    ("车速", "VehicleSpeed"),
    ("车速", "Vehicle_Speed"),
    ("SPEED", "VehV_v"),
    ("SPEED", "vfzf_w"),
    ("SPEED", "VehicleSpeed"),
    ("DACtrl_ActualSpeed", "VehV_v"),
    ("DACtrl_ActualSpeed", "ABS_ESP_1_VehicleSpeedVSOSig"),
    ("DACtrl_ActualSpeed", "Vehicle_Speed"),
    ("DACtrl_ActualSpeed", "VehicleSpeed"),
    ("DACtrl_ActualSpeed", "Speed"),
]


@dataclass
class XCorrResult:
    lag_seconds: float
    peak_corr: float
    used_fs: float
    grid_start: float
    grid_end: float
    signal_a: str = ""
    signal_b: str = ""


class MF4Merger:
    """按时间平移合并两个 MF4（MDA 兼容）。"""

    def __init__(
        self,
        prefer_names: Sequence[str] = (
            "TSPEED",
            "VehV_v",
            "ABS_ESP_1_VehicleSpeedVSOSig",
            "Vehicle_Speed",
            "Speed",
            "DACtrl_ActualSpeed",
        ),
        resample_hz: float = 10.0,
        max_lag_s: float = 200.0,
        suffix_b: str = "_B",
        progress: bool = False,
        precompress: int = 0,
        out_version: str = "4.10",
        out_compression: int = 2,  # 0: none, 1: deflate, 2: transpose+deflate(慎用)
        exclude_prefixes: Sequence[str] = ("$",),
        write_float32: bool = False,
        strings_mode: str = "drop",  # 'drop' or 'bytes'
        keep_plain_strings: bool = False,
        force_solo_groups: bool = False,
        max_str_bytes: int = 32,
        normalize_start: bool = True,
        signal_pairs: Optional[Sequence[Tuple[str, str]]] = None,
        merge_method: str = "stack",  # "stack"(workflow风格) or "manual"(逐信号手动)
    ) -> None:
        self.prefer_names = tuple(prefer_names)
        self.resample_hz = float(resample_hz)
        self.max_lag_s = float(max_lag_s)
        self.suffix_b = str(suffix_b)
        self.progress = bool(progress)
        self.precompress = int(precompress)
        self.out_version = out_version
        self.out_compression = int(out_compression)
        self.exclude_prefixes = tuple(exclude_prefixes)
        self.write_float32 = bool(write_float32)
        self.strings_mode = strings_mode.lower().strip()
        assert self.strings_mode in {"drop", "bytes"}
        self.keep_plain_strings = bool(keep_plain_strings)
        self.force_solo_groups = bool(force_solo_groups)
        self.max_str_bytes = max(1, int(max_str_bytes))
        self.normalize_start = bool(normalize_start)
        self._custom_signal_pairs = signal_pairs is not None
        self.signal_pairs = list(signal_pairs) if signal_pairs is not None else list(DEFAULT_SIGNAL_PAIRS)
        self.merge_method = merge_method.lower().strip()
        assert self.merge_method in {"stack", "manual"}

    # ------------------------
    # public API
    # ------------------------
    def merge_files(
        self,
        file_a_path: str,
        file_b_path: str,
        output_dir: Optional[str] = None,
        report_path: Optional[str] = None,
    ) -> str:
        if not _ASAMMDF_OK:
            raise CapabilityUnavailable("未安装 asammdf，无法合并 MF4；请先安装依赖")
        if not os.path.exists(file_a_path) or not os.path.exists(file_b_path):
            raise FileNotFoundError("输入文件不存在")

        tmp: Optional[tempfile.TemporaryDirectory] = None

        with _Stage("加载 MF4"):
            a = MDF(file_a_path)
            b = MDF(file_b_path)

        try:
            # 可选预压缩（减少 I/O），不会改变内容
            if self.precompress in (1, 2):
                with _Stage("预压缩输入"):
                    tmp = tempfile.TemporaryDirectory(prefix="mf4_pre_")
                    a_c = Path(tmp.name) / (Path(file_a_path).stem + "__pre.mf4")
                    b_c = Path(tmp.name) / (Path(file_b_path).stem + "__pre.mf4")
                    a.save(str(a_c), overwrite=True, compression=self.precompress)
                    b.save(str(b_c), overwrite=True, compression=self.precompress)
                    a.close(); b.close()
                    a = MDF(str(a_c))
                    b = MDF(str(b_c))

            # ——— 信号对搜索（workflow 风格，优先尝试） ———
            xres = None
            name_a = name_b = ""
            if self.signal_pairs and _SCIPY_OK:
                with _Stage("搜索最佳信号对 (workflow)"):
                    logger.info("候选通道数=A:%d, B:%d", len(self._channels(a)), len(self._channels(b)))
                    xres = self._find_best_signal_pair(a, b)
                    if xres:
                        name_a = xres.signal_a
                        name_b = xres.signal_b
                        logger.info("信号对匹配成功: (%s, %s), lag=%.3fs, corr=%.4f",
                                    name_a, name_b, xres.lag_seconds, xres.peak_corr)

            # ——— 回退：独立锚点 + 旧互相关 ———
            if xres is None:
                if self._custom_signal_pairs and self.signal_pairs:
                    raise ValueError("配置的信号对无法完成时间对齐")
                logger.info("信号对匹配未命中或 scipy 不可用，回退到独立锚点模式")
                with _Stage("选择锚点通道"):
                    logger.info("候选通道数=A:%d 示例=%s", len(self._channels(a)), self._channels(a)[:7])
                    logger.info("候选通道数=B:%d 示例=%s", len(self._channels(b)), self._channels(b)[:10])
                    name_a = self._pick_anchor_name(a)
                    name_b = self._pick_anchor_name(b)
                    logger.info("锚点(A): %s | 锚点(B): %s", name_a, name_b)

                with _Stage("估计时间偏移 (互相关)"):
                    ta, ya = self._extract_numeric_signal(a, name_a)
                    tb, yb = self._extract_numeric_signal(b, name_b)
                    xres = self._xcorr_lag(ta, ya, tb, yb, fs=self.resample_hz, max_lag=self.max_lag_s)

            logger.info("最终估计 lag=%.3f s (峰相关=%.4f, 信号对=%s↔%s)",
                        xres.lag_seconds, xres.peak_corr, name_a, name_b)

            # ——— 根据 merge_method 选择合并路径 ———
            if self.merge_method == "stack":
                # Workflow 风格：MDF.stack(sync=True) + header.start_time 调整
                with _Stage("Stack 合并 (workflow)"):
                    out = self._merge_stack(a, b, xres.lag_seconds)
            else:
                # 旧的手动合并：逐信号平移时间戳
                if xres.lag_seconds >= 0:
                    shift_a, shift_b = 0.0, -xres.lag_seconds
                else:
                    shift_a, shift_b = abs(xres.lag_seconds), 0.0
                logger.info("对齐策略：A shift=%.3f, B shift=%.3f", shift_a, shift_b)

                global_offset = 0.0
                if self.normalize_start:
                    ta_fb, _ = self._extract_numeric_signal(a, name_a)
                    tb_fb, _ = self._extract_numeric_signal(b, name_b)
                    t_min = min(float(ta_fb[0]) + shift_a, float(tb_fb[0]) + shift_b)
                    if t_min != 0:
                        global_offset = -t_min
                        logger.info("全局时间归一：写入时统一再平移 %+f s 使最早时间=0", global_offset)

                with _Stage("合并通道 (manual)"):
                    out = self._merge(a, b, shift_a=shift_a, shift_b=shift_b, global_offset=global_offset)

            # 保存
            out_dir = Path(output_dir) if output_dir else Path(file_a_path).parent
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{Path(file_a_path).stem}__MERGED__.mf4"

            with _Stage("保存 MF4"):
                if self.strings_mode != "drop" and self.out_compression > 1:
                    logger.warning("存在字符串且请求了 transpose 压缩(2)。为兼容 MDA 建议使用 0 或 1。")
                out.save(str(out_path), overwrite=True, compression=self.out_compression)
                out.close()
                logger.info("文件已保存: %s", out_path)

            # Workflow 风格验证：从磁盘重新打开验证信号数 + 文件大小
            with _Stage("验证输出文件"):
                size_mb = out_path.stat().st_size / (1024 * 1024)
                with MDF(str(out_path)) as verify:
                    signal_count = len(verify.channels_db)
                logger.info("✅ 合并完成: %.2f MB, %d 个信号, version=%s, compression=%d, method=%s",
                            size_mb, signal_count, self.out_version, self.out_compression, self.merge_method)

            # 报告
            if report_path:
                data = {
                    "file_a": os.path.abspath(file_a_path),
                    "file_b": os.path.abspath(file_b_path),
                    "anchor_a": name_a,
                    "anchor_b": name_b,
                    "merge_method": self.merge_method,
                    "signal_pairs_used": bool(xres.signal_a),
                    "estimate": {
                        "lag_seconds": xres.lag_seconds,
                        "peak_corr": xres.peak_corr,
                        "resample_hz": xres.used_fs,
                        "grid": [xres.grid_start, xres.grid_end],
                    },
                    "output": {
                        "version": self.out_version,
                        "compression": self.out_compression,
                        "size_mb": round(size_mb, 2),
                        "signal_count": signal_count,
                    },
                    "strings_mode": self.strings_mode,
                    "max_str_bytes": self.max_str_bytes,
                }
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                logger.info("已写报告：%s", report_path)

            return str(out_path)
        finally:
            for m in (locals().get("a"), locals().get("b")):
                try:
                    if m is not None:
                        m.close()
                except Exception:
                    pass
            if tmp is not None:
                try:
                    tmp.cleanup()
                except Exception:
                    pass

    # ------------------------
    # internals
    # ------------------------
    def _channels(self, mdf: "MDF") -> List[str]:
        """去重后的通道名，排除内部 time 和配置的前缀。"""
        try:
            names = list(mdf.channels_db.keys())  # type: ignore[attr-defined]
        except Exception:
            names = []
        out: List[str] = []
        for n in names:
            s = str(n).strip()
            low = s.lower()
            if low == "time":
                continue
            if any(s.startswith(p) for p in self.exclude_prefixes):
                continue
            out.append(s)
        return _unique_preserve_order(out)

    def _pick_anchor_name(self, mdf: "MDF") -> str:
        names = self._channels(mdf)
        if not names:
            raise ValueError("文件不含可枚举的通道")
        lower_map = {n.lower(): n for n in names}
        for p in self.prefer_names:
            if p.lower() in lower_map:
                return lower_map[p.lower()]
        for p in self.prefer_names:
            for n in names:
                if p.lower() in n.lower():
                    return n
        # 退化：挑选方差最大且数值型的通道
        best_name = None
        best_var = -1.0
        for n in iter_progress(names, "扫描通道(选锚点)", self.progress):
            try:
                s = self._safe_get(mdf, n)
                y = np.asarray(s.samples)
                if y.size > 5 and np.issubdtype(y.dtype, np.number):
                    v = float(np.nanvar(y))
                    if np.isfinite(v) and v > best_var:
                        best_var = v
                        best_name = n
            except Exception:
                continue
        if not best_name:
            raise ValueError("无法找到可用锚点通道（缺少数值型信号）")
        logger.warning("未命中优先名，改用方差最大：%s", best_name)
        return best_name

    def _find_best_signal_pair(self, a: "MDF", b: "MDF") -> Optional[XCorrResult]:
        """尝试所有信号对，找到最佳互相关匹配（workflow 风格）。

        使用完整时间范围 + scipy FFT 互相关，支持大偏移检测。
        """
        if not _SCIPY_OK or sp_signal is None:
            return None

        channels_a = self._channels(a)
        channels_b = self._channels(b)
        lower_map_a = {n.lower(): n for n in channels_a}
        lower_map_b = {n.lower(): n for n in channels_b}

        best: Optional[XCorrResult] = None

        for sig_a_pat, sig_b_pat in self.signal_pairs:
            # 精确匹配 → 模糊匹配
            actual_a = lower_map_a.get(sig_a_pat.lower())
            if not actual_a:
                for n in channels_a:
                    if sig_a_pat.lower() in n.lower():
                        actual_a = n
                        break
            if not actual_a:
                continue

            actual_b = lower_map_b.get(sig_b_pat.lower())
            if not actual_b:
                for n in channels_b:
                    if sig_b_pat.lower() in n.lower():
                        actual_b = n
                        break
            if not actual_b:
                continue

            try:
                ta, ya = self._extract_numeric_signal(a, actual_a)
                tb, yb = self._extract_numeric_signal(b, actual_b)

                # 完整时间范围（workflow 风格），而非仅重叠区
                n_points = min(10000, len(ta), len(tb))
                t_start = min(float(ta[0]), float(tb[0]))
                t_end = max(float(ta[-1]), float(tb[-1]))
                if t_end - t_start < 2.0:
                    continue
                common_time = np.linspace(t_start, t_end, num=n_points)

                a_interp = np.interp(common_time, ta, ya)
                b_interp = np.interp(common_time, tb, yb)

                a_std, b_std = float(np.std(a_interp)), float(np.std(b_interp))
                if a_std < 1e-6 or b_std < 1e-6:
                    continue

                a_norm = (a_interp - np.mean(a_interp)) / a_std
                b_norm = (b_interp - np.mean(b_interp)) / b_std

                corr = sp_signal.correlate(a_norm, b_norm, mode="full", method="fft")
                lags = sp_signal.correlation_lags(len(a_norm), len(b_norm), mode="full")

                dt = (t_end - t_start) / (n_points - 1)
                # 合理性掩码：只在两个信号时长一半范围内搜索峰值
                ta_dur = float(ta[-1] - ta[0])
                tb_dur = float(tb[-1] - tb[0])
                max_plausible = min(ta_dur, tb_dur) / 2.0
                plausible_lags = np.abs(lags.astype(float) * dt) <= max_plausible
                abs_corr_masked = np.where(plausible_lags, np.abs(corr), -1.0)
                max_idx = int(np.argmax(abs_corr_masked))
                offset = float(lags[max_idx]) * dt
                corr_value = float(corr[max_idx]) / n_points

                # max_lag 约束
                if abs(offset) > self.max_lag_s:
                    logger.debug("信号对 (%s, %s): 偏移 %.1fs 超出 max_lag，跳过",
                                 actual_a, actual_b, offset)
                    continue

                # 转为 _xcorr_lag 兼容约定（取反）
                lag_seconds = -offset

                logger.info("信号对 (%s, %s): lag=%.1fs, corr=%.3f",
                            actual_a, actual_b, lag_seconds, corr_value)

                if best is None or abs(corr_value) > abs(best.peak_corr):
                    best = XCorrResult(
                        lag_seconds=lag_seconds,
                        peak_corr=corr_value,
                        used_fs=1.0 / dt,
                        grid_start=t_start,
                        grid_end=t_end,
                        signal_a=actual_a,
                        signal_b=actual_b,
                    )
            except Exception as e:
                logger.debug("信号对 (%s, %s) 失败: %s", sig_a_pat, sig_b_pat, e)
                continue

        if best:
            logger.info("✅ 最佳信号对: (%s, %s), lag=%.1fs, corr=%.3f",
                        best.signal_a, best.signal_b, best.lag_seconds, best.peak_corr)
        return best

    def _safe_get(self, mdf: "MDF", name: str, raw: bool = False) -> "Signal":
        """重复名时使用 whereis 定位第一处。支持原始读取模式。"""
        try:
            locs = mdf.whereis(name)
        except Exception:
            locs = []
        if locs:
            g, i = locs[0][0], locs[0][1]
            return mdf.get(name, group=g, index=i, raw=raw)
        return mdf.get(name, raw=raw)

    def _extract_numeric_signal(self, mdf: "MDF", name: str) -> Tuple[np.ndarray, np.ndarray]:
        nm = str(name).strip()
        if nm.lower() == "time" or any(nm.startswith(p) for p in self.exclude_prefixes):
            raise ValueError("锚点不应为 'time' 或以排除前缀开头的通道")
        sig = self._safe_get(mdf, name)
        t = np.array(sig.timestamps, dtype=float, copy=True)
        y = np.array(sig.samples, dtype=float, copy=True)
        mask = _finite_mask(t, y)
        t = t[mask]
        y = y[mask]
        if t.size < 2:
            raise ValueError(f"通道 '{name}' 有效点不足")
        order = np.argsort(t, kind="mergesort")
        t = t[order]
        y = y[order]
        uniq = np.concatenate(([True], np.diff(t) > 0))
        t = t[uniq]
        y = y[uniq]
        return t, y

    def _xcorr_lag(self, ta: np.ndarray, ya: np.ndarray, tb: np.ndarray, yb: np.ndarray, fs: float, max_lag: float) -> XCorrResult:
        t0 = max(float(ta[0]), float(tb[0]))
        t1 = min(float(ta[-1]), float(tb[-1]))
        if t1 - t0 <= max(2.0, 5.0 / fs):
            raise ValueError("两段时间重叠过短，无法对齐")
        dt = 1.0 / max(fs, 1e-9)
        grid = np.arange(t0, t1, dt, dtype=float)
        if grid.size < 10:
            raise ValueError("重采样点数太少")
        ya_i = np.interp(grid, ta, ya)
        yb_i = np.interp(grid, tb, yb)

        def zn(x: np.ndarray) -> np.ndarray:
            x = x - np.nanmean(x)
            std = float(np.nanstd(x)) or 1.0
            return x / std

        a0 = zn(ya_i)
        b0 = zn(yb_i)
        c = np.correlate(a0, b0, mode="full")
        mid = len(c) // 2
        max_shift = int(round(max_lag / dt))
        max_shift = min(max_shift, len(a0) - 2)
        if max_shift < 1:
            raise ValueError("max_lag 设置过小或重叠过短")
        sl = slice(mid - max_shift, mid + max_shift + 1)
        cwin = c[sl]
        k = int(np.argmax(cwin))
        shift = k - max_shift
        lag_seconds = -shift * dt
        peak = float(cwin[k]) / (len(a0))
        return XCorrResult(lag_seconds=lag_seconds, peak_corr=peak, used_fs=fs, grid_start=float(grid[0]), grid_end=float(grid[-1]))

    def _unique_name(self, base: str, used: set, suffix: str) -> str:
        if base not in used:
            used.add(base)
            return base
        k = 1
        while True:
            cand = f"{base}{suffix}{k if k > 1 else ''}"
            if cand not in used:
                used.add(cand)
                return cand
            k += 1

    def _normalize_text_signal(self, y_raw: np.ndarray) -> Tuple[np.ndarray, str]:
        """将任意字符串样本规范化为字节数组，返回 (processed_array, 'S')"""
        kind = y_raw.dtype.kind
        N = self.max_str_bytes

        def padcut_bytes(b: bytes) -> bytes:
            if b is None:
                b = b""
            if len(b) > N:
                return b[:N]
            if len(b) < N:
                return b + b"\x00" * (N - len(b))
            return b

        if kind == 'U':  # Unicode -> latin-1 字节数组（MF4标准要求）
            flat = []
            for item in y_raw.flat:
                s = '' if item is None else str(item)
                b = s.encode('latin-1', errors='replace')
                flat.append(padcut_bytes(b))
            return np.array(flat, dtype=f"S{N}").reshape(y_raw.shape), 'S'

        if kind == 'S':  # bytes -> clamp to N
            flat = [padcut_bytes((x if x is not None else b"")) for x in y_raw.flat]
            return np.array(flat, dtype=f"S{N}").reshape(y_raw.shape), 'S'

        if kind == 'O':  # object -> 字节数组
            flat = []
            for x in y_raw.flat:
                if x is None:
                    b = b""
                elif isinstance(x, (bytes, bytearray)):
                    b = bytes(x)
                else:
                    s = str(x)
                    b = s.encode('latin-1', errors='replace')
                flat.append(padcut_bytes(b))
            return np.array(flat, dtype=f"S{N}").reshape(y_raw.shape), 'S'

        return y_raw, kind

    def _merge_stack(self, a: "MDF", b: "MDF", lag_seconds: float) -> "MDF":
        """Workflow 风格合并：调整 header.start_time + MDF.stack(sync=True)。

        将 A（台架/dyno 端）的 start_time 调整为 B（ECU 端）的 start_time + lag，
        然后 MDF.stack([A, B], sync=True) 完成时间对齐合并。
        """
        b_start = b.header.start_time
        # lag_seconds > 0 表示 A 开始比 B 晚 lag_seconds 秒 → A.start_time = B.start_time + lag
        adjusted_time = b_start + timedelta(seconds=float(lag_seconds))
        a.header.start_time = adjusted_time

        logger.info("Stack 合并: A.start=%s (adjusted), B.start=%s, lag=%.3fs",
                    adjusted_time, b_start, lag_seconds)

        merged = MDF.stack([a, b], sync=True, version=self.out_version)
        logger.info("Stack 合并完成, version=%s", self.out_version)
        return merged

    def _merge(self, a: "MDF", b: "MDF", *, shift_a: float, shift_b: float, global_offset: float) -> "MDF":
        out = MDF(version="4.00")
        names_a = self._channels(a)
        names_b = self._channels(b)
        logger.info("合并文件 A: %d 通道, 文件 B: %d 通道", len(names_a), len(names_b))

        # 名称占用表：先把 A 的原始名预订，避免 B 重名
        used_names: set = set()
        for n in names_a:
            used_names.add(n)

        stats_A = {"numeric": 0, "bool": 0, "string": 0, "skipped": {"empty": 0, "dtype": 0, "append_exc": 0}}
        stats_B = {"numeric": 0, "bool": 0, "string": 0, "skipped": {"empty": 0, "dtype": 0, "append_exc": 0}}

        def _append_side(src: "MDF", names: List[str], shift: float, is_B: bool):
            stats = stats_B if is_B else stats_A
            side_tag = "写入 B" if is_B else "写入 A"
            for n in iter_progress(names, side_tag, self.progress):
                try:
                    s: Signal = self._safe_get(src, n)
                    nm = str(s.name).strip()
                    if nm.lower() == "time" or any(nm.startswith(p) for p in self.exclude_prefixes):
                        continue

                    # 识别是否为枚举信号（带文本类转换）：先读原始信号检查 conversion
                    s_raw: Signal = self._safe_get(src, n, raw=True)
                    is_enum = hasattr(s_raw, "conversion") and s_raw.conversion is not None

                    if is_enum:
                        # 枚举信号：使用原始整型样本+保留转换表
                        try:
                            y_raw = np.array(s_raw.samples, copy=True)
                            if y_raw.size == 0:
                                stats["skipped"]["empty"] += 1
                                continue

                            if is_B and nm in used_names:
                                nm = self._unique_name(nm, used_names, self.suffix_b)
                            else:
                                used_names.add(nm)

                            t = np.array(s_raw.timestamps, dtype=np.float64, copy=True)
                            if shift != 0.0:
                                t += shift
                            if global_offset != 0.0:
                                t += global_offset

                            if t.size > 0:
                                order = np.argsort(t, kind='mergesort')
                                t = t[order]
                                y_raw = y_raw[order]
                                uniq = np.concatenate(([True], np.diff(t) > 0))
                                t = t[uniq]
                                y_raw = y_raw[uniq]
                                if t.size == 0:
                                    stats["skipped"]["empty"] += 1
                                    continue

                            sig = Signal(
                                samples=y_raw,
                                timestamps=t,
                                name=nm,
                                unit=str(s_raw.unit or ""),
                                conversion=s_raw.conversion,  # 保留原转换表
                                comment=str(s_raw.comment or ""),
                            )
                            out.append([sig], common_timebase=self.force_solo_groups is False)
                            stats["numeric"] += 1
                            continue
                        except Exception as ee:
                            logger.debug("枚举信号处理失败 '%s': %s", nm, ee)
                            stats["skipped"]["append_exc"] += 1
                            continue

                    # 非枚举信号
                    y_raw = np.array(s.samples, copy=True)
                    if y_raw.size == 0:
                        stats["skipped"]["empty"] += 1
                        continue

                    if is_B and nm in used_names:
                        nm = self._unique_name(nm, used_names, self.suffix_b)
                    else:
                        used_names.add(nm)

                    t = np.array(s.timestamps, dtype=np.float64, copy=True)
                    if shift != 0.0:
                        t += shift
                    if global_offset != 0.0:
                        t += global_offset

                    if t.size > 0:
                        order = np.argsort(t, kind='mergesort')
                        t = t[order]
                        y_raw = y_raw[order]
                        uniq = np.concatenate(([True], np.diff(t) > 0))
                        t = t[uniq]
                        y_raw = y_raw[uniq]
                        if t.size == 0:
                            stats["skipped"]["empty"] += 1
                            continue

                    kind = y_raw.dtype.kind
                    if np.issubdtype(y_raw.dtype, np.number):
                        y = y_raw.astype(np.float32, copy=False) if (self.write_float32 and y_raw.dtype.kind == 'f') else y_raw
                        stats["numeric"] += 1
                        sig = Signal(samples=y, timestamps=t, name=nm, unit=str(s.unit or ""), comment=str(s.comment or ""))
                        out.append([sig], common_timebase=False)
                    elif kind in ('b',):
                        stats["bool"] += 1
                        sig = Signal(samples=y_raw, timestamps=t, name=nm, unit=str(s.unit or ""), comment=str(s.comment or ""))
                        out.append([sig], common_timebase=False)
                    else:
                        # 真正的文本信号（非枚举），默认丢弃以保 MDA 兼容
                        if not self.keep_plain_strings:
                            stats["skipped"]["dtype"] += 1
                            continue
                        try:
                            if y_raw.dtype.kind == 'S':
                                y = y_raw
                            else:
                                y, result_kind = self._normalize_text_signal(y_raw)
                                if result_kind not in ('S',):
                                    stats["skipped"]["dtype"] += 1
                                    continue
                            sig = Signal(samples=y, timestamps=t, name=nm, unit=str(s.unit or ""), comment=str(s.comment or ""))
                            out.append([sig], common_timebase=False)
                            stats["string"] += 1
                        except Exception as ee:
                            stats["skipped"]["append_exc"] += 1
                            logger.debug("写入纯文本信号失败 '%s': %s", nm, ee)
                            continue

                except Exception as e:
                    stats["skipped"]["append_exc"] += 1
                    logger.debug("跳过信号 '%s': %s", n, str(e))
                    continue

            logger.info(
                "文件 %s 统计：numeric=%d bool=%d string=%d | skipped(empty=%d, dtype=%d, append_exc=%d)" % (
                    "B" if is_B else "A",
                    stats["numeric"], stats["bool"], stats["string"],
                    stats["skipped"]["empty"], stats["skipped"]["dtype"], stats["skipped"]["append_exc"],
                )
            )

        _append_side(a, names_a, shift_a, is_B=False)
        _append_side(b, names_b, shift_b, is_B=True)

        gc.collect()
        return out


def demo() -> None:
    """自检：造两个错开 5s 的 MF4（含同名车速信号 + 各自独有通道），合并后两侧通道都在。"""
    if not _ASAMMDF_OK:
        print("跳过 demo：未安装 asammdf")
        return
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # A: 台架，含 TSPEED + 独有 A_only
        ta = np.arange(0, 60, 0.1)
        speed = 50 + 40 * np.sin(ta / 5.0)
        a = MDF(version="4.10")
        a.append([Signal(speed, ta, name="TSPEED", unit="km/h")], common_timebase=True)
        a.append([Signal(ta * 2, ta, name="A_only", unit="-")], common_timebase=True)
        pa = d / "a.mf4"; a.save(str(pa), overwrite=True); a.close()
        # B: ECU，含 VehicleSpeed（A 的 TSPEED 整体延后 5s）+ 独有 B_only
        tb = np.arange(0, 60, 0.1)
        speed_b = 50 + 40 * np.sin((tb - 5.0) / 5.0)
        b = MDF(version="4.10")
        b.append([Signal(speed_b, tb, name="VehicleSpeed", unit="km/h")], common_timebase=True)
        b.append([Signal(tb * 3, tb, name="B_only", unit="-")], common_timebase=True)
        pb = d / "b.mf4"; b.save(str(pb), overwrite=True); b.close()

        out = MF4Merger().merge_files(str(pa), str(pb), output_dir=str(d))
        assert os.path.exists(out), "合并输出文件不存在"
        with MDF(out) as m:
            names = set(m.channels_db.keys())
        assert "A_only" in names, f"缺 A 侧通道: {names}"
        assert "B_only" in names, f"缺 B 侧通道: {names}"
        print(f"[OK] demo passed: {out} | channels include A_only/B_only")


if __name__ == "__main__":
    demo()
