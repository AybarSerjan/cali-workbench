from __future__ import annotations

import re
from datetime import datetime, timezone

from agent.router import classify
from agent.tools.catalog import query_catalog
from backend.db import SessionLocal
from backend.services.retrieval import cosine
from agent.tools.datasets import extract_mdf_metadata


def test_dataset_contract_and_honest_unavailable_capability(client, unique_name):
    dataset = client.post("/api/datasets", json={"name": unique_name, "kind": "mdf", "description": ""})
    assert dataset.status_code == 200
    dataset_id = dataset.json()["id"]
    upload = client.post(
        f"/api/datasets/{dataset_id}/files",
        files={"file": (f"{unique_name}.mf4", b"not-a-real-mdf-yet", "application/octet-stream")},
    )
    assert upload.status_code == 200
    # asammdf provides MDF signal reading, so the capability is advertised.
    assert upload.json()["inspection"]["capabilities"]["read_window"] is True
    # But a non-MDF blob must fail as bad input — the system never fabricates
    # a window result for unparseable data.
    window = client.post(
        f"/api/datasets/{dataset_id}/tools/window",
        json={"channel": "rpm", "start": 0, "end": 1},
    )
    assert window.status_code >= 400


def test_calibration_version_relationship(client, unique_name):
    dataset = client.post("/api/datasets", json={"name": unique_name, "kind": "calibration", "description": ""}).json()
    for filename, content in (("base.a2l", b"A2L"), ("v1.hex", b"111"), ("v2.hex", b"2222")):
        response = client.post(f"/api/datasets/{dataset['id']}/files", files={"file": (filename, content, "application/octet-stream")})
        assert response.status_code == 200
    compared = client.post(f"/api/datasets/{dataset['id']}/tools/compare", json={})
    assert compared.status_code == 200
    assert len(compared.json()["versions"]) == 2
    assert compared.json()["semantic_diff_available"] is False


def test_router_and_cosine():
    assert classify("读取 MF4 的转速信号") == "data"
    assert classify("MDF-0001 使用了哪个 A2L？") == "data"
    assert classify("查询数据库中的上传文件") == "data"
    assert classify("发布 wiki 草稿") == "wiki"
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_chat_graph_records_checkpoint(client, unique_name):
    dataset = client.post("/api/datasets", json={"name": unique_name, "kind": "mdf", "description": ""}).json()
    conversation = client.post("/api/conversations", json={"title": unique_name}).json()
    reply = client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "当前有哪些数据集？"})
    assert reply.status_code == 200, reply.text
    assert dataset["serial"] in reply.json()["answer"]
    assert any(source["kind"] == "database" and source["path"] == f"database:datasets/{dataset['serial']}" for source in reply.json()["sources"])
    checkpoints = client.get(f"/api/conversations/{conversation['id']}/checkpoints")
    assert checkpoints.status_code == 200
    assert len(checkpoints.json()) >= 2


def test_conversation_history_keeps_full_chat_and_recent_order(client, unique_name):
    client.post("/api/datasets", json={"name": unique_name, "kind": "mdf", "description": ""})
    first = client.post("/api/conversations", json={"title": "新对话"}).json()
    second = client.post("/api/conversations", json={"title": f"{unique_name}-idle"}).json()

    reply = client.post(f"/api/conversations/{first['id']}/messages", json={"content": "当前有哪些数据集？"})
    assert reply.status_code == 200, reply.text

    history = client.get("/api/conversations").json()
    assert history[0]["id"] == first["id"]
    assert history[0]["title"].startswith("当前有哪些数据集")
    assert second["id"] in {item["id"] for item in history}
    assert [message["role"] for message in history[0]["messages"][:2]] == ["user", "assistant"]
    assert history[0]["messages"][0]["content"] == "当前有哪些数据集？"

    removed = client.post(f"/api/conversations/{second['id']}/archive")
    assert removed.status_code == 200, removed.text
    visible = client.get("/api/conversations").json()
    assert second["id"] not in {item["id"] for item in visible}
    archived = client.get("/api/conversations?include_archived=true").json()
    assert any(item["id"] == second["id"] and item["archived"] is True for item in archived)


