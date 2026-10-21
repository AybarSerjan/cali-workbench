from __future__ import annotations

import yaml


def _definition(*, confirmed: bool = True, approved_threshold: bool = True) -> dict:
    return {
        "description": "Lambda tracking validation",
        "signals": [
            {
                "role": "measured",
                "a2l_name": "UEGO_rLamS1B1",
                "match_status": "confirmed" if confirmed else "proposed",
            }
        ],
        "scenario_gate": "nmot_w > 1000",
        "criteria": [
            {
                "expression": "abs(UEGO_rLamS1B1 - lamsbg_w_msg) > 0.05",
                "min_duration_s": 0.3,
                "threshold_source": "HUMAN_APPROVED" if approved_threshold else "AGENT_PROPOSED",
            }
        ],
    }


def test_revision_review_and_approve_state_machine(client, unique_name):
    created = client.post(
        "/api/workpackages",
        json={"wp_code": f"WP-{unique_name}", "title": unique_name, "definition": _definition()},
    )
    assert created.status_code == 200, created.text
    wp = created.json()
    assert wp["revisions"][0]["status"] == "draft"

    rejected = client.post(f"/api/workpackages/{wp['id']}/approve")
    assert rejected.status_code == 409

    reviewed = client.post(
        f"/api/workpackages/{wp['id']}/review",
        json={"review_notes": ["checked against test procedure"]},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["revisions"][0]["status"] == "reviewed"

    approved = client.post(f"/api/workpackages/{wp['id']}/approve")
    assert approved.status_code == 200, approved.text
    result = approved.json()
    assert result["approved_revision_id"] == result["revisions"][0]["id"]
    assert result["revisions"][0]["status"] == "approved"

    updated = client.put(
        f"/api/workpackages/{wp['id']}/revision",
        json={"definition": _definition(), "title": f"{unique_name} v2"},
    )
    assert updated.status_code == 200, updated.text
    assert [r["status"] for r in updated.json()["revisions"][:2]] == ["draft", "approved"]
    assert updated.json()["approved_revision_id"] == result["approved_revision_id"]


def test_approve_requires_confirmed_signals_and_human_thresholds(client, unique_name):
    created = client.post(
        "/api/workpackages",
        json={
            "wp_code": f"WP-BLOCK-{unique_name}",
            "title": unique_name,
            "definition": _definition(confirmed=False, approved_threshold=False),
        },
    ).json()
    assert client.post(f"/api/workpackages/{created['id']}/review", json={}).status_code == 200
    rejected = client.post(f"/api/workpackages/{created['id']}/approve")
    assert rejected.status_code == 409
    assert "confirmed" in rejected.json()["detail"]
    assert "HUMAN_APPROVED" in rejected.json()["detail"]


def test_yaml_export_import_creates_drafts(client, unique_name):
    code = f"WP-YAML-{unique_name}"
    created = client.post(
        "/api/workpackages",
        json={"wp_code": code, "title": unique_name, "definition": _definition()},
    )
    assert created.status_code == 200

    exported = client.get("/api/workpackages/export")
    assert exported.status_code == 200
    document = yaml.safe_load(exported.content)
    entry = next(item for item in document["workpackages"] if item["wp_code"] == code)
    assert entry["definition"] == _definition()

    entry["title"] = f"{unique_name} imported"
    subset = yaml.safe_dump(
        {"schema_version": 1, "workpackages": [entry]},
        sort_keys=False,
        allow_unicode=True,
    ).encode()
    imported = client.post(
        "/api/workpackages/import",
        files={"file": ("workpackages.yaml", subset, "application/yaml")},
    )
    assert imported.status_code == 200, imported.text
    result = imported.json()["workpackages"][0]
    assert result["title"].endswith(" imported")
    assert result["revisions"][0]["number"] == 2
    assert result["revisions"][0]["status"] == "draft"


def test_llm_draft_degrades_explicitly_when_disabled(client, unique_name):
    response = client.post("/api/workpackages/draft", json={
        "wp_code": f"WP-LLM-{unique_name}", "title": unique_name, "prompt": "validate lambda tracking"
    })
    assert response.status_code == 501
    assert "disabled" in response.json()["detail"]
