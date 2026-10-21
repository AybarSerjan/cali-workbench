from __future__ import annotations

import io
import json
import zipfile

from backend.services.wiki import page_from_content, parse_okf_document, serialize_page


def _evidence_text(page) -> str:
    return " ".join(item if isinstance(item, str) else item.get("text", "") for item in page["evidence"])


def test_page_content_round_trips():
    page = {
        "title": "T", "status": "review", "summary": "s", "claims": ["a", "b"],
        "evidence": [{"source": "src", "page": "3", "section": "", "text": "x"}, "plain"], "links": ["L1"],
    }
    once = page_from_content(serialize_page(page))
    assert page_from_content(serialize_page(once)) == once        # normalization is idempotent
    assert once["claims"] == ["a", "b"] and once["evidence"][1] == "plain" and once["links"] == ["L1"]
    md = page_from_content("---\ntitle: M\n---\n\n# M\n\n## Summary\n\nhello\n\n## Claims\n\n- c1\n\n## Links\n\n- [[Other]]\n")
    assert md["title"] == "M" and md["summary"] == "hello" and md["claims"] == ["c1"] and md["links"] == ["Other"]


def test_source_draft_edit_publish_and_conflict(client, unique_name):
    source = client.post(
        "/api/sources",
        files={"file": (f"{unique_name}.md", b"# Engine\nTorque is measured in Nm.\n", "text/markdown")},
    )
    assert source.status_code == 200
    source_data = source.json()
    assert len(source_data["sha256"]) == 64

    draft = client.post("/api/wiki/drafts", json={"source_id": source_data["id"], "title": unique_name})
    assert draft.status_code == 200, draft.text
    revision = draft.json()["revisions"][0]
    page = revision["page"]
    assert page["title"] == unique_name
    assert page["summary"]
    assert isinstance(page["claims"], list) and isinstance(page["evidence"], list)
    assert "Torque" in _evidence_text(page)                       # MarkItDown 转换保留了正文

    edited_page = {**page, "claims": ["Torque is measured in Nm."], "links": ["Engine"], "status": "review"}
    edited = client.put(f"/api/wiki/revisions/{revision['id']}", json={"content": json.dumps(edited_page)})
    assert edited.status_code == 200
    newest = edited.json()["revisions"][0]
    assert newest["number"] == revision["number"] + 1
    assert newest["page"]["claims"] == ["Torque is measured in Nm."]

    published = client.post(
        f"/api/wiki/revisions/{newest['id']}/publish",
        json={"expected_base_sha256": newest["base_sha256"]},
    )
    assert published.status_code == 200
    assert published.json()["published_revision_id"] == newest["id"]

    conflict = client.post(
        f"/api/wiki/revisions/{revision['id']}/publish",
        json={"expected_base_sha256": revision["base_sha256"]},
    )
    assert conflict.status_code == 409


def test_llm_drafts_claims_but_evidence_always_comes_from_source(client, unique_name, monkeypatch):
    from backend.services import wiki

    monkeypatch.setattr(wiki, "_llm_page", lambda title, markdown: {
        "title": title, "status": "review", "summary": "LLM summary",
        "claims": ["LLM claim"], "evidence": ["invented evidence"], "links": [],
    })
    source = client.post(
        "/api/sources",
        files={"file": (f"{unique_name}.md", b"Original source fact.", "text/markdown")},
    ).json()
    page = client.post("/api/wiki/drafts", json={"source_id": source["id"], "title": unique_name}).json()["revisions"][0]["page"]

    assert page["claims"] == ["LLM claim"]
    assert "Original source fact." in _evidence_text(page)
    assert "invented evidence" not in _evidence_text(page)
    assert any(item.get("section") == "line 1" for item in page["evidence"] if isinstance(item, dict))


def test_html_source_converts_to_structured_page(client, unique_name):
    term = f"Widget{unique_name}"
    html = f"<html><body><h1>Report</h1><p>{term} 是核心组件，用于标定。</p></body></html>".encode()
    source = client.post(
        "/api/sources",
        files={"file": (f"{unique_name}.html", html, "text/html")},
    )
    assert source.status_code == 200, source.text

    draft = client.post("/api/wiki/drafts", json={"source_id": source.json()["id"], "title": unique_name})
    assert draft.status_code == 200, draft.text
    page = draft.json()["revisions"][0]["page"]
    assert page["summary"] and page["evidence"]                   # 概述 + 证据存在
    assert term in f"{page['summary']} {_evidence_text(page)}"    # MarkItDown 转换保留了正文


