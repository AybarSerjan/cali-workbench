from __future__ import annotations

import bisect
import hashlib
import json
import re
import struct
from collections import defaultdict
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.config import ROOT
from backend.models import (
    CalibrationCommit,
    CalibrationSession,
    Dataset,
    DatasetFile,
    ParameterChange,
    SourceFile,
    WikiArticle,
    WikiRevision,
)
from backend.services.llm import chat_sync


# A2L/HEX decode tables. Ported from A2L_HEX_DCM_Analysis/hex_compare.py so the
# calibration tool can decode every CHARACTERISTIC type (VALUE/CURVE/MAP/
# VAL_BLK/ASCII), not just scalars. All HEX values are little-endian.
DTYPE_SIZES: dict[str, int] = {
    "UBYTE": 1, "SBYTE": 1, "BYTE": 1,
    "UWORD": 2, "SWORD": 2, "WORD": 2,
    "ULONG": 4, "SLONG": 4, "LONG": 4,
    "A_UINT64": 8, "A_INT64": 8,
    "FLOAT32_IEEE": 4, "FLOAT64_IEEE": 8,
}

_STRUCT_FMT: dict[str, str] = {
    "UBYTE": "<B", "BYTE": "<B", "SBYTE": "<b",
    "UWORD": "<H", "WORD": "<H", "SWORD": "<h",
    "ULONG": "<I", "LONG": "<I", "SLONG": "<i",
    "A_UINT64": "<Q", "A_INT64": "<q",
    "FLOAT32_IEEE": "<f", "FLOAT64_IEEE": "<d",
}

# Friendly names for the ParameterChange.data_type column.
_FRIENDLY: dict[str, str] = {
    "UBYTE": "uint8", "SBYTE": "int8", "BYTE": "uint8",
    "UWORD": "uint16", "SWORD": "int16", "WORD": "uint16",
    "ULONG": "uint32", "SLONG": "int32", "LONG": "uint32",
    "A_UINT64": "uint64", "A_INT64": "int64",
    "FLOAT32_IEEE": "float32", "FLOAT64_IEEE": "float64",
}

_MAX_DETAIL_CHANGES = 200  # cap changed-cell list per characteristic


@dataclass
class RecordLayoutInfo:
    name: str
    fnc_values_type: str = ""
    fnc_values_pos: int = 0
    fnc_values_dir: str = "COLUMN_DIR"
    has_no_axis_x: bool = False
    no_axis_x_type: str = ""
    has_no_axis_y: bool = False
    no_axis_y_type: str = ""
    has_axis_x: bool = False
    axis_x_type: str = ""
    has_axis_y: bool = False
    axis_y_type: str = ""


@dataclass
class CompuMethodInfo:
    name: str
    conv_type: str = "IDENTICAL"  # LINEAR, RAT_FUNC, IDENTICAL, TAB_VERB, …
    coeffs: tuple[float, ...] = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)  # RAT_FUNC a..f
    linear: tuple[float, float] | None = None  # (slope, offset) from COEFFS_LINEAR
    unit: str = ""


@dataclass
class AxisInfo:
    axis_type: str  # STD_AXIS, COM_AXIS, FIX_AXIS, CURVE_AXIS, RES_AXIS
    max_points: int = 0
    compu_method: str = ""


@dataclass
class CharInfo:
    name: str
    char_type: str  # VALUE, CURVE, MAP, VAL_BLK, ASCII, CUBOID
    address: int
    record_layout: str
    compu_method: str = ""
    description: str = ""
    module: str = "Unassigned"
    unit: str = ""
    range: str = ""
    x_axis: AxisInfo | None = None
    y_axis: AxisInfo | None = None
    matrix_dim: list[int] | None = None
    number: int | None = None
    byte_size: int = 0


def _source_path(source: SourceFile) -> Path:
    path = (ROOT / source.relative_path).resolve()
    if not path.is_file() or ROOT.resolve() not in path.parents:
        raise ValueError(f"Source file is unavailable: {source.original_name}")
    return path


def _dataset_file_path(source: DatasetFile) -> Path:
    path = (ROOT / source.relative_path).resolve()
    if not path.is_file() or ROOT.resolve() not in path.parents:
        raise ValueError(f"Dataset file is unavailable: {source.original_name}")
    return path


def _require_source(session: Session, source_id: str, suffix: str) -> SourceFile:
    source = session.get(SourceFile, source_id)
    if not source:
        raise ValueError(f"Source file does not exist: {source_id}")
    if source.suffix.lower() != suffix:
        raise ValueError(f"Expected {suffix} file: {source.original_name}")
    _source_path(source)
    return source


def create_session(
    db: Session, a2l_file_id: str, hex_file_ids: list[str], created_by: str = "user"
) -> CalibrationSession:
    _require_source(db, a2l_file_id, ".a2l")
    if len(hex_file_ids) != 2 or len(set(hex_file_ids)) != 2:
        raise ValueError("Exactly two different HEX files are required")
    for file_id in hex_file_ids:
        _require_source(db, file_id, ".hex")
    item = CalibrationSession(
        a2l_file_id=a2l_file_id,
        hex_file_ids=json.dumps(hex_file_ids),
        created_by=created_by,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S)


def _module_index(text: str) -> list[tuple[int, int, str]]:
    """Precompute each MODULE's (begin, end, name) span in ONE scan. The old
    _module_for() re-scanned the whole file per CHARACTERISTIC (O(chars × len) —
    the 32s on a 26 MB A2L); with the index, module lookup is a tiny linear pass
    over the handful of modules."""
    spans: list[tuple[int, int, str]] = []
    stack: list[tuple[int, str]] = []
    for m in re.finditer(r"/begin\s+MODULE\s+(\S+)|/end\s+MODULE", text, re.I):
        if m.group(1):
            stack.append((m.start(), m.group(1).strip('"')))
        elif stack:
            begin, name = stack.pop()
            spans.append((begin, m.end(), name))
    spans.sort()
    return spans


def _module_at(spans: list[tuple[int, int, str]], position: int) -> str:
    name = "Unassigned"
    for begin, end, mod in spans:  # innermost wins (nested begins sort later)
        if begin <= position < end:
            name = mod
    return name


