from __future__ import annotations

import io

from backend.services.calibration import (
    parse_byte_order,
    parse_functions,
    parse_measurements,
)

SAMPLE_A2L = """
/begin PROJECT Demo ""
/begin MODULE ECU ""
/begin MOD_COMMON ""
  BYTE_ORDER MSB_LAST
/end MOD_COMMON

/begin COMPU_METHOD CM_Lam "" LINEAR "%6.4" "-" COEFFS 0 0.001 0 0 0 1 /end COMPU_METHOD
/begin COMPU_METHOD CM_rl "" LINEAR "%6.2" "%" COEFFS 0 0.1 0 0 0 1 /end COMPU_METHOD

/begin MEASUREMENT UEGO_rLamS1B1
  "UEGO lambda sensor 1 bank 1"
  UWORD CM_Lam 1 100 0.5 1.5
  ECU_ADDRESS 0x40001000
/end MEASUREMENT
/begin MEASUREMENT rl_w
  "relative load"
  UWORD CM_rl 1 100 0.0 120.0
/end MEASUREMENT
/begin MEASUREMENT rl_w_msg
  "relative load message"
  UWORD CM_rl 1 100 0.0 120.0
/end MEASUREMENT
/begin MEASUREMENT nmot_w
  "engine speed"
  UWORD CM_rl 1 100 0.0 8000.0
/end MEASUREMENT

/begin CHARACTERISTIC KFFWLKH "warmup factor map" MAP 0x50000000 RL_MAP 100 CM_rl 0.0 2.0
/end CHARACTERISTIC

/begin FUNCTION UEGO_LamCalc "UEGO lambda calculation"
  /begin DEF_CHARACTERISTIC UEGO_IntfrCorrn_CUR UEGO_rIp2LamRecprBasFu_CUR /end DEF_CHARACTERISTIC
  /begin IN_MEASUREMENT rl_w_msg /end IN_MEASUREMENT
  /begin OUT_MEASUREMENT UEGO_rLamS1B1 /end OUT_MEASUREMENT
/end FUNCTION
/begin FUNCTION FuelCalc "fuel path"
  /begin DEF_CHARACTERISTIC KFFWLKH /end DEF_CHARACTERISTIC
  /begin OUT_MEASUREMENT rl_w_msg /end OUT_MEASUREMENT
  /begin SUB_FUNCTION FuelSub /end SUB_FUNCTION
/end FUNCTION
/begin FUNCTION ExhMod_RawSotMdl "exhaust model"
  /begin IN_MEASUREMENT UEGO_rLamS1B1 /end IN_MEASUREMENT
/end FUNCTION

/end MODULE
/end PROJECT
"""


def test_parse_measurements_and_functions():
    measurements = parse_measurements(SAMPLE_A2L)
    assert len(measurements) == 4
    lam = measurements["UEGO_rLamS1B1"]
    assert lam.description == "UEGO lambda sensor 1 bank 1"
    assert lam.datatype == "UWORD"
    assert lam.compu_method == "CM_Lam"
    assert lam.ecu_address == 0x40001000
    assert measurements["rl_w"].ecu_address is None

    functions = parse_functions(SAMPLE_A2L)
    assert len(functions) == 3
    lam_calc = functions["UEGO_LamCalc"]
    assert lam_calc.out_measurements == ["UEGO_rLamS1B1"]
    assert lam_calc.in_measurements == ["rl_w_msg"]
    assert lam_calc.def_characteristics == ["UEGO_IntfrCorrn_CUR", "UEGO_rIp2LamRecprBasFu_CUR"]
    assert functions["FuelCalc"].sub_functions == ["FuelSub"]
    assert functions["ExhMod_RawSotMdl"].def_characteristics == []

    quoted = SAMPLE_A2L.replace('"UEGO lambda sensor 1 bank 1"', '"UEGO ""lambda"" sensor 1 bank 1"')
    assert parse_measurements(quoted)["UEGO_rLamS1B1"].description == 'UEGO "lambda" sensor 1 bank 1'


def test_parse_byte_order():
    assert parse_byte_order(SAMPLE_A2L) == "MSB_LAST"
    assert parse_byte_order("no byte order here") == ""
    assert parse_byte_order("/begin MOD_COMMON \"\" BYTE_ORDER MSB_FIRST /end MOD_COMMON") == "MSB_FIRST"


def _upload_a2l(client, name="demo.a2l", content=SAMPLE_A2L):
    dataset = client.post("/api/datasets", json={"name": "标定索引测试", "kind": "calibration"}).json()
    response = client.post(
        f"/api/datasets/{dataset['id']}/files",
        files={"file": (name, io.BytesIO(content.encode("utf-8")), "text/plain")},
    )
    assert response.status_code == 200, response.text
    files = response.json()["files"]
    return next(f for f in files if f["name"] == name)


def test_index_build_and_trace(client):
    file_info = _upload_a2l(client)
    built = client.post("/api/a2l-index", json={"dataset_file_id": file_info["id"]})
    assert built.status_code == 200, built.text
    meta = built.json()
    assert meta["byte_order"] == "MSB_LAST"
    assert meta["counts"] == {"measurements": 4, "characteristics": 1, "functions": 3}
    sha = meta["sha256"]

    # Idempotent rebuild returns the same meta.
    again = client.post("/api/a2l-index", json={"dataset_file_id": file_info["id"]})
    assert again.json()["sha256"] == sha

    hits = client.get(f"/api/a2l-index/{sha}/measurements", params={"q": "lambda"}).json()["measurements"]
    assert [h["name"] for h in hits] == ["UEGO_rLamS1B1"]

    # FLOW_DESIGN §6 check: producer + DEF set + upstream + consumer.
    trace = client.get(f"/api/a2l-index/{sha}/trace", params={"signal": "UEGO_rLamS1B1"}).json()
    assert trace["found"] is True
    assert [p["function"] for p in trace["producers"]] == ["UEGO_LamCalc"]
    assert "UEGO_IntfrCorrn_CUR" in trace["candidate_characteristics"]
    assert [u["function"] for u in trace["upstream"]] == ["FuelCalc"]
    assert trace["upstream"][0]["via_signal"] == "rl_w_msg"
    assert trace["consumers"] == ["ExhMod_RawSotMdl"]


def test_variant_family(client):
    from backend.services import a2l_index

    file_info = _upload_a2l(client, name="variants.a2l")
    sha = client.post("/api/a2l-index", json={"dataset_file_id": file_info["id"]}).json()["sha256"]
    variants = a2l_index.variant_family(sha, "rl_w")
    assert [v["name"] for v in variants] == ["rl_w_msg"]
    assert variants[0]["variant_reason"] == "name_prefix"
    # nmot_w shares the compu method but is neither a prefix variant nor similar.
    assert all(v["name"] != "nmot_w" for v in variants)


def test_msb_first_rejected(client):
    from backend.services.calibration import _load_a2l
    from pathlib import Path
    import pytest
    import tempfile

    big_endian = SAMPLE_A2L.replace("MSB_LAST", "MSB_FIRST")
    with tempfile.NamedTemporaryFile("w", suffix=".a2l", delete=False, encoding="utf-8") as handle:
        handle.write(big_endian)
        path = Path(handle.name)
    with pytest.raises(ValueError, match="MSB_FIRST"):
        _load_a2l(path)
    path.unlink()
