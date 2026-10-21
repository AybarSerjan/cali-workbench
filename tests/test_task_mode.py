from __future__ import annotations

import backend.services.chat as chat
from backend.db import SessionLocal
from backend.models import Conversation, Message
from backend.services.chat import _extract_json_block, cancel_action, confirm_action, send_message
from sqlalchemy import select


def test_extract_json_block_parses_and_strips():
    text = '先看一下选项。\n```cali-ask\n{"question": "合并哪两个？", "options": ["MDF-0001", "MDF-0002"]}\n```'
    cleaned, data = _extract_json_block(text, "cali-ask")
    assert "cali-ask" not in cleaned and "先看一下选项" in cleaned
    assert data["question"] == "合并哪两个？" and data["options"] == ["MDF-0001", "MDF-0002"]


def test_extract_json_block_missing_or_invalid_returns_none():
    assert _extract_json_block("纯文本", "cali-ask") == ("纯文本", None)
    bad = "```cali-action\nnot json\n```"
    assert _extract_json_block(bad, "cali-action")[1] is None


def test_task_mode_ask_attaches_options(client, unique_name, monkeypatch):
    monkeypatch.setattr(chat, "chat_sync", lambda messages: '```cali-ask\n{"question": "要哪两个？", "options": ["MDF-0001", "MDF-0002"]}\n```')
    with SessionLocal() as session:
        conversation = Conversation(title=unique_name)
        session.add(conversation)
        session.commit()
        result = send_message(session, conversation, "合并文件", grill=True)
        assert result["meta"]["ask"]["options"] == ["MDF-0001", "MDF-0002"]
        assert not result["sources"]


def test_task_mode_action_confirm_executes(client, unique_name, monkeypatch):
    monkeypatch.setattr(chat, "chat_sync", lambda messages: '```cali-action\n{"action": "merge_mf4", "params": {"file_a": "MDF-0001", "file_b": "MDF-0002"}, "summary": "合并两个数据集"}\n```')
    monkeypatch.setattr(chat, "run_action", lambda session, action, params: {"summary": f"已执行 {action}", "serial": "MDF-9999"})
    with SessionLocal() as session:
        conversation = Conversation(title=unique_name)
        session.add(conversation)
        session.commit()
        result = send_message(session, conversation, "合并 MDF-0001 和 MDF-0002", grill=True)
        message_id = result["message_id"]
        assert result["meta"]["action"]["action"] == "merge_mf4"
        assert result["meta"]["status"] == "pending"

        confirmed = confirm_action(session, conversation, message_id)
        assert confirmed["answer"] == "已执行 merge_mf4"
        pending = session.get(Message, message_id)
        import json
        assert json.loads(pending.meta_json)["status"] == "confirmed"
        # A result message was appended.
        messages = session.scalars(select(Message).where(Message.conversation_id == conversation.id)).all()
        assert any(m.content == "已执行 merge_mf4" for m in messages)


def test_task_mode_action_cancel_marks_cancelled(client, unique_name, monkeypatch):
    monkeypatch.setattr(chat, "chat_sync", lambda messages: '```cali-action\n{"action": "merge_mf4", "params": {}, "summary": "合并"}\n```')
    with SessionLocal() as session:
        conversation = Conversation(title=unique_name)
        session.add(conversation)
        session.commit()
        result = send_message(session, conversation, "合并", grill=True)
        cancel_action(session, conversation, result["message_id"])
        import json
        assert json.loads(session.get(Message, result["message_id"]).meta_json)["status"] == "cancelled"