def parse_record_layouts(content: str) -> dict[str, RecordLayoutInfo]:
    layouts: dict[str, RecordLayoutInfo] = {}
    for m in re.finditer(r"/begin\s+RECORD_LAYOUT\s+(\S+)(.*?)/end\s+RECORD_LAYOUT", content, re.S):
        name, body = m.group(1), m.group(2)
        rl = RecordLayoutInfo(name=name)
        fv = re.search(r"FNC_VALUES\s+(\d+)\s+(\w+)\s+(\w+)", body)
        if fv:
            rl.fnc_values_pos, rl.fnc_values_type, rl.fnc_values_dir = int(fv.group(1)), fv.group(2), fv.group(3)
        nax = re.search(r"NO_AXIS_PTS_X\s+(\d+)\s+(\w+)", body)
        if nax:
            rl.has_no_axis_x, rl.no_axis_x_type = True, nax.group(2)
        nay = re.search(r"NO_AXIS_PTS_Y\s+(\d+)\s+(\w+)", body)
        if nay:
            rl.has_no_axis_y, rl.no_axis_y_type = True, nay.group(2)
        apx = re.search(r"AXIS_PTS_X\s+(\d+)\s+(\w+)", body)
        if apx:
            rl.has_axis_x, rl.axis_x_type = True, apx.group(2)
        apy = re.search(r"AXIS_PTS_Y\s+(\d+)\s+(\w+)", body)
        if apy:
            rl.has_axis_y, rl.axis_y_type = True, apy.group(2)
        layouts[name] = rl
    return layouts


def parse_compu_methods(content: str) -> dict[str, CompuMethodInfo]:
    methods: dict[str, CompuMethodInfo] = {}
    for m in re.finditer(
        r"/begin\s+COMPU_METHOD\s+(\S+)\s+\"[^\"]*\"\s+(\S+)(.*?)/end\s+COMPU_METHOD", content, re.S
    ):
        name, conv_type, body = m.group(1), m.group(2), m.group(3)
        cm = CompuMethodInfo(name=name, conv_type=conv_type)
        # "%format" "unit" pair that follows the conversion type.
        um = re.search(r"\"[^\"]*\"\s+\"([^\"]*)\"", body)
        if um:
            cm.unit = um.group(1)
        coeffs = re.search(
            r"COEFFS\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)"
            r"\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)",
            body,
        )
        if coeffs:
            cm.coeffs = tuple(float(coeffs.group(i)) for i in range(1, 7))
        linear = re.search(r"COEFFS_LINEAR\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)", body)
        if linear:
            cm.linear = (float(linear.group(1)), float(linear.group(2)))
        methods[name] = cm
    return methods


_A2L_QUOTED = r'"((?:""|[^"])*)"'


_CHAR_HEADER = re.compile(
    r"(\S+)\s+"                                    # name
    + _A2L_QUOTED + r"\s+"                         # description
    r"(VALUE|CURVE|MAP|VAL_BLK|ASCII|CUBOID)\s+"   # type
    r"(0x[0-9A-Fa-f]+)\s+"                         # address
    r"(\S+)\s+"                                    # record_layout
    r"\S+\s+"                                      # maxdiff
    r"(\S+)"                                       # compu_method
    r"(?:\s+(\S+)\s+(\S+))?"                       # lower / upper limit (optional)
)
_AXIS_BLOCK = re.compile(r"/begin\s+AXIS_DESCR\s+(.*?)/end\s+AXIS_DESCR", re.S)
_AXIS_HEADER = re.compile(
    r"(STD_AXIS|COM_AXIS|FIX_AXIS|CURVE_AXIS|RES_AXIS)\s+\S+\s+(\S+)\s+(\d+)"
)


def parse_characteristics(
    content: str, methods: dict[str, CompuMethodInfo], module_spans: list[tuple[int, int, str]] | None = None
) -> dict[str, CharInfo]:
    module_spans = module_spans if module_spans is not None else _module_index(content)
    chars: dict[str, CharInfo] = {}
    for m in re.finditer(r"/begin\s+CHARACTERISTIC\s+(.*?)/end\s+CHARACTERISTIC", content, re.S):
        body = m.group(1).strip()
        hdr = _CHAR_HEADER.match(body)
        if not hdr:
            continue
        ci = CharInfo(
            name=hdr.group(1),
            description=hdr.group(2).replace('""', '"'),
            char_type=hdr.group(3),
            address=int(hdr.group(4), 16),
            record_layout=hdr.group(5),
            compu_method=hdr.group(6),
            module=_module_at(module_spans, m.start()),
        )
        if hdr.group(7) and hdr.group(8):
            ci.range = f"{hdr.group(7)}-{hdr.group(8)}"
        phys = re.search(r"PHYS_UNIT\s+\"([^\"]*)\"", body)
        ci.unit = phys.group(1) if phys else methods.get(ci.compu_method, CompuMethodInfo(name="")).unit
        axes: list[AxisInfo] = []
        for am in _AXIS_BLOCK.finditer(body):
            ah = _AXIS_HEADER.match(am.group(1).strip())
            if ah:
                axes.append(AxisInfo(axis_type=ah.group(1), compu_method=ah.group(2), max_points=int(ah.group(3))))
        if axes:
            ci.x_axis = axes[0]
        if len(axes) >= 2:
            ci.y_axis = axes[1]
        md = re.search(r"MATRIX_DIM\s+([\d\s]+)", body)
        if md:
            ci.matrix_dim = [int(x) for x in md.group(1).split() if x.isdigit()]
        num = re.search(r"\bNUMBER\s+(\d+)", body)
        if num:
            ci.number = int(num.group(1))
        chars[ci.name] = ci
    return chars


@dataclass
class MeasurementInfo:
    name: str
    description: str = ""
    datatype: str = ""
    compu_method: str = ""
    unit: str = ""
    lower: str = ""
    upper: str = ""
    ecu_address: int | None = None


@dataclass
class FunctionInfo:
    name: str
    description: str = ""
    def_characteristics: list[str] | None = None
    ref_characteristics: list[str] | None = None
    in_measurements: list[str] | None = None
    out_measurements: list[str] | None = None
    loc_measurements: list[str] | None = None
    sub_functions: list[str] | None = None


_MEAS_HEADER = re.compile(
    r"(\S+)\s+"        # name
    + _A2L_QUOTED + r"\s+"  # description
    r"(\S+)\s+"        # datatype
    r"(\S+)\s+"        # compu_method
    r"\S+\s+\S+\s+"    # resolution accuracy
    r"(\S+)\s+(\S+)"   # lower / upper limit
)


