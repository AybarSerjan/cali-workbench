from __future__ import annotations

import backend.services.investigation as investigation


def _fake_windows(dataset, signal, op, threshold, min_duration=0.0):
    return {
        "dataset_id": dataset.id,
        "signal": signal,
        "unit": "-",
        "op": op,
        "threshold": threshold,
        "min_duration": min_duration,
        "window_count": 1,
        "windows": [{
            "file": "run.mf4", "start": 1.0, "end": 2.0, "duration": 1.0,
            "samples": 2, "min": 0.7, "mean": 0.8, "max": 0.9,
        }],
    }


def test_normal_chat_clarifies_anomaly_definition_then_runs(client, unique_name, monkeypatch):
    dataset = client.post("/api/datasets", json={"name": unique_name, "kind": "mdf"}).json()
    conversation = client.post("/api/conversations", json={"title": unique_name}).json()
    monkeypatch.setattr(investigation, "find_signal_windows", _fake_windows)
    monkeypatch.setattr(investigation, "search_wiki", lambda *_args, **_kwargs: [])

    first = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": f"在 {dataset['serial']} 下，UEGO_rlamS1B1 这个信号过浓的原因是什么？"},
    )
    assert first.status_code == 200, first.text
    assert "如何定义" in first.json()["answer"]
    assert first.json()["meta"]["investigation"]["signal"] == "UEGO_rlamS1B1"

    second = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "低于 0.9"},
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert "发现 1 个窗口" in body["answer"]
    assert "暂不推断原因" in body["answer"]
    assert body["meta"]["investigation"]["status"] == "completed"
    assert any(source["kind"] == "window" for source in body["sources"])


def test_investigation_accepts_multiple_datasets_without_task_mode(client, unique_name, monkeypatch):
    first_dataset = client.post("/api/datasets", json={"name": unique_name + "-a", "kind": "mdf"}).json()
    second_dataset = client.post("/api/datasets", json={"name": unique_name + "-b", "kind": "mdf"}).json()
    conversation = client.post("/api/conversations", json={"title": unique_name}).json()
    monkeypatch.setattr(investigation, "find_signal_windows", _fake_windows)
    monkeypatch.setattr(investigation, "search_wiki", lambda *_args, **_kwargs: [])

    reply = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={
            "content": (
                f"分析 {first_dataset['serial']} 和 {second_dataset['serial']} 中 "
                "UEGO_rlamS1B1 信号低于 0.9 的异常原因"
            )
        },
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert first_dataset["serial"] in body["answer"]
    assert second_dataset["serial"] in body["answer"]
    assert len([source for source in body["sources"] if source["kind"] == "window"]) == 2
