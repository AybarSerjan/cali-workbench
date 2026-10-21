from __future__ import annotations

import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent.graph import graph
from backend.db import utcnow
from backend.models import Conversation, Message, MemoryFact, Run
from backend.services.actions import catalog_brief, describe_actions, run_action
from backend.services.llm import chat_sync
from backend.services.investigation import handle_investigation
from backend.services.memory import add_fact, compact_if_needed, context
from backend.services.runs import create_run, update_run

# Task/agent mode: instead of answering, CALI helps the user carry out an
# operation. It asks one clarifying question at a time (offering clickable
# options), then proposes a structured, executable action and waits for the
# user's explicit confirmation before anything runs.
TASK_SYSTEM = (
    "你是 CALI 的任务助手。用户会描述一个想完成的操作；你的职责是问清必要信息，"
    "然后提出一个可执行的操作让用户确认——你自己绝不直接执行。\n"
    "可执行的操作（只有这些能真正运行）：\n{actions}\n"
    "当前可用的 MDF 数据集（编号=名称）：\n{catalog}\n"
    "工作规则：\n"
    "- 一次只问一个问题。缺少必要信息时，输出一个 ```cali-ask 代码块（合法 JSON），形如：\n"
    "  {{\"question\": \"要合并哪两个数据集？\", \"options\": [\"MDF-0001\", \"MDF-0002\"]}}\n"
    "  options 给出可点击的候选项，必须使用真实存在的数据集编号。代码块外可写一句话说明。\n"
    "- 当信息齐全、可以执行某个「可执行操作」时，输出一个 ```cali-action 代码块（合法 JSON），形如：\n"
    "  {{\"action\": \"merge_mf4\", \"params\": {{\"file_a\": \"MDF-0001\", \"file_b\": \"MDF-0002\"}}, "
    "\"summary\": \"合并 MDF-0001 与 MDF-0002 为新数据集\"}}\n"
    "  然后停下，等用户确认；不要假装已经执行。\n"
    "- 如果用户的任务不在可执行操作清单内，就不要输出 cali-action，而是用自然语言给出清晰的分步计划。\n"
    "- 每条回复最多包含一个 cali-ask 或一个 cali-action 代码块。\n"
    "- 绝不要声称「已完成/已执行」——真正的执行由系统在用户确认后完成。"
)

_FACTS_BLOCK = re.compile(r"```cali-facts\s*\n(.*?)```", re.DOTALL)


def _auto_title(content: str) -> str:
    title = " ".join(content.split())
    return title[:48] or "新对话"


def _is_default_title(title: str) -> bool:
    value = (title or "").strip()
    return not value or value in {"新对话", "CALI 知识问答"} or value.startswith("CALI ")


def _extract_json_block(text: str, name: str) -> tuple[str, dict | None]:
    """Pull a ```{name} JSON code block out of the reply. Returns (cleaned_text, data).
    On missing block or invalid JSON, returns the original text and None."""
    match = re.search(rf"```{name}\s*\n(.*?)```", text, re.DOTALL)
    if not match:
        return text, None
    try:
        data = json.loads(match.group(1).strip())
    except (json.JSONDecodeError, ValueError):
        return text, None
    if not isinstance(data, dict):
        return text, None
    cleaned = (text[: match.start()] + text[match.end():]).strip()
    return cleaned, data


def _capture_facts(session: Session, conversation_id: str, turn: int, answer: str) -> tuple[str, list[str]]:
    """Pull the ```cali-facts block out of the reply, persist new facts, return cleaned answer + recorded keys."""
    match = _FACTS_BLOCK.search(answer)
    if not match:
        return answer, []
    recorded: list[str] = []
    for line in match.group(1).splitlines():
        if "|" not in line:
            continue
        key, value = (part.strip() for part in line.split("|", 1))
        if not key or not value:
            continue
        if session.scalar(select(MemoryFact).where(MemoryFact.key == key, MemoryFact.value == value, MemoryFact.status == "active")):
            continue
        add_fact(session, key, value, conversation_id=conversation_id, source_turn=turn)
        recorded.append(key)
    return answer[: match.start()].rstrip() + answer[match.end():], recorded