def parse_measurements(content: str, methods: dict[str, CompuMethodInfo] | None = None) -> dict[str, MeasurementInfo]:
    methods = methods or {}
    out: dict[str, MeasurementInfo] = {}
    for m in re.finditer(r"/begin\s+MEASUREMENT\s+(.*?)/end\s+MEASUREMENT", content, re.S):
        body = m.group(1).strip()
        hdr = _MEAS_HEADER.match(body)
        if not hdr:
            continue
        mi = MeasurementInfo(
            name=hdr.group(1),
            description=hdr.group(2).replace('""', '"'),
            datatype=hdr.group(3),
            compu_method=hdr.group(4),
            lower=hdr.group(5),
            upper=hdr.group(6),
        )
        phys = re.search(r"PHYS_UNIT\s+\"([^\"]*)\"", body)
        mi.unit = phys.group(1) if phys else methods.get(mi.compu_method, CompuMethodInfo(name="")).unit
        addr = re.search(r"ECU_ADDRESS\s+(0x[0-9A-Fa-f]+)", body)
        if addr:
            mi.ecu_address = int(addr.group(1), 16)
        out[mi.name] = mi
    return out


# FUNCTION sub-blocks hold bare identifier lists. Anchoring each /begin to its
# named /end keeps a missing block from swallowing its siblings.
_FUNC_LIST_BLOCKS = (
    ("DEF_CHARACTERISTIC", "def_characteristics"),
    ("REF_CHARACTERISTIC", "ref_characteristics"),
    ("IN_MEASUREMENT", "in_measurements"),
    ("OUT_MEASUREMENT", "out_measurements"),
    ("LOC_MEASUREMENT", "loc_measurements"),
    ("SUB_FUNCTION", "sub_functions"),
)


def parse_functions(content: str) -> dict[str, FunctionInfo]:
    out: dict[str, FunctionInfo] = {}
    pattern = r"/begin\s+FUNCTION\s+(\S+)\s+" + _A2L_QUOTED + r"(.*?)/end\s+FUNCTION\b"
    for m in re.finditer(pattern, content, re.S):
        fi = FunctionInfo(name=m.group(1), description=m.group(2).replace('""', '"'))
        body = m.group(3)
        for block, attr in _FUNC_LIST_BLOCKS:
            sub = re.search(rf"/begin\s+{block}\s+(.*?)/end\s+{block}", body, re.S)
            setattr(fi, attr, sub.group(1).split() if sub else [])
        out[fi.name] = fi
    return out


def parse_byte_order(content: str) -> str:
    """BYTE_ORDER from MOD_COMMON: MSB_LAST (little-endian) / MSB_FIRST (big-endian).
    Empty string when the A2L does not declare one."""
    mc = re.search(r"/begin\s+MOD_COMMON\s+(.*?)/end\s+MOD_COMMON", content, re.S)
    m = re.search(r"BYTE_ORDER\s+(MSB_FIRST|MSB_LAST)", mc.group(1) if mc else content)
    return m.group(1) if m else ""


def _load_a2l(path: Path) -> tuple[dict[str, CharInfo], dict[str, RecordLayoutInfo], dict[str, CompuMethodInfo]]:
    content = path.read_bytes()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    text = _strip_comments(text)
    # The decode tables (_STRUCT_FMT/_read_le_uint) are little-endian; a big-endian
    # A2L would silently decode garbage, so refuse it explicitly.
    if parse_byte_order(text) == "MSB_FIRST":
        raise ValueError("A2L 声明 BYTE_ORDER MSB_FIRST（大端），当前解码器仅支持小端")
    layouts = parse_record_layouts(text)
    methods = parse_compu_methods(text)
    chars = parse_characteristics(text, methods, _module_index(text))
    return chars, layouts, methods


def _parse_intel_hex(path: Path) -> list[tuple[int, bytes]]:
    """Parse Intel HEX into sorted, coalesced ``(start, bytes)`` segments. A
    contiguous 5 MB image stays ~5 MB of bytes instead of the old
    ``{address: byte}`` dict (~80 B per byte → hundreds of MB)."""
    segments: list[tuple[int, bytearray]] = []
    cur_start = -1
    cur = bytearray()
    base = 0
    for number, raw in enumerate(path.read_text(encoding="ascii", errors="strict").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"Invalid Intel HEX record on line {number}")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise ValueError(f"Invalid Intel HEX bytes on line {number}") from exc
        if len(record) < 5 or record[0] + 5 != len(record) or sum(record) & 0xFF:
            raise ValueError(f"Invalid Intel HEX checksum or length on line {number}")
        count, offset, record_type = record[0], int.from_bytes(record[1:3], "big"), record[3]
        data = record[4 : 4 + count]
        if record_type == 0:
            addr = base + offset
            if cur and addr == cur_start + len(cur):
                cur.extend(data)  # extends the current run
            else:
                if cur:
                    segments.append((cur_start, cur))
                cur_start, cur = addr, bytearray(data)
        elif record_type == 2:
            base = int.from_bytes(data, "big") << 4
        elif record_type == 4:
            base = int.from_bytes(data, "big") << 16
        elif record_type == 1:
            break
    if cur:
        segments.append((cur_start, cur))
    segments.sort(key=lambda s: s[0])
    merged: list[tuple[int, bytes]] = []
    for start, data in segments:
        if merged and start < merged[-1][0] + len(merged[-1][1]):
            raise ValueError(f"Overlapping Intel HEX records at address 0x{start:X}")
        if merged and start == merged[-1][0] + len(merged[-1][1]):
            merged[-1] = (merged[-1][0], merged[-1][1] + bytes(data))
        else:
            merged.append((start, bytes(data)))
    return merged


