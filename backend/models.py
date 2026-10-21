from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base, utcnow


def new_id() -> str:
    return uuid.uuid4().hex


class SourceFile(Base):
    __tablename__ = "source_files"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    original_name: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    suffix: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    relative_path: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(Integer)
    # Provenance for external "link sources" (e.g. NotebookLM): empty = uploaded file.
    source_url: Mapped[str] = mapped_column(String(2048), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WikiArticle(Base):
    __tablename__ = "wiki_articles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    published_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    published_sha256: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    revisions: Mapped[list["WikiRevision"]] = relationship(back_populates="article", cascade="all, delete-orphan")


class WikiRevision(Base):
    __tablename__ = "wiki_revisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    article_id: Mapped[str] = mapped_column(ForeignKey("wiki_articles.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    base_sha256: Mapped[str] = mapped_column(String(64), default="")
    source_id: Mapped[str | None] = mapped_column(ForeignKey("source_files.id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, default="[]")
    created_by: Mapped[str] = mapped_column(String(32), default="agent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    article: Mapped[WikiArticle] = relationship(back_populates="revisions")
    __table_args__ = (UniqueConstraint("article_id", "number"),)


class WikiEmbedding(Base):
    __tablename__ = "wiki_embeddings"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    article_id: Mapped[str] = mapped_column(ForeignKey("wiki_articles.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("wiki_revisions.id"), unique=True)
    model: Mapped[str] = mapped_column(String(256))
    vector_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    files: Mapped[list["DatasetFile"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")
    annotation: Mapped["DatasetAnnotation | None"] = relationship(back_populates="dataset", cascade="all, delete-orphan", uselist=False)
    identity: Mapped["DatasetIdentity | None"] = relationship(back_populates="dataset", cascade="all, delete-orphan", uselist=False)


class DatasetIdentity(Base):
    __tablename__ = "dataset_identities"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), primary_key=True)
    serial: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    dataset: Mapped[Dataset] = relationship(back_populates="identity")


# Editable catalog fields per MDF dataset. Order is the display order; the
# tuple is the single source of truth reused by the payload/update code.
# Kept to fields the MDF deterministically provides (date + header references);
# fragile filename-token parsing was dropped.
ANNOTATION_FIELDS = (
    "measurement_date",  # 日期
    "a2l",
    "hex",
    "experiment",        # 测量界面 (INCA experiment, from header comment)
)


class DatasetAnnotation(Base):
    __tablename__ = "dataset_annotations"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), primary_key=True)
    measurement_date: Mapped[str] = mapped_column(String(10), default="")
    a2l: Mapped[str] = mapped_column(String(256), default="")
    hex: Mapped[str] = mapped_column(String(256), default="")
    experiment: Mapped[str] = mapped_column(String(256), default="")
    dataset: Mapped[Dataset] = relationship(back_populates="annotation")


class DatasetFile(Base):
    __tablename__ = "dataset_files"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    original_name: Mapped[str] = mapped_column(String(512))
    suffix: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    relative_path: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dataset: Mapped[Dataset] = relationship(back_populates="files")


class CalibrationCommit(Base):
    __tablename__ = "calibration_commits"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("calibration_commits.id"), nullable=True)
    a2l_file_id: Mapped[str] = mapped_column(ForeignKey("dataset_files.id"))
    hex_file_id: Mapped[str] = mapped_column(ForeignKey("dataset_files.id"))
    message: Mapped[str] = mapped_column(String(512))
    diff_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CalibrationSession(Base):
    __tablename__ = "calibration_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    a2l_file_id: Mapped[str] = mapped_column(ForeignKey("source_files.id"), index=True)
    hex_file_ids: Mapped[str] = mapped_column(Text)
    session_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(32), default="user")
    parameter_changes: Mapped[list["ParameterChange"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ParameterChange.parameter_name"
    )


class ParameterChange(Base):
    __tablename__ = "parameter_changes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("calibration_sessions.id"), index=True)
    parameter_name: Mapped[str] = mapped_column(String(256), index=True)
    module: Mapped[str] = mapped_column(String(256), index=True)
    data_type: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(64), default="")
    range: Mapped[str] = mapped_column(String(128), default="")
    a2l_description: Mapped[str] = mapped_column(Text, default="")
    # VALUE | CURVE | MAP | VAL_BLK | ASCII | CUBOID. For non-scalar types
    # old/new_value hold a human summary and detail_json holds the structured
    # per-cell diff (axes, changed cells, counts) from the decode engine.
    char_type: Mapped[str] = mapped_column(String(16), default="VALUE")
    old_value: Mapped[str] = mapped_column(Text)
    new_value: Mapped[str] = mapped_column(Text)
    change_summary: Mapped[str] = mapped_column(Text, default="")
    detail_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    session: Mapped[CalibrationSession] = relationship(back_populates="parameter_changes")


class Workpackage(Base):
    __tablename__ = "workpackages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    wp_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    approved_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    revisions: Mapped[list["WorkpackageRevision"]] = relationship(back_populates="workpackage", cascade="all, delete-orphan")


class WorkpackageRevision(Base):
    __tablename__ = "workpackage_revisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workpackage_id: Mapped[str] = mapped_column(ForeignKey("workpackages.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    # draft → reviewed → approved; forward-only, approved revisions are frozen.
    status: Mapped[str] = mapped_column(String(32), default="draft")
    definition_json: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    review_notes: Mapped[str] = mapped_column(Text, default="[]")
    created_by: Mapped[str] = mapped_column(String(32), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    workpackage: Mapped[Workpackage] = relationship(back_populates="revisions")
    __table_args__ = (UniqueConstraint("workpackage_id", "number"),)


class SignalMapping(Base):
    """Global versioned WP-signal ↔ MF4-channel map (reused across MF4 files).
    Corrections supersede, never delete — same idiom as MemoryFact."""
    __tablename__ = "signal_mappings"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    a2l_name: Mapped[str] = mapped_column(String(256), index=True)
    mf4_channel: Mapped[str] = mapped_column(String(256))
    match_layer: Mapped[str] = mapped_column(String(16))  # L1 | L2 | L3 | manual
    match_status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed | confirmed
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | superseded
    supersedes_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_by: Mapped[str] = mapped_column(String(32), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WpEvaluation(Base):
    __tablename__ = "wp_evaluations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    workpackage_id: Mapped[str] = mapped_column(ForeignKey("workpackages.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("workpackage_revisions.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    a2l_sha256: Mapped[str] = mapped_column(String(64), default="")
    hex_sha256: Mapped[str] = mapped_column(String(64), default="")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(512), default="新对话")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    turn_number: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    # Structured UI payload for task/agent mode: {"ask": {...}} (clarifying
    # options) or {"action": {...}, "status": "pending|confirmed|cancelled"}.
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    start_turn: Mapped[int] = mapped_column(Integer)
    end_turn: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("conversation_id", "start_turn", "end_turn"),)


class MemoryFact(Base):
    __tablename__ = "memory_facts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(256), index=True)
    value: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active")
    supersedes_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="running")
    current_node: Mapped[str] = mapped_column(String(128), default="created")
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    input_sha256: Mapped[str] = mapped_column(String(64), default="")
    manifest_path: Mapped[str] = mapped_column(String(1024), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