def _task_turn(session: Session, content: str, memory_context: dict) -> tuple[str, dict]:
    """One turn of task/agent mode: returns (answer_text, meta) where meta may carry
    a clarifying 'ask' (with options) or a proposed 'action' awaiting confirmation."""
    system = TASK_SYSTEM.format(actions=describe_actions(), catalog=catalog_brief(session))
    messages = [{"role": "system", "content": system}]
    for item in memory_context["messages"]:
        messages.append({"role": "assistant" if item["role"] == "assistant" else "user", "content": item["content"]})
    messages.append({"role": "user", "content": content})
    answer = chat_sync(messages)
    if not answer:
        return "任务模式需要启用生成模型（设置 → 模型连接），当前为禁用/确定性模式。", {}

    answer, action = _extract_json_block(answer, "cali-action")
    if action and action.get("action"):
        meta = {
            "action": {
                "action": str(action.get("action")),
                "params": action.get("params") if isinstance(action.get("params"), dict) else {},
                "summary": str(action.get("summary") or ""),
            },
            "status": "pending",
        }
        if not answer:
            answer = meta["action"]["summary"] or "请确认是否执行该操作。"
        return answer, meta

    answer, ask = _extract_json_block(answer, "cali-ask")
    if ask and ask.get("question"):
        options = [str(value) for value in (ask.get("options") or []) if str(value).strip()]
        meta = {"ask": {"question": str(ask.get("question")), "options": options}}
        if not answer:
            answer = meta["ask"]["question"]
        return answer, meta

    return answer or "（无内容）", {}


def send_message(session: Session, conversation: Conversation, content: str, grill: bool = False) -> dict:
    last_turn = session.scalar(select(func.max(Message.turn_number)).where(Message.conversation_id == conversation.id)) or 0
    turn = last_turn + 1
    memory_context = context(session, conversation.id)
    run = create_run(session, "task" if grill else "chat", conversation_id=conversation.id)
    if last_turn == 0 and _is_default_title(conversation.title):
        conversation.title = _auto_title(content)
    conversation.updated_at = utcnow()
    user = Message(conversation_id=conversation.id, turn_number=turn, role="user", content=content)
    session.add(user)
    session.commit()
    try:
        meta: dict = {}
        if grill:
            answer, meta = _task_turn(session, content, memory_context)
            sources: list = []
        else:
            result = handle_investigation(session, content, memory_context["messages"], run.id)
            if result is None:
                result = graph.invoke(
                    {
                        "conversation_id": conversation.id,
                        "run_id": run.id,
                        "question": content,
                        "summaries": memory_context["summaries"],
                        "recent_messages": memory_context["messages"],
                    },
                    {"configurable": {"thread_id": conversation.id}},
                )
            answer = result.get("answer", "没有生成回答。")
            sources = result.get("sources", [])
            meta = result.get("meta", {})
        assistant = Message(
            conversation_id=conversation.id,
            turn_number=turn,
            role="assistant",
            content=answer,
            sources_json=json.dumps(sources, ensure_ascii=False),
            meta_json=json.dumps(meta, ensure_ascii=False),
        )
        session.add(assistant)
        compact_if_needed(session, conversation.id)
        update_run(session, run, status="completed", node="completed", artifacts=result.get("artifacts", []) if not grill else [])
        conversation.updated_at = utcnow()
        session.commit()
        return {"run_id": run.id, "turn": turn, "answer": answer, "sources": sources, "meta": meta, "message_id": assistant.id}
    except Exception as exc:
        update_run(session, run, status="failed", node="failed", error=str(exc))
        raise


def confirm_action(session: Session, conversation: Conversation, message_id: str) -> dict:
    """Execute the pending action stored on an assistant message, then append a
    result message. Idempotent: a non-pending action is rejected."""
    message = session.get(Message, message_id)
    if not message or message.conversation_id != conversation.id:
        raise ValueError("消息不存在")
    meta = json.loads(message.meta_json or "{}")
    action = meta.get("action")
    if not action or meta.get("status") != "pending":
        raise ValueError("该消息没有待确认的操作")

    run = create_run(session, "task", conversation_id=conversation.id)
    try:
        result = run_action(session, action["action"], action.get("params") or {})
        meta["status"] = "confirmed"
        message.meta_json = json.dumps(meta, ensure_ascii=False)
        last_turn = session.scalar(select(func.max(Message.turn_number)).where(Message.conversation_id == conversation.id)) or 0
        turn = last_turn + 1
        summary = result.get("summary", "操作已完成。")
        assistant = Message(
            conversation_id=conversation.id,
            turn_number=turn,
            role="assistant",
            content=summary,
            meta_json=json.dumps({"result": result}, ensure_ascii=False),
        )
        conversation.updated_at = utcnow()
        session.add(assistant)
        update_run(session, run, status="completed", node="completed")
        return {"run_id": run.id, "turn": turn, "answer": summary, "result": result, "message_id": assistant.id}
    except Exception as exc:
        update_run(session, run, status="failed", node="failed", error=str(exc))
        raise


def cancel_action(session: Session, conversation: Conversation, message_id: str) -> dict:
    message = session.get(Message, message_id)
    if not message or message.conversation_id != conversation.id:
        raise ValueError("消息不存在")
    meta = json.loads(message.meta_json or "{}")
    if not meta.get("action") or meta.get("status") != "pending":
        raise ValueError("该消息没有待确认的操作")
    meta["status"] = "cancelled"
    message.meta_json = json.dumps(meta, ensure_ascii=False)
    conversation.updated_at = utcnow()
    session.commit()
    return {"ok": True, "message_id": message_id}

