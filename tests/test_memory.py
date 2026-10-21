from __future__ import annotations

from backend.db import SessionLocal
from backend.models import Conversation, ConversationSummary, MemoryFact, Message
from backend.services.chat import _capture_facts
from backend.services.memory import add_fact, compact_if_needed, context
from sqlalchemy import select


def test_ten_turn_window_compacts_when_eleventh_arrives(client, unique_name):
    with SessionLocal() as session:
        conversation = Conversation(title=unique_name)
        session.add(conversation)
        session.commit()
        for turn in range(1, 12):
            session.add(Message(conversation_id=conversation.id, turn_number=turn, role="user", content=f"问题 {turn}"))
            session.add(Message(conversation_id=conversation.id, turn_number=turn, role="assistant", content=f"回答 {turn}"))
        session.commit()
        summary = compact_if_needed(session, conversation.id)
        assert summary is not None
        assert (summary.start_turn, summary.end_turn) == (1, 10)
        current = context(session, conversation.id)
        assert current["summaries"][0]["end_turn"] == 10
        assert {item["turn"] for item in current["messages"]} == {2, 3, 4, 5, 6, 7, 8, 9, 10, 11}


def test_long_term_memory_is_append_only(client, unique_name):
    with SessionLocal() as session:
        first = add_fact(session, unique_name, "v1")
        second = add_fact(session, unique_name, "v2", supersedes_id=first.id)
        session.refresh(first)
        assert first.status == "superseded"
        assert second.status == "active"
        assert session.get(MemoryFact, first.id) is not None


def test_grill_capture_persists_facts_and_strips_block(client, unique_name):
    with SessionLocal() as session:
        conversation = Conversation(title=unique_name)
        session.add(conversation)
        session.commit()
        answer = (
            "推荐用 SQLite FTS5。你的文档量级大概多少？\n\n"
            "```cali-facts\n"
            f"术语：{unique_name} | 顾客提交的一次购买请求\n"
            "决策：全文检索用 SQLite FTS5 | 单用户、零额外依赖\n"
            "| 缺键的行应被忽略\n"
            "```"
        )
        cleaned, recorded = _capture_facts(session, conversation.id, 1, answer)
        assert "cali-facts" not in cleaned and "推荐用 SQLite FTS5" in cleaned
        assert recorded == [f"术语：{unique_name}", "决策：全文检索用 SQLite FTS5"]
        facts = session.scalars(select(MemoryFact).where(MemoryFact.conversation_id == conversation.id)).all()
        assert {f.key for f in facts} == set(recorded)
        # Idempotent: re-capturing the same block records nothing new.
        _, again = _capture_facts(session, conversation.id, 2, answer)
        assert again == []

