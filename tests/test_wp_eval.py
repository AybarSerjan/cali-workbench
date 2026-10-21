from __future__ import annotations

import io

import numpy as np
from asammdf import MDF, Signal

from agent.tools.expression import UnsafeExpression, align_series, evaluate_expression
from agent.tools.signals import bool_events, detect_startup_events, find_mask_windows


A2L = b'''/begin PROJECT P "" /begin MODULE M ""
/begin MOD_COMMON "" BYTE_ORDER MSB_LAST /end MOD_COMMON
/begin MEASUREMENT actual "Actual lambda" FLOAT32_IEEE CM 1 0 0 2 /end MEASUREMENT
/begin MEASUREMENT target "Target lambda" FLOAT32_IEEE CM 1 0 0 2 /end MEASUREMENT
/begin MEASUREMENT speed "Vehicle speed" FLOAT32_IEEE CM 1 0 0 300 /end MEASUREMENT
/begin MEASUREMENT speed_can "Vehicle speed" FLOAT32_IEEE CM 1 0 0 300 /end MEASUREMENT
/begin COMPU_METHOD CM "" IDENTICAL "%6.3" "" /end COMPU_METHOD
/begin FUNCTION Lambda "" /begin OUT_MEASUREMENT actual /end OUT_MEASUREMENT /begin IN_MEASUREMENT target /end IN_MEASUREMENT /end FUNCTION
/end MODULE /end PROJECT'''


def _mf4() -> bytes:
    t = np.arange(0, 1.01, 0.1)
    actual = np.ones(len(t)); actual[3:8] = 1.12
    mdf = MDF(version="4.10")
    mdf.append([Signal(actual, t, name="actual\\XCP:1"), Signal(np.ones(len(t)), t, name="target"),
                Signal(np.arange(len(t)), t, name="speed_can")])
    stream = io.BytesIO(); mdf.save(stream, overwrite=True); mdf.close()
    return stream.getvalue()


def _datasets(client, unique_name):
    mdf = client.post("/api/datasets/files", files={"file": (f"{unique_name}.mf4", _mf4(), "application/octet-stream")},
                      data={"kind": "mdf"}).json()
    cal = client.post("/api/datasets", json={"name": unique_name, "kind": "calibration", "description": ""}).json()
    cal = client.post(f"/api/datasets/{cal['id']}/files", files={"file": (f"{unique_name}.a2l", A2L, "text/plain")}).json()
    return mdf, next(item for item in cal["files"] if item["suffix"] == ".a2l")


def _wp(client, unique_name, approved=True):
    definition = {"signals": [{"a2l_name": "actual", "match_status": "confirmed"},
                              {"a2l_name": "target", "match_status": "confirmed"}],
                  "scenario_gate": "target > 0.5",
                  "criteria": [{"expression": "abs(actual - target) > 0.05", "min_duration_s": 0.3,
                                "threshold_source": "HUMAN_APPROVED"}]}
    wp = client.post("/api/workpackages", json={"wp_code": f"WP-E-{unique_name}", "title": unique_name,
                                                "definition": definition}).json()
    if approved:
        client.post(f"/api/workpackages/{wp['id']}/review", json={})
        wp = client.post(f"/api/workpackages/{wp['id']}/approve").json()
    return wp


def test_expression_whitelist_alignment_and_windows():
    t, values = align_series({"a": ([0, 1, 2], [1, 3, 1]), "b": ([0, 2], [1, 1])})
    mask = evaluate_expression("abs(a - b) > 1 and b == 1", values)
    assert t.tolist() == [0, 1, 2] and mask.tolist() == [False, True, False]
    assert find_mask_windows(t, mask) == [{"start": 1.0, "end": 1.0, "duration": 0.0}]
    for expression in ("__import__('os')", "a.__class__", "a[0]"):
        try: evaluate_expression(expression, values)
        except UnsafeExpression: pass
        else: raise AssertionError(f"unsafe expression accepted: {expression}")
    assert bool_events([0, 1, 2, 3], [0, 1, 1, 0])["active_s"] == 2.0
    assert detect_startup_events([0, 1, 2], [0, 500, 600])[0]["t_s"] == 1.0


def test_match_and_evaluate_approved_wp(client, unique_name):
    dataset, a2l = _datasets(client, unique_name)
    wp = _wp(client, unique_name)
    matched = client.post(f"/api/workpackages/{wp['id']}/match", json={"dataset_id": dataset["id"]})
    assert matched.status_code == 200, matched.text
    assert {item["layer"] for item in matched.json()["matches"]} == {"L1"}
    result = client.post(f"/api/workpackages/{wp['id']}/evaluate",
                         json={"dataset_id": dataset["id"], "a2l_file_id": a2l["id"]})
    assert result.status_code == 200, result.text
    summary = result.json()["summary"]
    assert summary["overall"] == "FAIL"
    assert summary["criteria"][0]["windows"][0]["duration"] >= 0.3
    assert summary["criteria"][0]["snapshots"]
    assert summary["explanation"]["status"] == "unavailable"
    detail = client.get(f"/api/wp-evaluations/{result.json()['id']}")
    assert detail.status_code == 200


def test_unapproved_wp_only_observes(client, unique_name):
    dataset, a2l = _datasets(client, unique_name)
    wp = _wp(client, unique_name, approved=False)
    result = client.post(f"/api/workpackages/{wp['id']}/evaluate",
                         json={"dataset_id": dataset["id"], "a2l_file_id": a2l["id"]})
    assert result.status_code == 200, result.text
    assert result.json()["summary"]["overall"] == "OBSERVATION"
    assert result.json()["summary"]["criteria"][0]["verdict"] == "OBSERVATION"


