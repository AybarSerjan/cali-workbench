"""Seed an isolated workspace through CALI's real APIs using synthetic inputs only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from generate_demo import generate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1] / ".demo-workspace")
    parser.add_argument("--approve-demo-rule", action="store_true",
                        help="Approve only the invented 0.05 / 0.5 s rule in the new synthetic workspace.")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if workspace.exists() and any(workspace.iterdir()):
        raise SystemExit("Choose an empty demo workspace. Existing workspaces are never changed.")
    inputs = workspace / "demo-inputs"
    generate(inputs)
    os.environ["CALI_WORKSPACE_ROOT"] = str(workspace)
    os.environ["CALI_LLM_PROVIDER"] = "disabled"
    os.environ["CALI_WIKI_AGENT_PROVIDER"] = "disabled"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fastapi.testclient import TestClient
    from backend.main import app

    def checked(response):
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Demo API failed ({response.status_code}): {response.text[:500]}")
        return response.json()

    with TestClient(app) as client:
        source = checked(client.post("/api/sources", files={"file": (
            "synthetic_source.md", (inputs / "synthetic_source.md").read_bytes(), "text/markdown")}))
        article = checked(client.post("/api/wiki/drafts", json={
            "source_id": source["id"], "title": "Synthetic lambda example", "slug": "synthetic-lambda-example"}))
        # The wiki remains a draft. No knowledge is published by this script.
        calibration = checked(client.post("/api/datasets", json={"name": "SYNTHETIC A2L", "kind": "calibration"}))
        calibration = checked(client.post(f"/api/datasets/{calibration['id']}/files", files={"file": (
            "synthetic_measurements.a2l", (inputs / "synthetic_measurements.a2l").read_bytes(), "text/plain")}))
        a2l = next(file for file in calibration["files"] if file["suffix"] == ".a2l")
        rule = json.loads((inputs / "workpackage.json").read_text(encoding="utf-8"))
        if args.approve_demo_rule:
            for criterion in rule["definition"]["criteria"]:
                criterion["threshold_source"] = "HUMAN_APPROVED"
        wp = checked(client.post("/api/workpackages", json=rule))
        checked(client.post(f"/api/workpackages/{wp['id']}/review", json={"review_notes": ["Toy rule; isolated software demo only."]}))
        if args.approve_demo_rule:
            checked(client.post(f"/api/workpackages/{wp['id']}/approve"))
        evaluations = []
        for entry in json.loads((inputs / "expected.json").read_text(encoding="utf-8"))["measurements"]:
            path = inputs / entry["file"]
            dataset = checked(client.post("/api/datasets/files", files={"file": (
                path.name, path.read_bytes(), "application/octet-stream")}, data={"kind": "mdf"}))
            evaluation = checked(client.post(f"/api/workpackages/{wp['id']}/evaluate", json={
                "dataset_id": dataset["id"], "a2l_file_id": a2l["id"]}))
            actual = evaluation["summary"]["overall"]
            expected_verdict = entry["expected_verdict"] if args.approve_demo_rule else "OBSERVATION"
            if actual != expected_verdict:
                raise AssertionError(f"{path.name}: expected {expected_verdict}, got {actual}")
            windows = evaluation["summary"]["criteria"][0]["windows"]
            expected_window = entry["expected_anomaly_window_s"]
            if expected_window:
                if len(windows) != 1 or any(abs(windows[0][key] - value) > 1e-6
                                           for key, value in zip(("start", "end"), expected_window)):
                    raise AssertionError(f"{path.name}: injected anomaly window was not recovered")
            elif windows:
                raise AssertionError(f"{path.name}: unexpected anomaly")
            evaluations.append({"file": path.name, "verdict": actual, "summary": evaluation["summary"]})
        result = {"synthetic": True, "demo_rule_approved": args.approve_demo_rule,
                  "wiki_published": False, "wiki_article_id": article["id"],
                  "pass": sum(e["verdict"] == "PASS" for e in evaluations),
                  "fail": sum(e["verdict"] == "FAIL" for e in evaluations),
                  "observation": sum(e["verdict"] == "OBSERVATION" for e in evaluations),
                  "anomaly_files": sum(bool(e["summary"]["criteria"][0]["windows"]) for e in evaluations),
                  "evaluations": evaluations}
        (workspace / "demo_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Synthetic demo verified: {result['pass']} PASS, {result['fail']} FAIL, "
              f"{result['observation']} OBSERVATION; {result['anomaly_files']} injected anomaly; wiki remains a draft.")
        print(f"Isolated workspace: {workspace}")


if __name__ == "__main__":
    main()