def test_calibration_commit_chain_and_validation(client, unique_name):
    dataset = client.post("/api/datasets", json={"name": unique_name, "kind": "calibration"}).json()

    def upload(name, content):
        response = client.post(
            f"/api/datasets/{dataset['id']}/files",
            files={"file": (name, content, "application/octet-stream")},
        )
        assert response.status_code == 200
        return next(item for item in response.json()["files"] if item["name"] == name)

    a2l = upload("engine.a2l", b"CHARACTERISTIC A\n")
    hex_v1 = upload("v1.hex", b":0100000001FE\n")
    hex_v2 = upload("v2.hex", b":0100000002FD\n")

    missing_a2l = client.post(f"/api/datasets/{dataset['id']}/commits", json={"hex_file_id": hex_v1["id"]})
    assert missing_a2l.status_code == 400

    first = client.post(
        f"/api/datasets/{dataset['id']}/commits",
        json={"a2l_file_id": a2l["id"], "hex_file_id": hex_v1["id"]},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/datasets/{dataset['id']}/commits",
        json={"parent_id": first.json()["id"], "hex_file_id": hex_v2["id"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["a2l"]["id"] == a2l["id"]
    assert second.json()["diff"]["hex"]["changed"] is True
    # v1/v2 are single differing hex lines -> +1/-1; reused A2L has equal sha -> no file read, 0/0.
    assert second.json()["diff"]["hex"]["lines"] == {"added": 1, "removed": 1}
    assert second.json()["diff"]["a2l"]["lines"] == {"added": 0, "removed": 0}
    assert second.json()["message"]

    evolution = client.get(f"/api/datasets/{dataset['id']}/evolution")
    assert evolution.status_code == 200, evolution.text
    report = evolution.json()
    assert [item["hex"]["name"] for item in report["versions"]] == ["v1.hex", "v2.hex"]
    assert report["comparisons"][0]["mode"] == "adjacent"
    assert report["comparisons"][0]["from_version"] == 1
    assert report["comparisons"][0]["to_version"] == 2

    conversation = client.post("/api/conversations", json={"title": unique_name}).json()
    answer = client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": f"{dataset['serial']} 的标定版本和提交关系是什么？"}).json()
    assert "engine.a2l" in answer["answer"]
    assert "v2.hex" in answer["answer"]
    assert any(source["path"] == f"database:datasets/{dataset['serial']}" for source in answer["sources"])
    with SessionLocal() as session:
        assert dataset["serial"] in {hit["title"] for hit in query_catalog(session, "engine.a2l 属于哪个数据集？")}

    other = client.post("/api/datasets", json={"name": f"{unique_name}-other", "kind": "calibration"}).json()
    cross_dataset = client.post(
        f"/api/datasets/{other['id']}/commits",
        json={"a2l_file_id": a2l["id"], "hex_file_id": hex_v1["id"]},
    )
    assert cross_dataset.status_code == 400


def test_hex_matches_unique_a2l_by_address_coverage(client, unique_name):
    dataset = client.post("/api/datasets", json={"name": unique_name, "kind": "calibration"}).json()

    def upload(name: str, content: bytes) -> dict:
        response = client.post(f"/api/datasets/{dataset['id']}/files", files={"file": (name, content, "application/octet-stream")})
        assert response.status_code == 200, response.text
        return next(item for item in response.json()["files"] if item["name"] == name)

    def a2l(address: int) -> bytes:
        return f'''/begin PROJECT P "P"
/begin MODULE M "M"
/begin RECORD_LAYOUT R
FNC_VALUES 1 UBYTE COLUMN_DIR DIRECT
/end RECORD_LAYOUT
/begin CHARACTERISTIC Value "Value"
VALUE 0x{address:X} R 0 NO_COMPU_METHOD 0 255
/end CHARACTERISTIC
/end MODULE
/end PROJECT
'''.encode()

    def hex_file(address: int) -> bytes:
        payload = bytes([1, address >> 8, address & 0xFF, 0, 42])
        return (":" + (payload + bytes([(-sum(payload)) & 0xFF])).hex().upper() + "\n:00000001FF\n").encode()

    expected = upload("engine.a2l", a2l(0x1000))
    upload("other.a2l", a2l(0x2000))
    compatible_hex = upload("engine.hex", hex_file(0x1000))
    unknown_hex = upload("unknown.hex", hex_file(0x3000))

    matched = client.get(f"/api/datasets/{dataset['id']}/a2l-match/{compatible_hex['id']}")
    assert matched.status_code == 200, matched.text
    assert matched.json()["status"] == "matched"
    assert matched.json()["a2l"]["id"] == expected["id"]
    assert matched.json()["a2l"]["coverage"] == 1

    missing = client.get(f"/api/datasets/{dataset['id']}/a2l-match/{unknown_hex['id']}")
    assert missing.status_code == 200, missing.text
    assert missing.json()["status"] == "missing" and missing.json()["a2l"] is None


def test_upload_each_file_creates_dataset_and_reuses_duplicate(client):
    first = client.post(
        "/api/datasets/files",
        data={"kind": "mdf", "description": "数据分析"},
        files={"file": ("drive_a.mf4", b"time,speed\n0,1\n", "application/octet-stream")},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/datasets/files",
        data={"kind": "mdf", "description": "数据分析"},
        files={"file": ("drive_b.mf4", b"time,speed\n0,2\n", "application/octet-stream")},
    )
    assert second.status_code == 200, second.text
    duplicate = client.post(
        "/api/datasets/files",
        data={"kind": "mdf", "description": "数据分析"},
        files={"file": ("drive_a_copy.mf4", b"time,speed\n0,1\n", "application/octet-stream")},
    )
    assert duplicate.status_code == 200, duplicate.text
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["id"] != second_payload["id"]
    assert re.fullmatch(r"MDF-\d{4}", first_payload["serial"])
    assert re.fullmatch(r"MDF-\d{4}", second_payload["serial"])
    assert first_payload["serial"] != second_payload["serial"]
    assert re.fullmatch(r"MDF-\d{8}-\d{3}", first_payload["name"])
    assert re.fullmatch(r"MDF-\d{8}-\d{3}", second_payload["name"])
    assert first_payload["name"] != "drive_a"
    assert first_payload["files"][0]["name"] == "drive_a.mf4"
    assert first_payload["summary"]["source_file"] == "drive_a.mf4"
    assert first_payload["summary"]["sha256_short"] == first_payload["files"][0]["sha256"][:12]
    assert duplicate.json()["id"] == first_payload["id"]
    assert duplicate.json()["serial"] == first_payload["serial"]
    assert duplicate.json()["duplicate"] is True


def test_delete_dataset_removes_it_from_listing(client):
    created = client.post(
        "/api/datasets/files",
        data={"kind": "mdf", "description": "数据分析"},
        files={"file": ("delete_me.mf4", b"time,speed\n0,1\n", "application/octet-stream")},
    ).json()
    removed = client.delete(f"/api/datasets/{created['id']}")
    assert removed.status_code == 200, removed.text
    listed = client.get("/api/datasets").json()
    assert all(item["id"] != created["id"] for item in listed)


def test_delete_calibration_dataset_with_commits_succeeds(client, unique_name):
    dataset = client.post("/api/datasets", json={"name": unique_name, "kind": "calibration"}).json()

    def upload(name: str, content: bytes) -> dict:
        response = client.post(
            f"/api/datasets/{dataset['id']}/files",
            files={"file": (name, content, "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
        return next(item for item in response.json()["files"] if item["name"] == name)

    a2l = upload("engine.a2l", b"CHARACTERISTIC A\n")
    hex_v1 = upload("v1.hex", b":0100000001FE\n")

    committed = client.post(
        f"/api/datasets/{dataset['id']}/commits",
        json={"a2l_file_id": a2l["id"], "hex_file_id": hex_v1["id"]},
    )
    assert committed.status_code == 200, committed.text

    removed = client.delete(f"/api/datasets/{dataset['id']}")
    assert removed.status_code == 200, removed.text

    listed = client.get("/api/datasets").json()
    assert all(item["id"] != dataset["id"] for item in listed)


def test_update_dataset_label_and_description(client):
    created = client.post(
        "/api/datasets/files",
        data={"kind": "mdf", "description": "数据分析"},
        files={"file": ("label_me.mf4", b"time,speed\n0,3\n", "application/octet-stream")},
    ).json()
    updated = client.patch(
        f"/api/datasets/{created['id']}",
        json={"name": "低温启动-001", "description": "车辆 A，第 2 次低温启动"},
    )
    assert updated.status_code == 200, updated.text
    payload = updated.json()
    assert payload["serial"] == created["serial"]
    assert payload["name"] == "低温启动-001"
    assert payload["description"] == "车辆 A，第 2 次低温启动"
    assert payload["summary"]["label"].startswith("低温启动-001 · 车辆 A，第 2 次低温启动")

    invalid = client.patch(f"/api/datasets/{created['id']}", json={"name": "   "})
    assert invalid.status_code == 400


def test_extract_and_edit_mdf_metadata(client, tmp_path):
    from asammdf import MDF

    path = tmp_path / "synthetic_metadata.mf4"
    with MDF(version="4.10") as mdf:
        mdf.start_time = datetime(2000, 1, 1, 8, 30, tzinfo=timezone.utc)
        mdf.header.comment = "<HDcomment><TX>Experiment: SYNTHETIC_cold_start_v1\nWP: foo</TX></HDcomment>"
        mdf.attach(b"A2L", file_name="engine.a2l")
        mdf.attach(b"HEX", file_name="release.hex")
        mdf.save(path, overwrite=True)

    extracted = extract_mdf_metadata(path)
    assert extracted["measurement_date"] == "2000-01-01"
    assert extracted["a2l"] == "engine.a2l"
    assert extracted["hex"] == "release.hex"
    assert extracted["experiment"] == "SYNTHETIC_cold_start_v1"

    uploaded = client.post("/api/datasets/files", files={"file": (path.name, path.read_bytes(), "application/octet-stream")})
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    detected = client.post(f"/api/datasets/{created['id']}/tools/metadata", json={})
    assert detected.status_code == 200, detected.text
    assert detected.json() == extracted
    updated = client.patch(f"/api/datasets/{created['id']}", json={**extracted, "experiment": "手动实验名", "a2l": "manual.a2l"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["metadata"]["experiment"] == "手动实验名"
    assert updated.json()["metadata"]["a2l"] == "manual.a2l"

    conversation = client.post("/api/conversations", json={"title": "database metadata"}).json()
    answer = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": f"{created['serial']} 使用哪个 A2L、HEX，日期和 Experiment 是什么？"},
    ).json()
    assert all(value in answer["answer"] for value in ("manual.a2l", "release.hex", "2000-01-01", "手动实验名"))
    with SessionLocal() as session:
        assert created["serial"] in {hit["title"] for hit in query_catalog(session, "2000-01-01 有哪些数据集？")}


def test_obfcm_analysis_reads_channels_and_returns_result_and_factor(client, tmp_path):
    from asammdf import MDF, Signal
    import numpy as np

    # Constant operating point: base_uproml = 0.00037*1000/740 = 0.0005,
    # denom = 1000*0.0005 = 0.5, K = 0.0005*1000/0.5 = 1.0.
    t = np.arange(0, 5, 0.1)

    def sig(name, value):
        return Signal(samples=np.full(t.shape, value, dtype=float), timestamps=t, name=name)

    path = tmp_path / "obfcm_run.mf4"
    with MDF() as mdf:
        mdf.append([
            sig("Epm_nEng", 3000.0), sig("rkme_w_msg", 95.0), sig("InjSys_dvolFu", 0.0005),
            sig("rksum_w", 1000.0), sig("InjSys_facCnvnRatFuM", 0.00037), sig("InjSys_rhoFuStd", 740.0),
            sig("THC_g_s", 0.0), sig("CO_g_s", 0.0), sig("CO2_g_s", 100.0),
        ])
        mdf.save(path, overwrite=True)

    created = client.post("/api/datasets/files", files={"file": (path.name, path.read_bytes(), "application/octet-stream")}).json()
    result = client.post(f"/api/datasets/{created['id']}/analysis/obfcm", json={})
    assert result.status_code == 200, result.text
    body = result.json()
    assert abs(body["k_factor"]["mean"] - 1.0) < 1e-6
    assert body["reference_fuel_available"] is True
    assert body["obfcm_accuracy"] is not None
    # rpm 3000 → bin 2500-4000 (col 2), load 95 → bin 80-110 (row 2).
    assert abs(body["k_matrix"][2][2]["k_avg"] - 1.0) < 1e-6


def test_obfcm_missing_required_channel_fails_loudly(client):
    created = client.post(
        "/api/datasets/files",
        files={"file": ("plain.mf4", b"not-a-real-mdf", "application/octet-stream")},
    ).json()
    result = client.post(f"/api/datasets/{created['id']}/analysis/obfcm", json={})
    assert result.status_code >= 400


def test_catalog_query_is_read_only_bounded_and_honest(client):
    conversation = client.post("/api/conversations", json={"title": "missing dataset"}).json()
    missing = client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "MDF-9999 的文件是什么？"})
    assert missing.json()["answer"] == "数据库中没有找到匹配记录。"
    assert missing.json()["sources"] == []

    with SessionLocal() as session:
        hits = query_catalog(session, "当前有哪些数据集？", limit=3)
        assert len(hits) <= 3
        assert not session.new and not session.dirty and not session.deleted
