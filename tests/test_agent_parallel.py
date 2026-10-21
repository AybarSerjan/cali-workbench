from __future__ import annotations

import json

import pytest

import agent.nodes as nodes
from agent.nodes import data_specialist_node, merge_specialists_node
from agent.state import AgentReport, merge_agent_reports
from backend.config import ROOT


def _report(agent: str, run_id: str = "run") -> AgentReport:
    return {
        "run_id": run_id,
        "agent": agent,
        "status": "ok",
        "relevance": 1.0,
        "summary": agent,
        "evidence": [],
        "artifacts": [],
        "warnings": [],
        "error": None,
        "duration_ms": 1,
    }


def test_report_reducer_isolates_branches_and_turns():
    reports = merge_agent_reports({"wiki": _report("wiki")}, {"data": _report("data")})
    assert set(reports) == {"wiki", "data"}
    with pytest.raises(ValueError, match="重复写入"):
        merge_agent_reports(reports, {"wiki": _report("wiki")})
    next_turn = merge_agent_reports(reports, {"wiki": _report("wiki", "next")})
    assert next_turn["wiki"]["run_id"] == "next"


def test_irrelevant_data_agent_does_not_query_catalog(monkeypatch):
    monkeypatch.setattr(nodes, "query_catalog", lambda *_args, **_kwargs: pytest.fail("不应查询数据目录"))
    result = data_specialist_node({"run_id": "r", "route": "knowledge", "question": "发动机扭矩怎么定义"})
    assert result["reports"]["data"]["status"] == "not_applicable"


def test_merge_order_is_deterministic(client):
    reports = {name: _report(name) for name in ("operations", "memory", "data", "wiki")}
    merged = merge_specialists_node({"run_id": "run", "reports": reports})
    assert [item["agent"] for item in merged["merged_reports"]] == ["wiki", "data", "memory", "operations"]


def test_one_agent_failure_isolated_and_audited(client, monkeypatch, unique_name):
    def fail_catalog(*_args, **_kwargs):
        raise RuntimeError("catalog offline")

    monkeypatch.setattr(nodes, "query_catalog", fail_catalog)
    conversation = client.post("/api/conversations", json={"title": unique_name}).json()
    reply = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "当前有哪些数据集？"},
    )
    assert reply.status_code == 200, reply.text
    assert "catalog offline" in reply.json()["answer"]
    run_id = reply.json()["run_id"]
    reports = {
        name: json.loads((ROOT / "outputs" / "runs" / run_id / "agents" / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("wiki", "data", "memory", "operations")
    }
    assert reports["data"]["status"] == "error"
    assert reports["data"]["error"] == "catalog offline"
    assert reports["operations"]["status"] == "not_applicable"

    manifest = json.loads((ROOT / "outputs" / "runs" / run_id / "manifest.json").read_text(encoding="utf-8"))
    artifact_paths = {item["path"] for item in manifest["artifacts"]}
    assert {f"outputs/runs/{run_id}/agents/{name}.json" for name in reports} <= artifact_paths
    events = [json.loads(line) for line in (ROOT / "outputs" / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(item["node"] == "merge" for item in events) == 1
    assert not any(item["node"].startswith("specialist:") for item in events)


def test_reports_do_not_leak_between_routes_or_turns(client, unique_name):
    dataset = client.post("/api/datasets", json={"name": unique_name, "kind": "mdf"}).json()
    conversation = client.post("/api/conversations", json={"title": unique_name}).json()

    knowledge = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "发动机扭矩怎么定义"},
    )
    assert knowledge.status_code == 200, knowledge.text
    assert all(source["kind"] != "database" for source in knowledge.json()["sources"])
    first_data_report = json.loads(
        (ROOT / "outputs" / "runs" / knowledge.json()["run_id"] / "agents" / "data.json").read_text(encoding="utf-8")
    )
    assert first_data_report["status"] == "not_applicable"

    data = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "当前有哪些数据集？"},
    )
    assert data.status_code == 200, data.text
    assert dataset["serial"] in data.json()["answer"]
    assert any(source["kind"] == "database" for source in data.json()["sources"])


def test_mf4_agent_writes_full_window_artifact(client, tmp_path, unique_name):
    import numpy as np
    from asammdf import MDF, Signal

    path = tmp_path / f"{unique_name}.mf4"
    with MDF(version="4.10") as mdf:
        mdf.append(Signal(
            samples=np.array([1200.0, 900.0, 800.0, 1200.0]),
            timestamps=np.array([0.0, 1.0, 2.0, 3.0]),
            name="rpm",
            unit="rpm",
        ))
        mdf.save(path, overwrite=True)
    dataset = client.post(
        "/api/datasets/files",
        files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
    ).json()
    conversation = client.post("/api/conversations", json={"title": unique_name}).json()
    reply = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": f"{dataset['serial']} rpm < 1000 持续 1秒"},
    )
    assert reply.status_code == 200, reply.text
    assert "发现 1 个窗口" in reply.json()["answer"]
    report = json.loads(
        (ROOT / "outputs" / "runs" / reply.json()["run_id"] / "agents" / "data.json").read_text(encoding="utf-8")
    )
    artifact = report["artifacts"][0]
    windows = json.loads((ROOT / artifact["path"]).read_text(encoding="utf-8"))
    assert windows["window_count"] == 1
    assert windows["windows"][0]["duration"] == 1.0
