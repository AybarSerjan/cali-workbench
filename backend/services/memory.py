from __future__ import annotations

import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import ConversationSummary, MemoryFact, Message


WINDOW_TURNS = 10


def add_fact(
    session: Session,
    key: str,
    value: str,
    *,
    conversation_id: str | None = None,
    supersedes_id: str | None = None,
    source_turn: int | None = None,
) -> MemoryFact:
    if supersedes_id:
        previous = session.get(MemoryFact, supersedes_id)
        if not previous:
            raise ValueError("被替代的记忆不存在")
        previous.status = "superseded"
    fact = MemoryFact(
        key=key,
        value=value,
        conversation_id=conversation_id,
        supersedes_id=supersedes_id,
        source_turn=source_turn,
    )
    session.add(fact)
    session.commit()
    session.refresh(fact)
    return fact


def _one_sentence(messages: list[Message]) -> str:
    user_text = "；".join(" ".join(item.content.split())[:80] for item in messages if item.role == "user")
    return f"本段对话围绕：{user_text[:420]}。" if user_text else "本段对话未包含可摘要的用户内容。"


def extract_facts(session: Session, messages: list[Message]) -> list[MemoryFact]:
    created: list[MemoryFact] = []
    patterns = (
        re.compile(r"我的(?P<key>[\u4e00-\u9fffA-Za-z0-9_]{1,24})(?:是|为|叫)(?P<value>[^。！？\n]{1,120})"),
        re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_ .-]{0,40})\s*=\s*(?P<value>[^\n]{1,120})"),
    )
    for message in messages:
        if message.role != "user":
            continue
        for pattern in patterns:
            for match in pattern.finditer(message.content):
                key, value = match.group("key").strip(), match.group("value").strip()
                duplicate = session.scalar(select(MemoryFact).where(MemoryFact.key == key, MemoryFact.value == value, MemoryFact.status == "active"))
                if not duplicate:
                    fact = MemoryFact(key=key, value=value, conversation_id=message.conversation_id, source_turn=message.turn_number)
                    session.add(fact)
                    created.append(fact)
    session.commit()
    return created


def compact_if_needed(session: Session, conversation_id: str) -> ConversationSummary | None:
    max_turn = session.scalar(select(func.max(Message.turn_number)).where(Message.conversation_id == conversation_id)) or 0
    if max_turn <= WINDOW_TURNS:
        return None
    completed_end = ((max_turn - 1) // WINDOW_TURNS) * WINDOW_TURNS
    start = completed_end - WINDOW_TURNS + 1
    existing = session.scalar(
        select(ConversationSummary).where(
            ConversationSummary.conversation_id == conversation_id,
            ConversationSummary.start_turn == start,
            ConversationSummary.end_turn == completed_end,
        )
    )
    if existing:
        return None
    messages = session.scalars(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.turn_number >= start,
            Message.turn_number <= completed_end,
        ).order_by(Message.turn_number, Message.created_at)
    ).all()
    summary = ConversationSummary(
        conversation_id=conversation_id,
        start_turn=start,
        end_turn=completed_end,
        summary=_one_sentence(messages),
    )
    session.add(summary)
    extract_facts(session, list(messages))
    session.commit()
    return summary


def consolidate_conversation(session: Session, conversation_id: str) -> list[MemoryFact]:
    messages = session.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.turn_number)).all()
    return extract_facts(session, list(messages))


def context(session: Session, conversation_id: str) -> dict:
    max_turn = session.scalar(select(func.max(Message.turn_number)).where(Message.conversation_id == conversation_id)) or 0
    first_recent = max(1, max_turn - WINDOW_TURNS + 1)
    messages = session.scalars(
        select(Message).where(Message.conversation_id == conversation_id, Message.turn_number >= first_recent).order_by(Message.turn_number, Message.created_at)
    ).all()
    summaries = session.scalars(
        select(ConversationSummary).where(ConversationSummary.conversation_id == conversation_id).order_by(ConversationSummary.start_turn)
    ).all()
    return {
        "summaries": [{"start_turn": x.start_turn, "end_turn": x.end_turn, "summary": x.summary} for x in summaries],
        "messages": [
            {
                "id": x.id,
                "turn": x.turn_number,
                "role": x.role,
                "content": x.content,
                "sources": json.loads(x.sources_json or "[]"),
                "meta": json.loads(x.meta_json or "{}"),
            }
            for x in messages
        ],
    }
