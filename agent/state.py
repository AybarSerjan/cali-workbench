from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict


AgentStatus = Literal["ok", "not_applicable", "partial", "error"]


class AgentReport(TypedDict):
    run_id: str
    agent: str
    status: AgentStatus
    relevance: float
    summary: str
    evidence: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    warnings: list[str]
    error: str | None
    duration_ms: int


def merge_agent_reports(
    left: dict[str, AgentReport] | None,
    right: dict[str, AgentReport] | None,
) -> dict[str, AgentReport]:
    """Merge isolated branch outputs; duplicate writes in one run are a bug."""
    merged = dict(left or {})
    for agent, report in (right or {}).items():
        previous = merged.get(agent)
        if previous and previous.get("run_id") == report.get("run_id"):
            raise ValueError(f"同一运行重复写入 AgentReport: {agent}")
        merged[agent] = report
    return merged


class AgentState(TypedDict, total=False):
    conversation_id: str
    run_id: str
    question: str
    route: str
    summaries: list[dict[str, Any]]
    recent_messages: list[dict[str, Any]]
    reports: Annotated[dict[str, AgentReport], merge_agent_reports]
    merged_reports: list[AgentReport]
    review_notes: list[str]
    review_warnings: list[str]
    answer: str
    sources: list[dict[str, Any]]