class _HexMemory:
    """Random-access view over coalesced HEX segments. Same ``read``/``get``/
    ``in`` surface the decode engine expects; gaps read back as 0xFF
    (unprogrammed flash). Lookup is a bisect over the (few) segments."""

    def __init__(self, segments: list[tuple[int, bytes]]) -> None:
        self._starts = [s for s, _ in segments]
        self._segs = segments

    def _seg_for(self, address: int) -> tuple[int, bytes] | None:
        i = bisect.bisect_right(self._starts, address) - 1
        if i < 0:
            return None
        start, data = self._segs[i]
        return (start, data) if address < start + len(data) else None

    def read(self, address: int, size: int) -> bytes:
        if size <= 0:
            return b""
        out = bytearray(b"\xff" * size)
        # Copy whatever overlapping segment bytes exist; the rest stays 0xFF.
        i = bisect.bisect_right(self._starts, address + size - 1)
        for start, data in reversed(self._segs[:i]):
            end = start + len(data)
            if end <= address:
                break
            lo, hi = max(address, start), min(address + size, end)
            if lo < hi:
                out[lo - address : hi - address] = data[lo - start : hi - start]
        return bytes(out)

    def get(self, address: int, default: int = 0) -> int:
        seg = self._seg_for(address)
        return seg[1][address - seg[0]] if seg else default

    def __contains__(self, address: int) -> bool:
        return self._seg_for(address) is not None


def _read_le_uint(hex_data: _HexMemory, addr: int, nbytes: int) -> int:
    val = 0
    for i in range(nbytes):
        val |= hex_data.get(addr + i) << (8 * i)
    return val


def _decode_val(data: bytes, offset: int, dtype: str) -> float:
    fmt = _STRUCT_FMT.get(dtype)
    if not fmt:
        return 0
    size = struct.calcsize(fmt)
    if offset + size > len(data):
        return 0
    return struct.unpack_from(fmt, data, offset)[0]


def _apply_compu(raw: float, cm: CompuMethodInfo | None) -> float:
    """Convert a raw HEX value to its physical value via COMPU_METHOD."""
    if cm is None:
        return raw
    if cm.linear is not None:  # COEFFS_LINEAR slope offset -> slope*raw + offset
        slope, offset = cm.linear
        return slope * raw + offset
    if cm.conv_type == "RAT_FUNC":  # physical = (raw*f - c) / b
        a, b, c, d, e, f = cm.coeffs
        return raw if b == 0 else (raw * f - c) / b
    if cm.conv_type == "LINEAR":
        a, b, c, d, e, f = cm.coeffs
        return a + b * raw
    return raw


def _compu_reason(cm: CompuMethodInfo | None) -> str | None:
    """None when we can convert raw→physical correctly; otherwise a human reason.
    Table/verbal/non-linear methods used to silently fall through to raw and emit
    a *wrong* physical value — now we flag them instead of guessing."""
    if cm is None or cm.linear is not None or cm.conv_type in ("IDENTICAL", "LINEAR"):
        return None
    if cm.conv_type == "RAT_FUNC":
        a, b, c, d, e, f = cm.coeffs
        if a == 0 and d == 0 and e == 0 and b != 0:  # reduces to linear (f/b)·raw − c/b
            return None
        return "RAT_FUNC 非线性，未支持精确换算"
    return f"{cm.conv_type} 换算未支持"


def _rv(v: float) -> int | float | None:
    """Round / compact a decoded value for JSON embedding."""
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        r = round(v, 6)
        if r == int(r) and abs(r) < 1e10:
            return int(r)
        return r
    return v


def _display(value: int | float) -> str:
    return f"{value:.12g}" if isinstance(value, float) else str(value)


def _read_axes(hex_data: _HexMemory, ci: CharInfo, rl: RecordLayoutInfo, methods: dict[str, CompuMethodInfo]):
    """Read axis dims/values of a CURVE/MAP. Returns (xd, yd, xa, ya, data_offset)."""
    addr, off = ci.address, 0
    xd = ci.x_axis.max_points if ci.x_axis else 0
    yd = ci.y_axis.max_points if ci.y_axis else 0
    xa: list | None = None
    ya: list | None = None
    if ci.x_axis and ci.x_axis.axis_type == "STD_AXIS":
        if rl.has_no_axis_x:
            csz = DTYPE_SIZES.get(rl.no_axis_x_type, 1)
            xd = min(_read_le_uint(hex_data, addr + off, csz), ci.x_axis.max_points or 256)
            off += csz
        if rl.has_no_axis_y and ci.y_axis:
            csz = DTYPE_SIZES.get(rl.no_axis_y_type, 1)
            yd = min(_read_le_uint(hex_data, addr + off, csz), ci.y_axis.max_points or 256)
            off += csz
        if rl.has_axis_x and xd > 0:
            asz = DTYPE_SIZES.get(rl.axis_x_type, 0)
            if asz > 0:
                raw = hex_data.read(addr + off, xd * asz)
                x_cm = methods.get(ci.x_axis.compu_method)
                xa = [_apply_compu(_decode_val(raw, j * asz, rl.axis_x_type), x_cm) for j in range(xd)]
                off += xd * asz
        if rl.has_axis_y and yd > 0 and ci.y_axis:
            asz = DTYPE_SIZES.get(rl.axis_y_type, 0)
            if asz > 0:
                raw = hex_data.read(addr + off, yd * asz)
                y_cm = methods.get(ci.y_axis.compu_method)
                ya = [_apply_compu(_decode_val(raw, j * asz, rl.axis_y_type), y_cm) for j in range(yd)]
                off += yd * asz
    return xd, yd, xa, ya, off


A2lDefs = tuple[dict[str, "CharInfo"], dict[str, "RecordLayoutInfo"], dict[str, "CompuMethodInfo"]]


