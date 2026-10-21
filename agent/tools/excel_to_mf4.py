# -*- coding: utf-8 -*-
"""
Excel → MF4 转换器
==================
将一个 .xlsx 工作表转换为 ASAM MDF 4.10 (.mf4)。

约定的表格布局（与界面提示一致）：
- 第 1 行：通道名（每列一个信号）。
- 第 2 行：可选的单位行——当该行非时间列**全部为非数值**时自动识别为单位，
  数据从第 3 行开始；否则视为没有单位行，数据从第 2 行开始。
- 时间轴：优先按列名识别时间列（time/timestamp/t/时间/seconds…），其值作为秒；
  若没有时间列，则用采样率 sample_rate(Hz) 生成 `arange(n)/sample_rate`。
- 非数值列（无法整体转为 float 的列）整列丢弃，仅写出数值通道
  （与 mf4_merge 丢弃字符串通道一致）。

只依赖 openpyxl 读取表格，asammdf/numpy 写出 MF4，均为延迟导入。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent.tools.datasets import CapabilityUnavailable

logger = logging.getLogger("excel_to_mf4")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(_handler)


# 时间列的候选名（小写、去空白后比较）。
TIME_COLUMN_NAMES = {
    "time",
    "timestamp",
    "timestamps",
    "t",
    "时间",
    "时间戳",
    "sec",
    "secs",
    "second",
    "seconds",
}


@dataclass
class ConversionResult:
    """转换结果：输出路径与写出的通道信息。"""

    path: str
    channels: list[str] = field(default_factory=list)
    rows: int = 0
    skipped: list[str] = field(default_factory=list)
    time_source: str = ""  # "column:<name>" 或 "sample_rate"


def _to_float(value: object) -> float:
    """把单元格转成 float；空/非数值返回 NaN。"""
    if value is None:
        return math.nan
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return math.nan
    try:
        return float(text)
    except ValueError:
        return math.nan


class ExcelToMf4:
    """把单个 .xlsx 表格转换为 MF4 文件。"""

    def __init__(self, version: str = "4.10") -> None:
        self.version = version

    def convert_file(
        self,
        xlsx_path: str,
        output_dir: Optional[str] = None,
        out_name: Optional[str] = None,
        sample_rate: float = 1.0,
        units_row: str = "auto",
        sheet: Optional[str] = None,
    ) -> ConversionResult:
        """转换 ``xlsx_path``，把 .mf4 写入 ``output_dir``（默认与源同目录）。

        - ``sample_rate``：无时间列时生成时间轴用的采样率（Hz），必须 > 0。
        - ``units_row``：``"auto"`` 自动识别单位行；``"yes"`` 强制第二行为单位；
          ``"no"`` 强制没有单位行。
        - ``sheet``：工作表名；缺省取活动表。
        """
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - 仅在缺依赖时触发
            raise CapabilityUnavailable("未安装 openpyxl，无法读取 Excel；请先安装依赖") from exc
        try:
            import numpy as np
            from asammdf import MDF, Signal
        except ImportError as exc:  # pragma: no cover - 仅在缺依赖时触发
            raise CapabilityUnavailable("未安装 asammdf，无法写出 MF4；请先安装依赖") from exc

        if not sample_rate or sample_rate <= 0:
            raise ValueError("采样率必须为正数（Hz）")

        source = Path(xlsx_path)
        if source.suffix.lower() != ".xlsx":
            raise ValueError("仅支持 .xlsx 文件")

        workbook = load_workbook(filename=str(source), read_only=True, data_only=True)
        try:
            worksheet = workbook[sheet] if sheet else workbook.active
            if worksheet is None:
                raise ValueError("Excel 中没有可用的工作表")
            rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
        finally:
            workbook.close()

        # 去掉完全为空的尾随行。
        while rows and all(cell is None or str(cell).strip() == "" for cell in rows[-1]):
            rows.pop()
        if not rows:
            raise ValueError("Excel 工作表为空")

        header = [("" if cell is None else str(cell).strip()) for cell in rows[0]]
        if not any(header):
            raise ValueError("第一行缺少通道名")

        column_count = len(header)
        data_start = self._resolve_data_start(rows, header, units_row)
        units = self._resolve_units(rows, header, data_start)

        body = rows[data_start:]
        if not body:
            raise ValueError("没有数据行（表头之后没有内容）")

        # 列优先地把每列读成 float 数组。
        columns: list[list[float]] = [[] for _ in range(column_count)]
        for row in body:
            for index in range(column_count):
                cell = row[index] if index < len(row) else None
                columns[index].append(_to_float(cell))

        arrays = [np.asarray(column, dtype=np.float64) for column in columns]
        row_total = len(body)

        time_index = self._find_time_column(header)
        if time_index is not None and not np.all(np.isnan(arrays[time_index])):
            timestamps = arrays[time_index]
            time_source = f"column:{header[time_index]}"
        else:
            time_index = None
            timestamps = np.arange(row_total, dtype=np.float64) / float(sample_rate)
            time_source = "sample_rate"

        # 丢掉时间为 NaN 的行，保证时间轴有效。
        valid = np.isfinite(timestamps)
        if not np.all(valid):
            timestamps = timestamps[valid]
            arrays = [array[valid] for array in arrays]
            row_total = int(valid.sum())
        if row_total == 0:
            raise ValueError("有效数据行为 0（时间列全部为空）")

        signals = []
        channel_names: list[str] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for index in range(column_count):
            if index == time_index:
                continue
            name = header[index] or f"Channel_{index + 1}"
            samples = arrays[index]
            if not np.any(np.isfinite(samples)):
                skipped.append(name)
                continue
            unique_name = name
            suffix = 2
            while unique_name in seen:
                unique_name = f"{name}_{suffix}"
                suffix += 1
            seen.add(unique_name)
            samples = np.nan_to_num(samples, nan=0.0)
            signals.append(
                Signal(
                    samples=samples,
                    timestamps=timestamps,
                    name=unique_name,
                    unit=units.get(index, ""),
                )
            )
            channel_names.append(unique_name)

        if not signals:
            raise ValueError("没有可写出的数值通道（所有非时间列都不是数值）")

        out_dir = Path(output_dir) if output_dir else source.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = (out_name or source.stem).strip() or source.stem
        if not stem.lower().endswith(".mf4"):
            stem += ".mf4"
        out_path = out_dir / stem

        logger.info("写出 %d 个通道、%d 行到 %s", len(signals), row_total, out_path.name)
        mdf = MDF(version=self.version)
        try:
            mdf.configure(raise_on_multiple_occurrences=False)
            mdf.append(signals, comment="created by CALI excel_to_mf4")
            mdf.save(str(out_path), overwrite=True)
        finally:
            mdf.close()

        return ConversionResult(
            path=str(out_path),
            channels=channel_names,
            rows=row_total,
            skipped=skipped,
            time_source=time_source,
        )

    def _find_time_column(self, header: list[str]) -> Optional[int]:
        for index, name in enumerate(header):
            if name.strip().lower() in TIME_COLUMN_NAMES:
                return index
        return None

    def _resolve_data_start(self, rows: list[list], header: list[str], units_row: str) -> int:
        """返回数据起始行下标（0-based）。第 2 行是单位则为 2，否则为 1。"""
        mode = (units_row or "auto").lower()
        if mode in {"no", "false", "0"}:
            return 1
        if mode in {"yes", "true", "1"}:
            return 2 if len(rows) > 1 else 1
        # auto：当第二行的非时间列**全部非数值**时认定为单位行。
        if len(rows) <= 2:
            return 1
        time_index = self._find_time_column(header)
        second = rows[1]
        non_time_cells = [
            second[index] if index < len(second) else None
            for index in range(len(header))
            if index != time_index and (header[index] != "")
        ]
        has_value = any(cell is not None and str(cell).strip() != "" for cell in non_time_cells)
        all_non_numeric = has_value and all(
            (cell is None or str(cell).strip() == "" or math.isnan(_to_float(cell)))
            for cell in non_time_cells
        )
        return 2 if all_non_numeric else 1

    def _resolve_units(self, rows: list[list], header: list[str], data_start: int) -> dict[int, str]:
        """data_start==2 时把第 2 行作为单位映射，否则空。"""
        if data_start < 2 or len(rows) < 2:
            return {}
        unit_row = rows[1]
        units: dict[int, str] = {}
        for index in range(len(header)):
            cell = unit_row[index] if index < len(unit_row) else None
            text = "" if cell is None else str(cell).strip()
            if text:
                units[index] = text
        return units
