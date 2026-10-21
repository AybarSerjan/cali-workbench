from __future__ import annotations

import pytest

from backend.services.calibration import (
    CharInfo,
    CompuMethodInfo,
    RecordLayoutInfo,
    _compu_reason,
    _decode_param,
    _diff_decoded,
    _HexMemory,
    _load_a2l,
    _parse_intel_hex,
    compare_parsed,
)


def _rec(addr: int, data: bytes) -> str:
    payload = bytes([len(data), (addr >> 8) & 0xFF, addr & 0xFF, 0]) + data
    return ":" + (payload + bytes([(-sum(payload)) & 0xFF])).hex().upper()


def _write_hex(path, *chunks: tuple[int, bytes]) -> None:
    path.write_text("\n".join(_rec(a, d) for a, d in chunks) + "\n:00000001FF\n", encoding="ascii")


def test_hex_memory_segments_and_holes():
    mem = _HexMemory([(0x10, b"\x01\x02\x03"), (0x20, b"\xaa\xbb")])
    assert mem.read(0x10, 3) == b"\x01\x02\x03"
    assert mem.get(0x21) == 0xBB
    assert mem.read(0x12, 3) == b"\x03\xff\xff"      # runs off the end of a segment into a hole
    assert mem.read(0x1F, 3) == b"\xff\xaa\xbb"      # hole then the next segment
    assert 0x18 not in mem and 0x20 in mem


def test_parse_intel_hex_coalesces_and_rejects_overlap(tmp_path):
    good = tmp_path / "good.hex"
    _write_hex(good, (0x00, b"\x01\x02"), (0x02, b"\x03"), (0x10, b"\xff"))  # first two are contiguous
    segs = _parse_intel_hex(good)
    assert segs == [(0x00, b"\x01\x02\x03"), (0x10, b"\xff")]

    bad = tmp_path / "bad.hex"
    _write_hex(bad, (0x00, b"\x01\x02"), (0x01, b"\x09"))  # overwrites 0x01
    with pytest.raises(ValueError, match="Overlapping"):
        _parse_intel_hex(bad)


_A2L_TEMPLATE = """/begin PROJECT P "P"
  /begin MODULE Engine "Engine"
    /begin RECORD_LAYOUT ScalarU16
      FNC_VALUES 1 UWORD COLUMN_DIR DIRECT
    /end RECORD_LAYOUT
    /begin COMPU_METHOD Ident "id" IDENTICAL "%6.0" "-"
    /end COMPU_METHOD
    /begin CHARACTERISTIC EngineIdleRPM "Idle"
      VALUE {addr} ScalarU16 0 Ident 0 8000
    /end CHARACTERISTIC
  /end MODULE
/end PROJECT
"""


def test_each_version_decoded_with_its_own_a2l(tmp_path):
    # Same parameter lives at a DIFFERENT address after the A2L revision. Reading
    # the old HEX through the new A2L (the old bug) would decode 0x2000 in the old
    # image (a hole -> wrong). compare_parsed must read each side at its own addr.
    old_a2l = tmp_path / "old.a2l"
    new_a2l = tmp_path / "new.a2l"
    old_a2l.write_text(_A2L_TEMPLATE.format(addr="0x1000"), encoding="utf-8")
    new_a2l.write_text(_A2L_TEMPLATE.format(addr="0x2000"), encoding="utf-8")

    old_hex = tmp_path / "old.hex"
    new_hex = tmp_path / "new.hex"
    _write_hex(old_hex, (0x1000, (600).to_bytes(2, "little")))
    _write_hex(new_hex, (0x2000, (650).to_bytes(2, "little")))

    changes = compare_parsed(
        _load_a2l(old_a2l), _HexMemory(_parse_intel_hex(old_hex)),
        _load_a2l(new_a2l), _HexMemory(_parse_intel_hex(new_hex)),
    )
    assert len(changes) == 1
    assert changes[0]["old_value"] == "600" and changes[0]["new_value"] == "650"


def test_unsupported_compu_flags_instead_of_wrong_value():
    assert _compu_reason(CompuMethodInfo(name="m", conv_type="TAB_VERB")) is not None
    assert _compu_reason(CompuMethodInfo(name="m", conv_type="LINEAR")) is None

    rl = RecordLayoutInfo(name="s", fnc_values_type="UWORD")
    methods = {"m": CompuMethodInfo(name="m", conv_type="TAB_VERB")}
    ci = CharInfo(name="P", char_type="VALUE", address=0x0, record_layout="s", compu_method="m")
    old = _decode_param(_HexMemory([(0x0, (600).to_bytes(2, "little"))]), ci, rl, methods)
    new = _decode_param(_HexMemory([(0x0, (650).to_bytes(2, "little"))]), ci, rl, methods)
    assert old["u"] and old["v"] == 600  # raw kept, not run through a bogus conversion
    change = _diff_decoded(ci, rl, old, new)
    assert change["unsupported"] and "换算未支持" in change["new_value"]