def _decode_param(
    hex_data: _HexMemory, ci: CharInfo, rl: RecordLayoutInfo, methods: dict[str, CompuMethodInfo]
) -> dict[str, Any] | None:
    """Decode ONE CHARACTERISTIC from ONE HEX image into physical values. Decoupled
    from diffing so each version is decoded once, against its OWN A2L — reading an
    old HEX through a new A2L's layout (the previous bug) produced wrong values."""
    ft = rl.fnc_values_type
    fs = DTYPE_SIZES.get(ft, 0)
    if fs == 0:
        return None
    cm = methods.get(ci.compu_method)
    reason = _compu_reason(cm)
    conv = (lambda r: r) if reason else (lambda r: _apply_compu(r, cm))  # raw when unsupported, never a wrong physical
    ctype = ci.char_type

    if ctype == "VALUE":
        return {"t": "VALUE", "v": _rv(conv(_decode_val(hex_data.read(ci.address, fs), 0, ft))), "u": reason}

    if ctype == "ASCII":
        size = ci.byte_size or ci.number or (ci.matrix_dim[0] if ci.matrix_dim else 0) or 1
        return {"t": "ASCII", "v": hex_data.read(ci.address, size).decode("ascii", errors="replace").rstrip("\x00\xff")}

    if ctype == "VAL_BLK":
        count = 1
        if ci.matrix_dim:
            for dim in ci.matrix_dim:
                if dim > 0:
                    count *= dim
        else:
            count = ci.number or 1
        raw = hex_data.read(ci.address, count * fs)
        return {"t": "VAL_BLK", "vals": [_rv(conv(_decode_val(raw, i * fs, ft))) for i in range(count)], "u": reason}

    # CURVE / MAP / CUBOID
    xd, yd, xa, ya, off = _read_axes(hex_data, ci, rl, methods)
    xax = [_rv(v) for v in xa] if xa else None
    yax = [_rv(v) for v in ya] if ya else None
    if ctype == "CURVE":
        raw = hex_data.read(ci.address + off, xd * fs)
        vals = [_rv(conv(_decode_val(raw, i * fs, ft))) for i in range(xd)]
        return {"t": "CURVE", "xDim": xd, "yDim": 0, "xAx": xax, "vals": vals, "u": reason}
    col = rl.fnc_values_dir == "COLUMN_DIR"
    raw = hex_data.read(ci.address + off, xd * yd * fs)
    grid = [[_rv(conv(_decode_val(raw, ((c * yd + r) if col else (r * xd + c)) * fs, ft))) for c in range(xd)] for r in range(yd)]
    return {"t": ctype, "xDim": xd, "yDim": yd, "xAx": xax, "yAx": yax, "vals": grid, "u": reason}


def _dim_label(ci: CharInfo, detail: dict[str, Any]) -> str:
    if ci.char_type in ("MAP", "CUBOID"):
        return f"{detail.get('xDim', 0)}×{detail.get('yDim', 0)} {ci.char_type}"
    if ci.char_type == "CURVE":
        return f"{detail.get('xDim', 0)}-pt CURVE"
    return f"{detail.get('total', 0)}-elem VAL_BLK"


def _cell_delta(va: Any, vb: Any) -> int:
    """+1 if b>a, else -1. None/non-numeric counts as a (downward) change."""
    return 1 if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and vb > va else -1


def _diff_cells(pairs: list[tuple[Any, Any, dict]]) -> dict[str, Any]:
    """Shared aggregate diff: pairs of (old, new, index-fields). Returns the
    detail dict (total set by caller)."""
    chg: list[dict] = []
    n_up = n_dn = 0
    for va, vb, where in pairs:
        if va != vb:
            if _cell_delta(va, vb) > 0:
                n_up += 1
            else:
                n_dn += 1
            if len(chg) < _MAX_DETAIL_CHANGES:
                chg.append({**where, "o": va, "n": vb})
    return {"nChg": n_up + n_dn, "nUp": n_up, "nDn": n_dn, "chg": chg}


def _diff_decoded(ci: CharInfo, rl: RecordLayoutInfo, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any] | None:
    """Diff two already-decoded snapshots of the same parameter name."""
    reason = new.get("u") or old.get("u")
    base = {
        "parameter_name": ci.name,
        "module": ci.module,
        "data_type": _FRIENDLY.get(rl.fnc_values_type, rl.fnc_values_type),
        "unit": ci.unit,
        "range": ci.range,
        "a2l_description": ci.description,
        "char_type": ci.char_type,
        "detail": None,
    }
    if reason:
        base["unsupported"] = reason

    if ci.char_type == "VALUE":
        ov, nv = old["v"], new["v"]
        if ov is None or nv is None or ov == nv:
            return None
        ov_d, nv_d = _display(ov), _display(nv)
        if reason:
            base.update(old_value=ov_d, new_value=f"{nv_d}（换算未支持：{reason}）", change_magnitude=None)
        else:
            base.update(old_value=ov_d, new_value=nv_d, change_magnitude=_numeric_difference(ov_d, nv_d))
        return base
    if ci.char_type == "ASCII":
        if old["v"] == new["v"]:
            return None
        base.update(old_value=str(old["v"]), new_value=str(new["v"]), change_magnitude=None)
        return base

    # Aggregate types: compare logical vals element-wise over the overlap.
    if ci.char_type == "VAL_BLK":
        ov, nv = old["vals"], new["vals"]
        n = min(len(ov), len(nv))
        detail = _diff_cells([(ov[i], nv[i], {"i": i}) for i in range(n)])
        detail["total"] = max(len(ov), len(nv))
    elif ci.char_type == "CURVE":
        ov, nv = old["vals"], new["vals"]
        n = min(len(ov), len(nv))
        detail = _diff_cells([(ov[i], nv[i], {"i": i}) for i in range(n)])
        detail.update(total=max(len(ov), len(nv)), xDim=new["xDim"] or old["xDim"], yDim=0,
                      xAx=new.get("xAx") or old.get("xAx"), vals=nv)
    else:  # MAP / CUBOID
        og, ng = old["vals"], new["vals"]
        yn = min(len(og), len(ng))
        pairs = [(og[r][c], ng[r][c], {"r": r, "c": c}) for r in range(yn) for c in range(min(len(og[r]), len(ng[r])))]
        detail = _diff_cells(pairs)
        detail.update(total=max(old["xDim"] * old["yDim"], new["xDim"] * new["yDim"]),
                      xDim=new["xDim"] or old["xDim"], yDim=new["yDim"] or old["yDim"],
                      xAx=new.get("xAx") or old.get("xAx"), yAx=new.get("yAx") or old.get("yAx"), vals=ng)
    detail = {k: v for k, v in detail.items() if v is not None}
    if detail.get("nChg", 0) <= 0:
        return None
    summary = f"{_dim_label(ci, detail)} · {detail['nChg']} 个单元变化 (↑{detail['nUp']} ↓{detail['nDn']})"
    if reason:
        summary += f"（换算未支持：{reason}）"
    base.update(old_value="", new_value=summary, detail=detail, change_magnitude=None)
    return base


def _decode_all(defs: A2lDefs, mem: _HexMemory) -> dict[str, tuple[CharInfo, RecordLayoutInfo, dict[str, Any]]]:
    chars, layouts, methods = defs
    out: dict[str, tuple[CharInfo, RecordLayoutInfo, dict[str, Any]]] = {}
    for ci in chars.values():
        rl = layouts.get(ci.record_layout)
        if not rl or not rl.fnc_values_type:
            continue
        dec = _decode_param(mem, ci, rl, methods)
        if dec is not None:
            out[ci.name] = (ci, rl, dec)
    return out