def test_publish_upgrades_legacy_markdown_and_rejects_bad_okf(client, unique_name):
    source = client.post(
        "/api/sources",
        files={"file": (f"{unique_name}.md", b"legacy evidence", "text/markdown")},
    ).json()
    article = client.post("/api/wiki/drafts", json={"source_id": source["id"], "title": unique_name}).json()
    revision = article["revisions"][0]
    legacy = f"# {unique_name}\n\nLegacy body.\n\n> Source SHA256: {source['sha256']}\n"
    edited = client.put(f"/api/wiki/revisions/{revision['id']}", json={"content": legacy}).json()
    published = client.post(
        f"/api/wiki/revisions/{edited['revisions'][0]['id']}/publish",
        json={"expected_base_sha256": ""},
    )
    assert published.status_code == 200, published.text
    frontmatter, body = parse_okf_document(published.json()["revisions"][0]["content"])
    assert frontmatter["type"] == "CALI Wiki Article"
    assert frontmatter["title"] == unique_name
    assert "Legacy body" in body

    next_draft = client.post("/api/wiki/drafts", json={"source_id": source["id"], "title": unique_name}).json()["revisions"][0]
    invalid = client.put(
        f"/api/wiki/revisions/{next_draft['id']}",
        json={"content": "---\ntype: [broken\n---\n\nbody"},
    ).json()["revisions"][0]
    rejected = client.post(
        f"/api/wiki/revisions/{invalid['id']}/publish",
        json={"expected_base_sha256": invalid["base_sha256"]},
    )
    assert rejected.status_code == 400


def test_okf_bundle_import_export_and_path_guard(client, unique_name):
    concept_id = f"procedures/{unique_name}"
    document = (
        "---\n"
        "type: Test Procedure\n"
        f"title: {unique_name}\n"
        "description: Imported procedure.\n"
        "tags: [test]\n"
        "timestamp: 2026-06-30T00:00:00Z\n"
        "x-cali-field: preserved\n"
        "---\n\n"
        "# Procedure\n\nImported body.\n"
    )
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("index.md", '# Test Procedure\n\n* [Imported](procedures/item.md)\n')
        archive.writestr(f"{concept_id}.md", document)
        archive.writestr("viz.html", "ignored")

    imported = client.post(
        "/api/wiki/import",
        files={"file": ("bundle.zip", bundle.getvalue(), "application/zip")},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["imported"] == 1
    article = imported.json()["articles"][0]
    assert article["slug"] == concept_id
    revision = article["revisions"][0]
    assert parse_okf_document(revision["content"])[0]["x-cali-field"] == "preserved"

    published = client.post(
        f"/api/wiki/revisions/{revision['id']}/publish",
        json={"expected_base_sha256": revision["base_sha256"]},
    )
    assert published.status_code == 200, published.text
    exported = client.get("/api/wiki/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert f"{concept_id}.md" in archive.namelist()
        exported_frontmatter, _ = parse_okf_document(archive.read(f"{concept_id}.md").decode())
    assert exported_frontmatter["x-cali-field"] == "preserved"

    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("../escape.md", document)
    rejected = client.post(
        "/api/wiki/import",
        files={"file": ("bad.zip", malicious.getvalue(), "application/zip")},
    )
    assert rejected.status_code == 400


def test_link_source_drafts_publishes_and_is_searchable(client, unique_name):
    from backend.db import SessionLocal
    from backend.services.retrieval import search_wiki

    term = f"Zorblax{unique_name}"
    url = "https://notebooklm.google.com/notebook/abc123"
    source = client.post(
        "/api/sources/link",
        json={"url": url, "title": unique_name, "text": f"# 概念\n{term} 是一个外部知识要点。\n"},
    )
    assert source.status_code == 200, source.text
    source_data = source.json()
    assert source_data["source_url"] == url
    assert source_data["suffix"] == ".md"

    draft = client.post("/api/wiki/drafts", json={"source_id": source_data["id"], "title": unique_name})
    assert draft.status_code == 200, draft.text
    article = draft.json()
    revision = article["revisions"][0]
    assert url in revision["content"]          # 出处链接进入正文
    assert term in revision["content"]          # 粘贴正文作为证据

    published = client.post(
        f"/api/wiki/revisions/{revision['id']}/publish",
        json={"expected_base_sha256": revision["base_sha256"]},
    )
    assert published.status_code == 200, published.text

    with SessionLocal() as session:
        hits = search_wiki(session, term)
    assert any(hit.id == article["id"] for hit in hits)  # 已进检索
    hit = next(hit for hit in hits if hit.id == article["id"])
    assert term in hit.content
    assert "#L" in hit.path


def test_link_source_rejects_bad_url(client, unique_name):
    bad = client.post("/api/sources/link", json={"url": "ftp://x", "title": unique_name, "text": "x"})
    assert bad.status_code == 422  # pydantic pattern rejects non-http(s)


def test_source_hash_deduplicates(client, unique_name):
    content = f"same-{unique_name}".encode()
    first = client.post("/api/sources", files={"file": (f"{unique_name}.md", content, "text/markdown")}).json()
    second = client.post("/api/sources", files={"file": (f"other-{unique_name}.md", content, "text/markdown")}).json()
    assert first["id"] == second["id"]

