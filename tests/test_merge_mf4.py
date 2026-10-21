from pathlib import Path

import pytest


def _upload(client, name: str, content: bytes) -> dict:
    response = client.post(
        "/api/datasets/files",
        files={"file": (name, content, "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_merge_datasets_passes_configured_signal_pairs(client, monkeypatch, unique_name):
    dataset_a = _upload(client, f"{unique_name}-dyno.mf4", f"{unique_name}-a".encode())
    dataset_b = _upload(client, f"{unique_name}-ecu.mf4", f"{unique_name}-b".encode())
    captured: dict = {}

    class FakeMerger:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def merge_files(self, file_a_path: str, file_b_path: str, output_dir: str) -> str:
            captured["inputs"] = (Path(file_a_path).name, Path(file_b_path).name)
            output = Path(output_dir) / "aligned.mf4"
            output.write_bytes(f"{unique_name}-merged".encode())
            return str(output)

    monkeypatch.setattr("agent.tools.mf4_merge.MF4Merger", FakeMerger)
    response = client.post(
        "/api/datasets/merge",
        json={
            "dataset_a_id": dataset_a["id"],
            "dataset_b_id": dataset_b["id"],
            "name": "Dyno_ECU_aligned",
            "signal_pairs": [
                {"signal_a": " TSPEED ", "signal_b": " VehV_v "},
                {"signal_a": "RollSpeed", "signal_b": "VehicleSpeed"},
            ],
        },
    )

    assert response.status_code == 200, response.text
    assert captured["signal_pairs"] == [
        ("TSPEED", "VehV_v"),
        ("RollSpeed", "VehicleSpeed"),
    ]
    assert captured["inputs"][0].endswith(f"{unique_name}-dyno.mf4")
    assert captured["inputs"][1].endswith(f"{unique_name}-ecu.mf4")
    assert response.json()["summary"]["source_file"] == "Dyno_ECU_aligned.mf4"


def test_merge_rejects_incomplete_signal_pair(client, unique_name):
    response = client.post(
        "/api/datasets/merge",
        json={
            "dataset_a_id": "dataset-a",
            "dataset_b_id": "dataset-b",
            "signal_pairs": [{"signal_a": "TSPEED", "signal_b": "   "}],
        },
    )

    assert response.status_code == 422


def test_custom_signal_pairs_do_not_silently_fallback(monkeypatch, tmp_path):
    import agent.tools.mf4_merge as merge_module

    class FakeMDF:
        def __init__(self, _path):
            pass

        def close(self):
            pass

    monkeypatch.setattr(merge_module, "MDF", FakeMDF)
    monkeypatch.setattr(merge_module, "_ASAMMDF_OK", True)
    monkeypatch.setattr(merge_module, "_SCIPY_OK", True)
    merger = merge_module.MF4Merger(signal_pairs=[("DynoSpeed", "VehicleSpeed")])
    monkeypatch.setattr(merger, "_channels", lambda _mdf: ["UnrelatedSignal"])
    monkeypatch.setattr(merger, "_find_best_signal_pair", lambda _a, _b: None)
    file_a = tmp_path / "a.mf4"
    file_b = tmp_path / "b.mf4"
    file_a.write_bytes(b"a")
    file_b.write_bytes(b"b")

    with pytest.raises(ValueError, match="配置的信号对无法完成时间对齐"):
        merger.merge_files(str(file_a), str(file_b), output_dir=str(tmp_path))