def _diff_versions(old_decoded: dict[str, tuple], new_decoded: dict[str, tuple]) -> list[dict[str, Any]]:
    """Diff two decoded versions by parameter name. Added/removed params are
    reported separately via A2L label sets; this covers params in both."""
    changes: list[dict[str, Any]] = []
    for name, (ci, rl, new_dec) in new_decoded.items():
        old_entry = old_decoded.get(name)
        if not old_entry:
            continue
        _, _, old_dec = old_entry
        if old_dec["t"] != new_dec["t"]:  # type changed across an A2L revision
            changes.append({
                "parameter_name": ci.name, "module": ci.module,
                "data_type": _FRIENDLY.get(rl.fnc_values_type, rl.fnc_values_type),
                "unit": ci.unit, "range": ci.range, "a2l_description": ci.description,
                "char_type": ci.char_type, "detail": None, "change_magnitude": None,
                "old_value": old_dec["t"], "new_value": f"类型变更 {old_dec['t']} → {new_dec['t']}",
            })
            continue
        change = _diff_decoded(ci, rl, old_dec, new_dec)
        if change:
            changes.append(change)
    return changes


def compare_files(a2l_path: Path, old_hex_path: Path, new_hex_path: Path) -> tuple[bool, list[dict[str, Any]]]:
    """Session path: both HEX read through the same A2L."""
    defs = _load_a2l(a2l_path)
    old_dec = _decode_all(defs, _HexMemory(_parse_intel_hex(old_hex_path)))
    new_dec = _decode_all(defs, _HexMemory(_parse_intel_hex(new_hex_path)))
    return bool(defs[0]), _diff_versions(old_dec, new_dec)


def compare_parsed(old_defs: A2lDefs, old_mem: _HexMemory, new_defs: A2lDefs, new_mem: _HexMemory) -> list[dict[str, Any]]:
    """Evolution path: each side decoded against its own version's A2L."""
    return _diff_versions(_decode_all(old_defs, old_mem), _decode_all(new_defs, new_mem))


def _a2l_label_names(path: Path) -> set[str]:
    """Read only CHARACTERISTIC names for report KPIs; avoid a full A2L decode."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"/begin\s+CHARACTERISTIC\s+([^\s]+)", text, flags=re.I))


def match_a2l_for_hex(db: Session, dataset: Dataset, hex_file_id: str) -> dict[str, Any]:
    """Find the unique A2L whose CHARACTERISTIC addresses overlap a HEX image best."""
    if dataset.kind != "calibration":
        raise ValueError("Only calibration datasets support A2L matching")
    hex_file = db.get(DatasetFile, hex_file_id)
    if not hex_file or hex_file.dataset_id != dataset.id or hex_file.suffix != ".hex":
        raise ValueError("HEX 文件不属于当前数据集或类型不正确")
    memory = _HexMemory(_parse_intel_hex(_dataset_file_path(hex_file)))
    candidates = []
    a2l_files = db.scalars(
        select(DatasetFile).where(DatasetFile.dataset_id == dataset.id, DatasetFile.suffix == ".a2l")
        .order_by(DatasetFile.original_name, DatasetFile.id)
    ).all()
    for a2l in a2l_files:
        try:
            chars = _load_a2l(_dataset_file_path(a2l))[0]
            matched = sum(characteristic.address in memory for characteristic in chars.values())
            total = len(chars)
            candidates.append({
                "id": a2l.id, "name": a2l.original_name, "matched": matched, "total": total,
                "coverage": round(matched / total, 4) if total else 0,
            })
        except Exception as exc:
            candidates.append({"id": a2l.id, "name": a2l.original_name, "matched": 0, "total": 0, "coverage": 0, "error": str(exc)})
    ranked = sorted(candidates, key=lambda item: (item["matched"], item["coverage"]), reverse=True)
    best = ranked[0] if ranked and ranked[0]["matched"] else None
    ties = [item for item in ranked if best and (item["matched"], item["coverage"]) == (best["matched"], best["coverage"])]
    return {
        "status": "missing" if not best else "ambiguous" if len(ties) > 1 else "matched",
        "hex": {"id": hex_file.id, "name": hex_file.original_name},
        "a2l": best if best and len(ties) == 1 else None,
        "candidates": ranked,
    }


def dataset_evolution_payload(db: Session, dataset: Dataset) -> dict[str, Any]:
    """Build an Evolution Report from a calibration dataset's immutable commit chain."""
    if dataset.kind != "calibration":
        raise ValueError("Only calibration datasets have an evolution report")
    commits = list(
        db.scalars(
            select(CalibrationCommit)
            .where(CalibrationCommit.dataset_id == dataset.id)
            .order_by(CalibrationCommit.created_at.asc(), CalibrationCommit.id.asc())
        ).all()
    )
    versions: list[dict[str, Any]] = []
    label_sets: dict[str, set[str]] = {}
    file_cache: dict[str, DatasetFile] = {}
    for index, commit in enumerate(commits, start=1):
        a2l = db.get(DatasetFile, commit.a2l_file_id)
        hex_file = db.get(DatasetFile, commit.hex_file_id)
        if not a2l or not hex_file:
            continue
        file_cache[a2l.id] = a2l
        file_cache[hex_file.id] = hex_file
        if a2l.id not in label_sets:
            label_sets[a2l.id] = _a2l_label_names(_dataset_file_path(a2l))
        versions.append({
            "index": index,
            "id": commit.id,
            "message": commit.message,
            "created_at": commit.created_at,
            "a2l": {"id": a2l.id, "name": a2l.original_name, "label_count": len(label_sets[a2l.id])},
            "hex": {"id": hex_file.id, "name": hex_file.original_name},
        })

    comparisons: list[dict[str, Any]] = []
    anomalies: list[dict[str, str]] = []

    # Parse each distinct A2L / HEX once (keyed by content SHA) and reuse across
    # every comparison — the old code re-parsed both files per pair (O(versions²)
    # parses; the 26 MB A2L alone was seconds each). ponytail: holds every
    # version's HEX in memory for the report; fine for the handful of versions a
    # local dataset has — add SHA-LRU eviction if a dataset ever grows huge.
    a2l_defs_cache: dict[str, A2lDefs] = {}
    hex_mem_cache: dict[str, _HexMemory] = {}

    def defs_for(a2l_file: DatasetFile) -> A2lDefs:
        if a2l_file.sha256 not in a2l_defs_cache:
            a2l_defs_cache[a2l_file.sha256] = _load_a2l(_dataset_file_path(a2l_file))
        return a2l_defs_cache[a2l_file.sha256]

    def mem_for(hex_file: DatasetFile) -> _HexMemory:
        if hex_file.sha256 not in hex_mem_cache:
            hex_mem_cache[hex_file.sha256] = _HexMemory(_parse_intel_hex(_dataset_file_path(hex_file)))
        return hex_mem_cache[hex_file.sha256]

    def add_comparison(old_index: int, new_index: int, mode: str) -> None:
        old, new = versions[old_index], versions[new_index]
        old_a2l = file_cache[old["a2l"]["id"]]
        new_a2l = file_cache[new["a2l"]["id"]]
        old_hex = file_cache[old["hex"]["id"]]
        new_hex = file_cache[new["hex"]["id"]]
        comparison_id = f"{mode}-{old['index']}-{new['index']}"
        try:
            changes = compare_parsed(
                defs_for(old_a2l), mem_for(old_hex), defs_for(new_a2l), mem_for(new_hex)
            )
        except Exception as exc:
            anomalies.append({"id": comparison_id, "comparison": f"V{old['index']} → V{new['index']}", "detail": str(exc)})
            return
        counts: dict[str, int] = defaultdict(int)
        for change in changes:
            counts[change["char_type"]] += 1
        comparisons.append({
            "id": comparison_id,
            "mode": mode,
            "from_version": old["index"],
            "to_version": new["index"],
            "from_hex": old["hex"]["name"],
            "to_hex": new["hex"]["name"],
            "a2l_old": old["a2l"]["name"],
            "a2l_new": new["a2l"]["name"],
            "a2l_updated": old["a2l"]["id"] != new["a2l"]["id"],
            "counts": dict(counts),
            "total": len(changes),
            "changes": changes,
        })

    for index in range(1, len(versions)):
        add_comparison(index - 1, index, "adjacent")
    for index in range(2, len(versions)):
        add_comparison(0, index, "baseline")

    adjacent = [item for item in comparisons if item["mode"] == "adjacent"]
    a2l_updates = []
    for item in adjacent:
        if not item["a2l_updated"]:
            continue
        old_version, new_version = versions[item["from_version"] - 1], versions[item["to_version"] - 1]
        old_labels = label_sets[old_version["a2l"]["id"]]
        new_labels = label_sets[new_version["a2l"]["id"]]
        a2l_updates.append({
            "from_version": item["from_version"],
            "to_version": item["to_version"],
            "old_name": item["a2l_old"],
            "new_name": item["a2l_new"],
            "added": len(new_labels - old_labels),
            "removed": len(old_labels - new_labels),
            "common_changes": item["total"],
        })
    return {
        "dataset": {"id": dataset.id, "name": dataset.name},
        "versions": versions,
        "comparisons": comparisons,
        "a2l_updates": a2l_updates,
        "anomalies": anomalies,
        "latest_label_count": versions[-1]["a2l"]["label_count"] if versions else 0,
        "max_baseline_diff": max((item["total"] for item in comparisons if item["mode"] == "baseline"), default=0),
    }


