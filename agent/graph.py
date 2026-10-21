from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from agent.nodes import (
    classify_node,
    data_specialist_node,
    memory_node,
    memory_specialist_node,
    merge_specialists_node,
    operations_specialist_node,
    reviewer_node,
    synthesize_node,
    wiki_specialist_node,
)
from agent.state import AgentState
from backend.config import settings


settings.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
_connection = sqlite3.connect(settings.checkpoint_db, check_same_thread=False)
checkpointer = SqliteSaver(_connection)


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("classify", classify_node)
    builder.add_node("data_specialist", data_specialist_node)
    builder.add_node("wiki_specialist", wiki_specialist_node)
    builder.add_node("operations_specialist", operations_specialist_node)
    builder.add_node("memory_specialist", memory_specialist_node)
    builder.add_node("merge_specialists", merge_specialists_node)
    builder.add_node("review", reviewer_node)
    builder.add_node("synthesize", synthesize_node)
    builder.add_node("memory", memory_node)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "data_specialist")
    builder.add_edge("classify", "wiki_specialist")
    builder.add_edge("classify", "operations_specialist")
    builder.add_edge("classify", "memory_specialist")
    builder.add_edge("data_specialist", "merge_specialists")
    builder.add_edge("wiki_specialist", "merge_specialists")
    builder.add_edge("operations_specialist", "merge_specialists")
    builder.add_edge("memory_specialist", "merge_specialists")
    builder.add_edge("merge_specialists", "review")
    builder.add_edge("review", "synthesize")
    builder.add_edge("synthesize", "memory")
    builder.add_edge("memory", END)
    return builder.compile(checkpointer=checkpointer)


graph = build_graph()
