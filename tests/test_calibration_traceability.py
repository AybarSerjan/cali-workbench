from __future__ import annotations

from sqlalchemy import select

from backend.db import SessionLocal
from backend.models import WikiArticle, WikiRevision


def _hex_record(address: int, data: bytes) -> bytes:
    payload = bytes([len(data), address >> 8, address & 0xFF, 0]) + data
    checksum = (-sum(payload)) & 0xFF
    return (":" + (payload + bytes([checksum])).hex().upper() + "\n:00000001FF\n").encode()


A2L = b'''/begin PROJECT Demo "Demo"
  /begin MODULE Engine "Engine"
    /begin RECORD_LAYOUT ScalarU16
      FNC_VALUES 1 UWORD COLUMN_DIR DIRECT
    /end RECORD_LAYOUT
    /begin COMPU_METHOD RpmConversion "RPM" LINEAR "%6.0" "rpm"
      COEFFS_LINEAR 1 0
    /end COMPU_METHOD
    /begin CHARACTERISTIC EngineIdleRPM "Idle speed target"
      VALUE 0x1000 ScalarU16 0 RpmConversion 0 8000
    /end CHARACTERISTIC
  /end MODULE
/end PROJECT
'''


def _upload_source(client, name: str, content: bytes) -> dict:
    response = client.post("/api/sources", files={"file": (name, content, "application/octet-stream")})
    assert response.status_code == 200, response.text
    return response.json()


def test_calibration_session_compare_analyze_query_and_wiki_drafts(client):
    a2l = _upload_source(client, "engine.a2l", A2L)
    old_hex = _upload_source(client, "engine-v1.hex", _hex_record(0x1000, (600).to_bytes(2, "little")))
    new_hex = _upload_source(client, "engine-v2.hex", _hex_record(0x1000, (650).to_bytes(2, "little")))

    created = client.post(
        "/api/calibration/sessions",
        json={"a2l_file_id": a2l["id"], "hex_file_ids": [old_hex["id"], new_hex["id"]]},
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]

    compared = client.post(f"/api/calibration/sessions/{session_id}/compare")
    assert compared.status_code == 200, compared.text
    changes = compared.json()["parameter_changes"]
    assert len(changes) == 1
    assert changes[0] | {
        "parameter_name": "EngineIdleRPM",
        "module": "Engine",
        "data_type": "uint16",
        "unit": "rpm",
        "range": "0-8000",
        "old_value": "600",
        "new_value": "650",
        "change_magnitude": 50.0,
    } == changes[0]

    analyzed = client.post(f"/api/calibration/sessions/{session_id}/analyze")
    assert analyzed.status_code == 200, analyzed.text
    assert "1 parameters changed" in analyzed.json()["session_summary"]
    assert "EngineIdleRPM: 600" in analyzed.json()["parameter_changes"][0]["change_summary"]

    queried = client.get("/api/calibration/parameters", params={"module": "Engine", "parameter_name": "Idle"})
    assert queried.status_code == 200
    assert [item["session_id"] for item in queried.json()] == [session_id]

    synced = client.post("/api/calibration/wiki/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json()["drafts_created"] == 2
    with SessionLocal() as db:
        articles = {item.slug: item for item in db.scalars(select(WikiArticle)).all()}
        assert {"calibration-timeline", "calibration-engine"} <= articles.keys()
        timeline = db.scalar(
            select(WikiRevision)
            .where(WikiRevision.article_id == articles["calibration-timeline"].id)
            .order_by(WikiRevision.number.desc())
        )
        assert timeline.status == "draft"
        assert "EngineIdleRPM" in timeline.content


CURVE_A2L = b'''/begin PROJECT Demo "Demo"
  /begin MODULE Engine "Engine"
    /begin RECORD_LAYOUT CurveU8
      NO_AXIS_PTS_X 1 UBYTE
      AXIS_PTS_X 2 UBYTE INDEX_INCR DIRECT
      FNC_VALUES 3 UBYTE COLUMN_DIR DIRECT
    /end RECORD_LAYOUT
    /begin COMPU_METHOD Ident "identity" IDENTICAL "%6.0" "-"
    /end COMPU_METHOD
    /begin CHARACTERISTIC IdleCurve "Idle curve"
      CURVE 0x2000 CurveU8 0 Ident 0 255
      /begin AXIS_DESCR STD_AXIS InVar Ident 4 0 255
      /end AXIS_DESCR
    /end CHARACTERISTIC
  /end MODULE
/end PROJECT
'''


def test_calibration_session_detects_curve_cell_change(client):
    # Layout at 0x2000: [count=4][axis 0,16,32,48][fnc values]. Only fnc[1] moves 20 -> 25.
    a2l = _upload_source(client, "curve.a2l", CURVE_A2L)
    old_hex = _upload_source(client, "curve-v1.hex", _hex_record(0x2000, bytes([4, 0, 16, 32, 48, 10, 20, 30, 40])))
    new_hex = _upload_source(client, "curve-v2.hex", _hex_record(0x2000, bytes([4, 0, 16, 32, 48, 10, 25, 30, 40])))

    created = client.post(
        "/api/calibration/sessions",
        json={"a2l_file_id": a2l["id"], "hex_file_ids": [old_hex["id"], new_hex["id"]]},
    )
    assert created.status_code == 200, created.text
    compared = client.post(f"/api/calibration/sessions/{created.json()['id']}/compare")
    assert compared.status_code == 200, compared.text
    changes = compared.json()["parameter_changes"]
    assert len(changes) == 1
    change = changes[0]
    assert change["parameter_name"] == "IdleCurve"
    assert change["char_type"] == "CURVE"
    assert change["detail"]["nChg"] == 1
    assert change["detail"]["chg"] == [{"i": 1, "o": 20, "n": 25}]


def test_calibration_session_validates_file_roles(client):
    a2l = _upload_source(client, "only.a2l", A2L)
    response = client.post(
        "/api/calibration/sessions",
        json={"a2l_file_id": a2l["id"], "hex_file_ids": [a2l["id"], "missing"]},
    )
    assert response.status_code == 400