def compare_session(db: Session, session_id: str) -> CalibrationSession:
    item = db.scalar(
        select(CalibrationSession)
        .where(CalibrationSession.id == session_id)
        .options(selectinload(CalibrationSession.parameter_changes))
    )
    if not item:
        raise ValueError("Calibration session does not exist")
    if item.parameter_changes:
        return item
    a2l = _require_source(db, item.a2l_file_id, ".a2l")
    hex_ids = json.loads(item.hex_file_ids)
    old_hex, new_hex = (_require_source(db, file_id, ".hex") for file_id in hex_ids)
    _, changes = compare_files(_source_path(a2l), _source_path(old_hex), _source_path(new_hex))
    for change in changes:
        db.add(
            ParameterChange(
                session_id=item.id,
                parameter_name=change["parameter_name"],
                module=change["module"],
                data_type=change["data_type"],
                unit=change["unit"],
                range=change["range"],
                a2l_description=change["a2l_description"],
                old_value=change["old_value"],
                new_value=change["new_value"],
                char_type=change["char_type"],
                detail_json=json.dumps(change["detail"], ensure_ascii=False) if change["detail"] else "",
            )
        )
    db.commit()
    db.expire(item, ["parameter_changes"])
    return db.scalar(
        select(CalibrationSession)
        .where(CalibrationSession.id == item.id)
        .options(selectinload(CalibrationSession.parameter_changes))
    )


def _json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("Model did not return JSON")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Model response must be a JSON object")
    return value


def analyze_session(db: Session, session_id: str) -> CalibrationSession:
    item = compare_session(db, session_id)
    changes = list(item.parameter_changes)
    modules = sorted({change.module for change in changes})
    fallback = (
        f"{len(changes)} parameters changed across {len(modules)} modules: {', '.join(modules)}."
        if changes
        else "No parameter value changes were found at A2L-declared scalar addresses."
    )
    facts = [
        {
            "id": change.id,
            "parameter_name": change.parameter_name,
            "module": change.module,
            "data_type": change.data_type,
            "unit": change.unit,
            "range": change.range,
            "a2l_description": change.a2l_description,
            "char_type": change.char_type,
            "old_value": change.old_value,
            "new_value": change.new_value,
        }
        for change in changes
    ]
    try:
        answer = chat_sync(
            [
                {
                    "role": "system",
                    "content": (
                        "Analyze only the supplied calibration facts. Return JSON with session_summary and "
                        "change_summaries keyed by change id. Do not invent intent, causality, dependencies, or "
                        "engineering impact; label any interpretation as uncertain. Keep summaries concise."
                    ),
                },
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
            ],
            reasoning=True,
        )
        parsed = _json_object(answer) if answer else {}
    except Exception:
        parsed = {}
    item.session_summary = str(parsed.get("session_summary") or fallback)[:2000]
    summaries = parsed.get("change_summaries") if isinstance(parsed.get("change_summaries"), dict) else {}
    for change in changes:
        if change.char_type in ("VALUE", "ASCII"):
            magnitude = _numeric_difference(change.old_value, change.new_value)
            deterministic = f"{change.parameter_name}: {change.old_value} → {change.new_value}"
            if change.unit:
                deterministic += f" {change.unit}"
            if magnitude is not None:
                deterministic += f" (Δ {magnitude:+.12g})"
        else:
            # old_value is empty for tables; new_value already holds the "N cells changed" summary.
            deterministic = f"{change.parameter_name} ({change.new_value})"
        change.change_summary = str(summaries.get(change.id) or deterministic)[:2000]
    db.commit()
    return item


