from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.models import MemoryFact, WikiArticle, WikiEmbedding, WikiRevision


@dataclass
class SearchHit:
    kind: str
    id: str
    title: str
    content: str
    score: float
    path: str


def _fts_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{1,8}", query)
    return " OR ".join(f'"{term}"' for term in terms[:12])


def cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _relevant_excerpt(content: str, query: str, max_chars: int = 1800) -> tuple[str, int]:
    """Return a compact query-centred passage and its one-based start line."""
    lines = content.splitlines() or [content]
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{1,8}", query)]
    scores = [sum(len(term) for term in terms if term in line.lower()) for line in lines]
    best = max(range(len(lines)), key=lambda index: scores[index]) if any(scores) else 0
    start = max(0, best - 3)
    end = min(len(lines), best + 7)
    excerpt = "\n".join(lines[start:end]).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rsplit("\n", 1)[0] or excerpt[:max_chars]
    return excerpt, start + 1


def search_wiki(session: Session, query: str, vector: list[float] | None = None, limit: int = 5) -> list[SearchHit]:
    scores: dict[str, float] = {}
    fts = _fts_query(query)
    if fts:
        try:
            rows = session.execute(
                text("SELECT article_id, bm25(wiki_fts) AS rank FROM wiki_fts WHERE wiki_fts MATCH :q LIMIT 20"),
                {"q": fts},
            ).all()
            for article_id, rank in rows:
                scores[article_id] = max(scores.get(article_id, 0), 1.0 / (1.0 + abs(float(rank))))
        except Exception:
            session.rollback()
    if vector:
        for item in session.scalars(select(WikiEmbedding)).all():
            semantic = cosine(vector, json.loads(item.vector_json))
            scores[item.article_id] = max(scores.get(item.article_id, 0), semantic * 0.85)
    hits: list[SearchHit] = []
    for article_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]:
        article = session.get(WikiArticle, article_id)
        revision = session.get(WikiRevision, article.published_revision_id) if article else None
        if article and revision:
            content = revision.content or ""
            if content.lstrip().startswith("{"):
                from backend.services.wiki import page_from_content, page_search_text
                content = page_search_text(page_from_content(content))
            excerpt, line = _relevant_excerpt(content, query)
            hits.append(SearchHit("wiki", article.id, article.title, excerpt, score, f"wiki/{article.slug}.md#L{line}"))
    return hits


def search_memories(session: Session, query: str, limit: int = 5) -> list[SearchHit]:
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{1,8}", query)]
    facts = session.scalars(select(MemoryFact).where(MemoryFact.status.in_(["active", "disputed"]))).all()
    hits = []
    for fact in facts:
        haystack = f"{fact.key} {fact.value}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            hits.append(SearchHit("memory", fact.id, fact.key, fact.value, float(score), "memory"))
    return sorted(hits, key=lambda item: item.score, reverse=True)[:limit]