def test_l2_proposal_requires_confirmation_then_reuses(client, unique_name):
    dataset, a2l = _datasets(client, unique_name)
    index = client.post("/api/a2l-index", json={"dataset_file_id": a2l["id"]}).json()
    definition = {"signals": [{"a2l_name": "speed", "match_status": "confirmed"}],
                  "criteria": [{"expression": "speed > 300", "threshold_source": "HUMAN_APPROVED"}]}
    wp = client.post("/api/workpackages", json={"wp_code": f"WP-L2-{unique_name}", "title": unique_name,
                                                "definition": definition}).json()
    first = client.post(f"/api/workpackages/{wp['id']}/match",
                        json={"dataset_id": dataset["id"], "a2l_sha256": index["sha256"]}).json()["matches"][0]
    assert first["layer"] == "L2" and first["status"] == "proposed"
    assert client.post(f"/api/signal-map/{first['mapping_id']}/confirm").status_code == 200
    second = client.post(f"/api/workpackages/{wp['id']}/match",
                         json={"dataset_id": dataset["id"], "a2l_sha256": index["sha256"]}).json()["matches"][0]
    assert second["layer"] == "reuse" and second["status"] == "confirmed"


def test_missing_signal_and_empty_gate_are_not_pass(client, unique_name):
    dataset, a2l = _datasets(client, unique_name)
    missing_def = {"signals": [{"a2l_name": "not_recorded", "match_status": "confirmed"}],
                   "criteria": [{"expression": "not_recorded > 0", "threshold_source": "HUMAN_APPROVED"}]}
    missing_wp = client.post("/api/workpackages", json={"wp_code": f"WP-MISS-{unique_name}", "title": unique_name,
                                                        "definition": missing_def}).json()
    missing = client.post(f"/api/workpackages/{missing_wp['id']}/evaluate",
                          json={"dataset_id": dataset["id"], "a2l_file_id": a2l["id"]})
    assert missing.status_code == 200 and missing.json()["summary"]["overall"] == "NOT_EVALUABLE"

    gate_def = {"signals": [{"a2l_name": "target", "match_status": "confirmed"}],
                "scenario_gate": "target < 0",
                "criteria": [{"expression": "target > 0", "threshold_source": "HUMAN_APPROVED"}]}
    gate_wp = client.post("/api/workpackages", json={"wp_code": f"WP-GATE-{unique_name}", "title": unique_name,
                                                     "definition": gate_def}).json()
    client.post(f"/api/workpackages/{gate_wp['id']}/review", json={})
    client.post(f"/api/workpackages/{gate_wp['id']}/approve")
    gated = client.post(f"/api/workpackages/{gate_wp['id']}/evaluate",
                        json={"dataset_id": dataset["id"], "a2l_file_id": a2l["id"]})
    assert gated.status_code == 200 and gated.json()["summary"]["overall"] == "NOT_APPLICABLE_IN_FILE"


def test_evaluate_without_a2l(client, unique_name):
    dataset, _ = _datasets(client, unique_name)
    wp = _wp(client, unique_name)
    result = client.post(f"/api/workpackages/{wp['id']}/evaluate", json={"dataset_id": dataset["id"]})
    assert result.status_code == 200, result.text
    summary = result.json()["summary"]
    assert summary["overall"] == "FAIL"
    assert result.json()["a2l_sha256"] == ""


def test_delete_workpackage_with_evaluations(client, unique_name):
    dataset, _ = _datasets(client, unique_name)
    wp = _wp(client, unique_name)
    assert client.post(f"/api/workpackages/{wp['id']}/evaluate", json={"dataset_id": dataset["id"]}).status_code == 200
    assert client.delete(f"/api/workpackages/{wp['id']}").status_code == 200
    assert client.get(f"/api/workpackages/{wp['id']}").status_code == 400  # not found is mapped to 400 by fail()


def test_evaluate_all_and_overview(client, unique_name):
    dataset, a2l = _datasets(client, unique_name)
    approved = _wp(client, unique_name)
    draft_def = {"signals": [{"a2l_name": "target", "match_status": "confirmed"}],
                 "criteria": [{"expression": "target > 0", "threshold_source": "HUMAN_APPROVED"}]}
    draft = client.post("/api/workpackages", json={"wp_code": f"WP-D-{unique_name}", "title": unique_name,
                                                   "definition": draft_def}).json()
    batch = client.post("/api/workpackages/evaluate-all",
                        json={"dataset_id": dataset["id"], "a2l_file_id": a2l["id"]})
    assert batch.status_code == 200, batch.text
    payload = batch.json()
    rows = {item["wp_code"]: item for item in payload["results"]}
    assert rows[approved["wp_code"]]["overall"] == "FAIL"
    assert rows[draft["wp_code"]]["overall"] == "NOT_APPROVED"
    assert payload["counts"]["FAIL"] >= 1
    assert payload["explanation"]["status"] == "unavailable"
    overview = client.get("/api/workpackages/overview")
    assert overview.status_code == 200
    view = {item["wp_code"]: item for item in overview.json()}
    assert view[approved["wp_code"]]["overall"] == "FAIL" and view[approved["wp_code"]]["approved"]
    assert view[draft["wp_code"]]["overall"] is None and not view[draft["wp_code"]]["approved"]