def _numeric_difference(old: str, new: str) -> float | None:
    try:
        return float(new) - float(old)
    except ValueError:
        return None


def change_payload(change: ParameterChange) -> dict[str, Any]:
    return {
        "id": change.id,
        "session_id": change.session_id,
        "parameter_name": change.parameter_name,
        "module": change.module,
        "data_type": change.data_type,
        "unit": change.unit,
        "range": change.range,
        "a2l_description": change.a2l_description,
        "char_type": change.char_type,
        "old_value": change.old_value,
        "new_value": change.new_value,
        "change_magnitude": _numeric_difference(change.old_value, change.new_value),
        "change_summary": change.change_summary,
        "detail": json.loads(change.detail_json) if change.detail_json else None,
        "created_at": change.created_at,
    }


def session_payload(db: Session, item: CalibrationSession, include_changes: bool = True) -> dict[str, Any]:
    a2l = db.get(SourceFile, item.a2l_file_id)
    hex_files = [db.get(SourceFile, file_id) for file_id in json.loads(item.hex_file_ids)]
    payload: dict[str, Any] = {
        "id": item.id,
        "a2l_file": {"id": a2l.id, "name": a2l.original_name} if a2l else None,
        "hex_files": [{"id": source.id, "name": source.original_name} for source in hex_files if source],
        "session_summary": item.session_summary,
        "created_at": item.created_at,
        "created_by": item.created_by,
        "change_count": len(item.parameter_changes),
    }
    if include_changes:
        payload["parameter_changes"] = [change_payload(change) for change in item.parameter_changes]
    return payload


def list_sessions(db: Session, offset: int, limit: int, sort: str) -> list[CalibrationSession]:
    order = CalibrationSession.created_at.asc() if sort == "asc" else CalibrationSession.created_at.desc()
    return list(
        db.scalars(
            select(CalibrationSession)
            .options(selectinload(CalibrationSession.parameter_changes))
            .order_by(order)
            .offset(offset)
            .limit(limit)
        ).all()
    )


def get_session(db: Session, session_id: str) -> CalibrationSession | None:
    return db.scalar(
        select(CalibrationSession)
        .where(CalibrationSession.id == session_id)
        .options(selectinload(CalibrationSession.parameter_changes))
    )


def query_changes(
    db: Session, module: str | None, parameter_name: str | None, offset: int, limit: int
) -> list[ParameterChange]:
    query = select(ParameterChange).order_by(ParameterChange.created_at.desc())
    if module:
        query = query.where(ParameterChange.module == module)
    if parameter_name:
        query = query.where(ParameterChange.parameter_name.ilike(f"%{parameter_name}%"))
    return list(db.scalars(query.offset(offset).limit(limit)).all())


def _draft(db: Session, slug: str, title: str, content: str) -> WikiRevision:
    article = db.scalar(select(WikiArticle).where(WikiArticle.slug == slug))
    if not article:
        article = WikiArticle(slug=slug, title=title)
        db.add(article)
        db.flush()
    number = (db.scalar(select(func.max(WikiRevision.number)).where(WikiRevision.article_id == article.id)) or 0) + 1
    published = db.get(WikiRevision, article.published_revision_id) if article.published_revision_id else None
    revision = WikiRevision(
        article_id=article.id,
        number=number,
        status="draft",
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        base_sha256=published.content_sha256 if published else "",
        review_notes="[]",
        created_by="calibration-agent",
    )
    db.add(revision)
    return revision


def sync_wiki(db: Session) -> list[WikiRevision]:
    sessions = list_sessions(db, 0, 10000, "desc")
    timeline = ["# Calibration timeline", "", "Agent-generated draft. User review and publish are required.", ""]
    by_module: dict[str, list[tuple[CalibrationSession, ParameterChange]]] = defaultdict(list)
    for item in sessions:
        date = item.created_at.astimezone(timezone.utc).date().isoformat()
        timeline.extend([f"## {item.session_summary or 'Calibration comparison'} — {date}", "", f"Session `{item.id}`", ""])
        timeline.extend(["| Parameter | Type | Module | Old | New | Summary |", "|---|---|---|---:|---:|---|"])
        for change in item.parameter_changes:
            module_slug = re.sub(r"[^a-z0-9]+", "-", change.module.lower()).strip("-") or "unassigned"
            timeline.append(
                f"| [{change.parameter_name}](calibration-{module_slug}) | {change.char_type} | {change.module} | "
                f"{change.old_value} | {change.new_value} | {change.change_summary} |"
            )
            by_module[change.module].append((item, change))
        timeline.append("")
    revisions = [_draft(db, "calibration-timeline", "Calibration/Timeline", "\n".join(timeline))]
    for module, entries in sorted(by_module.items()):
        slug_part = re.sub(r"[^a-z0-9]+", "-", module.lower()).strip("-") or "unassigned"
        names = {change.parameter_name for _, change in entries}
        lines = [
            f"# {module}",
            "",
            "Agent-generated draft. User review and publish are required.",
            "",
            "## Parameter overview",
            "",
            f"{len(names)} parameters have recorded changes.",
            "",
            "## Change history (newest first)",
            "",
        ]
        for item, change in entries:
            date = item.created_at.astimezone(timezone.utc).date().isoformat()
            lines.extend(
                [
                    f"### CalibrationSession {item.id} | {date}",
                    f"- {change.parameter_name}: {change.old_value} → {change.new_value} {change.unit}".rstrip(),
                    f"- Summary: {change.change_summary}",
                    "- [View complete change set](calibration-timeline)",
                    "",
                ]
            )
        revisions.append(_draft(db, f"calibration-{slug_part}", f"Calibration/{module.replace('.', '/')}", "\n".join(lines)))
    db.commit()
    return revisions
