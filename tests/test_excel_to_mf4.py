from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pytest


def _build_xlsx(rows: list[list]) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "data.xlsx"
        workbook.save(path)
        return path.read_bytes()


def test_excel_to_mf4_with_time_column_and_units(client):
    pytest.importorskip("asammdf")
    payload = _build_xlsx([
        ["time", "Lambda", "Speed"],
        ["s", "-", "km/h"],
        [0.0, 1.0, 10.0],
        [1.0, 0.9, 20.0],
        [2.0, 1.1, 30.0],
    ])
    response = client.post(
        "/api/tools/excel_to_mf4",
        files={"file": ("data.xlsx", io.BytesIO(payload), "application/octet-stream")},
        data={"units_row": "auto"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows"] == 3
    assert set(body["channels"]) == {"Lambda", "Speed"}
    assert body["time_source"] == "column:time"
    token = body["token"]

    download = client.get(f"/api/tools/excel_to_mf4/{token}/download")
    assert download.status_code == 200

    from asammdf import MDF

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / body["name"]
        path.write_bytes(download.content)
        with MDF(path) as mdf:
            names = {ch.name for group in mdf.groups for ch in group.channels}
            assert {"Lambda", "Speed"}.issubset(names)
            lambda_sig = mdf.get("Lambda")
            assert list(lambda_sig.timestamps) == [0.0, 1.0, 2.0]
            assert lambda_sig.unit == "-"


def test_excel_to_mf4_sample_rate_fallback_and_skip_text(client):
    pytest.importorskip("asammdf")
    payload = _build_xlsx([
        ["Lambda", "Note"],
        [1.0, "ok"],
        [0.9, "ok"],
        [1.1, "bad"],
        [1.0, "ok"],
    ])
    response = client.post(
        "/api/tools/excel_to_mf4",
        files={"file": ("data.xlsx", io.BytesIO(payload), "application/octet-stream")},
        data={"sample_rate": "10", "units_row": "no"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["channels"] == ["Lambda"]
    assert body["skipped"] == ["Note"]
    assert body["time_source"] == "sample_rate"

    from asammdf import MDF

    download = client.get(f"/api/tools/excel_to_mf4/{body['token']}/download")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / body["name"]
        path.write_bytes(download.content)
        with MDF(path) as mdf:
            sig = mdf.get("Lambda")
            # sample_rate=10Hz → timestamps 0, 0.1, 0.2, 0.3
            assert list(sig.timestamps) == pytest.approx([0.0, 0.1, 0.2, 0.3])


def test_excel_to_mf4_save_as_dataset(client):
    pytest.importorskip("asammdf")
    payload = _build_xlsx([
        ["time", "Lambda"],
        [0.0, 1.0],
        [1.0, 0.9],
    ])
    converted = client.post(
        "/api/tools/excel_to_mf4",
        files={"file": ("data.xlsx", io.BytesIO(payload), "application/octet-stream")},
        data={"name": "from_excel"},
    ).json()
    saved = client.post(f"/api/tools/excel_to_mf4/{converted['token']}/save")
    assert saved.status_code == 200, saved.text
    dataset = saved.json()
    assert dataset["kind"] == "mdf"
    assert any(f["suffix"] == ".mf4" for f in dataset["files"])


def test_excel_to_mf4_rejects_non_xlsx(client):
    response = client.post(
        "/api/tools/excel_to_mf4",
        files={"file": ("data.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
    )
    assert response.status_code == 400
