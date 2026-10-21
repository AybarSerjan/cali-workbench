import io
import tempfile
from pathlib import Path

import pytest

from agent.tools.signals import find_windows


# --- Deterministic core: no asammdf needed ---------------------------------

def test_find_windows_detects_runs_below_threshold():
    timestamps = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    samples = [1.0, 0.5, 0.8, 1.0, 0.4, 0.3]
    windows = find_windows(timestamps, samples, "<", 0.9)
    assert len(windows) == 2
    first, second = windows
    assert (first["start"], first["end"]) == (1.0, 2.0)
    assert first["samples"] == 2
    assert first["min"] == 0.5 and first["max"] == 0.8
    assert (second["start"], second["end"]) == (4.0, 5.0)


def test_find_windows_includes_trailing_run():
    # Condition still true on the final sample — the run must be closed/emitted.
    windows = find_windows([0.0, 1.0, 2.0], [1.0, 0.2, 0.1], "<", 0.9)
    assert len(windows) == 1
    assert windows[0]["end"] == 2.0


def test_find_windows_min_duration_filters_short_runs():
    timestamps = [0.0, 1.0, 2.0, 3.0]
    samples = [0.5, 1.0, 0.5, 0.5]  # a single-sample dip, then a 2-sample dip
    windows = find_windows(timestamps, samples, "<", 0.9, min_duration=0.5)
    assert len(windows) == 1
    assert windows[0]["start"] == 2.0 and windows[0]["end"] == 3.0


def test_find_windows_rejects_unknown_operator():
    with pytest.raises(ValueError):
        find_windows([0.0], [1.0], "≈", 0.9)


def test_find_windows_rejects_length_mismatch():
    with pytest.raises(ValueError):
        find_windows([0.0, 1.0], [1.0], "<", 0.9)


# --- End-to-end through the API (needs asammdf to author a real MF4) --------

def test_signal_window_endpoint(client, unique_name):
    asammdf = pytest.importorskip("asammdf")
    numpy = pytest.importorskip("numpy")

    signal = asammdf.Signal(
        samples=numpy.array([1.0, 0.5, 0.8, 1.0, 0.4]),
        timestamps=numpy.array([0.0, 1.0, 2.0, 3.0, 4.0]),
        name="Lambda",
        unit="-",
    )
    mdf = asammdf.MDF()
    mdf.append(signal)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.mf4"
        mdf.save(path, overwrite=True)
        payload = path.read_bytes()

    dataset = client.post("/api/datasets", json={"name": unique_name, "kind": "mdf"}).json()
    upload = client.post(
        f"/api/datasets/{dataset['id']}/files",
        files={"file": ("run.mf4", io.BytesIO(payload), "application/octet-stream")},
    )
    assert upload.status_code == 200, upload.text

    response = client.post(
        f"/api/datasets/{dataset['id']}/analysis/windows",
        json={"signal": "Lambda", "op": "<", "threshold": 0.9},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["window_count"] == 2
    assert body["windows"][0]["file"] == "run.mf4"
    assert (body["windows"][0]["start"], body["windows"][0]["end"]) == (1.0, 2.0)

    missing = client.post(
        f"/api/datasets/{dataset['id']}/analysis/windows",
        json={"signal": "DoesNotExist", "op": "<", "threshold": 0.9},
    )
    assert missing.status_code == 400
