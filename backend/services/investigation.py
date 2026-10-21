"""Conversational, evidence-first signal investigation.

This module owns the small amount of dialogue state needed to turn a question
such as "why is Lambda rich?" into a deterministic window search followed by
Wiki-grounded interpretation.  State is stored in assistant message metadata,
so it remains auditable and requires no mutable hidden agent state.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from agent.tools.catalog import SERIAL_RE, get_dataset
from backend.services.actions import available_mdf_datasets
from backend.services.analysis import find_signal_windows
from backend.services.llm import chat_sync, embed_sync
from backend.services.retrieval import search_wiki
from backend.services.runs import write_agent_artifact


CAUSE_TERMS = ("原因", "为什么", "为何", "过浓", "过稀", "异常", "偏高", "偏低")


def _dataset_refs(text: str) -> list[str]:
    return list(dict.fromkeys(match.group().upper() for match in SERIAL_RE.finditer(text)))


def _signal_name(text: str) -> str | None:
    patterns = (
        r"(?P<signal>[A-Za-z_][A-Za-z0-9_./:-]{2,})\s*(?:这个)?信号",
        r"(?:信号|通道)\s*(?P<signal>[A-Za-z_][A-Za-z0-9_./:-]{2,})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match and not SERIAL_RE.fullmatch(match.group("signal")):
            return match.group("signal")
    candidates = [
        token for token in re.findall(r"[A-Za-z_][A-Za-z0-9_./:-]{3,}", text)
        if not SERIAL_RE.fullmatch(token) and token.lower() not in {"mf4", "mdf", "wiki"}
    ]
    return max(candidates, key=len) if candidates else None


def _rule(text: str) -> tuple[str, float] | None:
    symbolic = re.search(r"(?:<=|>=|==|!=|<|>)\s*(-?\d+(?:\.\d+)?)", text)
    if symbolic:
        op = re.search(r"<=|>=|==|!=|<|>", symbolic.group()).group()
        return op, float(symbolic.group(1))
    mappings = (
        (r"(?:不高于|小于等于|至多)\s*(-?\d+(?:\.\d+)?)", "<="),
        (r"(?:不低于|大于等于|至少)\s*(-?\d+(?:\.\d+)?)", ">="),
        (r"(?<!不)(?:低于|小于|少于|不足)\s*(-?\d+(?:\.\d+)?)", "<"),
        (r"(?<!不)(?:高于|大于|超过)\s*(-?\d+(?:\.\d+)?)", ">"),
    )
    for pattern, op in mappings:
        match = re.search(pattern, text)
        if match:
            return op, float(match.group(1))
    return None


def _duration(text: str) -> float:
    match = re.search(r"持续\s*(\d+(?:\.\d+)?)\s*(?:s|秒)", text, re.IGNORECASE)
    return float(match.group(1)) if match else 0.0


def _is_investigation(text: str) -> bool:
    return bool(_signal_name(text) and any(term in text for term in CAUSE_TERMS))


def _ask(session: Session, request: dict[str, Any]) -> dict[str, Any]:
    if not request.get("datasets"):
        options = [item["serial"] for item in available_mdf_datasets(session)]
        question = "要分析哪个数据集？可以选择一个或多个。"
        return {
            "answer": question,
            "sources": [],
            "meta": {"ask": {"question": question, "options": options}, "investigation": request},
        }
    if request.get("op") is None or request.get("threshold") is None:
        signal = request["signal"]
        question = f"你如何定义 {signal} 的这个异常？例如“低于 0.9”或“高于 1.1”。"
        return {
            "answer": question,
            "sources": [],
            "meta": {"ask": {"question": question, "options": []}, "investigation": request},
        }
    raise AssertionError("complete investigation request must be executed")


def _execute(session: Session, request: dict[str, Any], run_id: str) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for ref in request["datasets"]:
        dataset = get_dataset(session, ref)
        if not dataset:
            errors.append(f"找不到数据集 {ref}")
            continue
        try:
            result = find_signal_windows(
                dataset, request["signal"], request["op"], request["threshold"], request["min_duration"]
            )
            result["serial"] = ref
            results.append(result)
        except Exception as exc:
            errors.append(f"{ref}: {exc}")

    query = f"{request['signal']} {request.get('original_question', '')} 原因 诊断"
    try:
        vector = embed_sync(query)
    except Exception:
        vector = None
    wiki_hits = search_wiki(session, query, vector, limit=5)
    artifact = write_agent_artifact(run_id, "investigation", "windows.json", {
        "request": request, "results": results, "errors": errors
    })

    measured_lines = []
    for result in results:
        measured_lines.append(
            f"{result['serial']}：{result['signal']} {result['op']} {result['threshold']}，"
            f"发现 {result['window_count']} 个窗口。"
        )
        for window in result["windows"][:5]:
            measured_lines.append(
                f"- {window['file']} {window['start']:.3f}s–{window['end']:.3f}s，"
                f"持续 {window['duration']:.3f}s，min/mean/max="
                f"{window['min']:.4g}/{window['mean']:.4g}/{window['max']:.4g}"
            )
    if errors:
        measured_lines.append("读取警告：" + "；".join(errors))
    measured = "\n".join(measured_lines) or "没有成功读取任何数据窗口。"
    wiki_context = "\n".join(f"- {hit.title} ({hit.path}): {hit.content}" for hit in wiki_hits)

    answer = None
    if wiki_hits and results:
        answer = chat_sync([
            {
                "role": "system",
                "content": (
                    "你是 CALI 信号调查助手。严格区分实测事实与 Wiki 中的解释。"
                    "只能基于给出的窗口和已发布 Wiki 提出原因；用‘可能’表述归因；"
                    "给出下一步检查建议并保留 Wiki 路径。不要把相关性说成因果性。"
                ),
            },
            {"role": "user", "content": f"问题：{request['original_question']}\n\n实测窗口：\n{measured}\n\nWiki：\n{wiki_context}"},
        ])
    if not answer:
        if wiki_hits:
            causes = "\n".join(
                f"- {hit.title}（{hit.path}）：{' '.join(hit.content.split())[:360]}" for hit in wiki_hits
            )
            answer = f"实测结果\n{measured}\n\nWiki 中可供核对的相关资料\n{causes}"
        else:
            answer = (
                f"实测结果\n{measured}\n\n"
                "已发布 Wiki 中没有找到足以解释该信号原因的证据，因此暂不推断原因。"
                "建议补充该信号的控制逻辑、使能条件和已知故障模式资料后再分析。"
            )
    sources = [
        {"kind": "window", "title": f"{item['serial']} · {item['signal']}", "path": artifact["path"], "score": 1.0}
        for item in results
    ] + [
        {"kind": hit.kind, "title": hit.title, "path": hit.path, "score": hit.score} for hit in wiki_hits
    ]
    return {
        "answer": answer,
        "sources": sources,
        "meta": {"investigation": {**request, "status": "completed", "artifact": artifact["path"]}},
        "artifacts": [artifact["path"]],
    }


def handle_investigation(
    session: Session,
    content: str,
    recent_messages: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any] | None:
    """Continue a pending investigation or start one from the current message."""
    pending = None
    if recent_messages:
        previous = recent_messages[-1]
        candidate = (previous.get("meta") or {}).get("investigation")
        if previous.get("role") == "assistant" and candidate and candidate.get("status") == "gathering":
            pending = dict(candidate)

    if pending:
        refs = _dataset_refs(content)
        rule = _rule(content)
        if refs:
            pending["datasets"] = refs
        if rule:
            pending["op"], pending["threshold"] = rule
        pending["min_duration"] = _duration(content) or pending.get("min_duration", 0.0)
        if not pending.get("datasets") or pending.get("threshold") is None:
            return _ask(session, pending)
        return _execute(session, pending, run_id)

    if not _is_investigation(content):
        return None
    rule = _rule(content)
    request = {
        "status": "gathering",
        "original_question": content,
        "datasets": _dataset_refs(content),
        "signal": _signal_name(content),
        "op": rule[0] if rule else None,
        "threshold": rule[1] if rule else None,
        "min_duration": _duration(content),
    }
    if not request["datasets"] or request["threshold"] is None:
        return _ask(session, request)
    return _execute(session, request, run_id)
