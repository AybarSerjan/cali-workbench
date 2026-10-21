from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    kind: Literal["mdf", "calibration"]
    description: str = ""


class DatasetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    measurement_date: str | None = Field(default=None, pattern=r"^(?:\d{4}-\d{2}-\d{2})?$")
    a2l: str | None = Field(default=None, max_length=256)
    hex: str | None = Field(default=None, max_length=256)
    experiment: str | None = Field(default=None, max_length=256)


class MergeSignalPair(BaseModel):
    signal_a: str = Field(min_length=1, max_length=256)
    signal_b: str = Field(min_length=1, max_length=256)

    @field_validator("signal_a", "signal_b")
    @classmethod
    def clean_signal(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("对齐信号不能为空")
        return value


class DatasetMergeRequest(BaseModel):
    dataset_a_id: str = Field(min_length=1)
    dataset_b_id: str = Field(min_length=1)
    name: str = Field(default="", max_length=80)
    signal_pairs: list[MergeSignalPair] = Field(default_factory=list)


class CalibrationCommitCreate(BaseModel):
    parent_id: str | None = None
    a2l_file_id: str | None = None
    hex_file_id: str


class CalibrationSessionCreate(BaseModel):
    a2l_file_id: str
    hex_file_ids: list[str] = Field(min_length=2, max_length=2)
    created_by: str = Field(default="user", min_length=1, max_length=32)


class A2lIndexCreate(BaseModel):
    dataset_file_id: str = Field(min_length=1)


class WorkpackageCreate(BaseModel):
    wp_code: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=512)
    definition: dict[str, Any] = Field(default_factory=dict)
    created_by: Literal["agent", "user"] = "user"


class WorkpackageRevisionCreate(BaseModel):
    definition: dict[str, Any]
    title: str | None = Field(default=None, min_length=1, max_length=512)
    created_by: Literal["agent", "user"] = "user"


class WorkpackageReview(BaseModel):
    review_notes: list[str] = Field(default_factory=list)


class WorkpackageDraftRequest(BaseModel):
    wp_code: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=512)
    prompt: str = Field(min_length=1, max_length=20_000)


class WorkpackageMatchRequest(BaseModel):
    dataset_id: str = Field(min_length=1)
    a2l_sha256: str = ""


class SignalMappingCreate(BaseModel):
    a2l_name: str = Field(min_length=1, max_length=256)
    mf4_channel: str = Field(min_length=1, max_length=256)
    evidence: dict[str, Any] = Field(default_factory=dict)


class WorkpackageEvaluateRequest(BaseModel):
    dataset_id: str = Field(min_length=1)
    a2l_file_id: str | None = None
    hex_file_id: str | None = None


class SignalWindowRequest(BaseModel):
    signal: str = Field(min_length=1)
    op: Literal["<", "<=", ">", ">=", "==", "!="] = "<"
    threshold: float
    min_duration: float = Field(default=0.0, ge=0)


class ObfcmRequest(BaseModel):
    # Override default MDF channel names per dataset; omit to use the standard names.
    channel_map: dict[str, str] | None = None


class LinkSourceCreate(BaseModel):
    url: str = Field(pattern=r"^https?://", max_length=2048)
    title: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1)


class WikiDraftCreate(BaseModel):
    source_id: str
    title: str | None = None
    slug: str | None = None


class WikiDraftUpdate(BaseModel):
    content: str = Field(min_length=1)


class PublishRequest(BaseModel):
    expected_base_sha256: str = ""


class ConversationCreate(BaseModel):
    title: str = "新对话"


class ChatRequest(BaseModel):
    content: str = Field(min_length=1)
    grill: bool = False


class MemoryFactCreate(BaseModel):
    key: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1)
    conversation_id: str | None = None
    supersedes_id: str | None = None
    source_turn: int | None = None


class ModelSettings(BaseModel):
    provider: Literal["disabled", "ollama", "zhipu", "openai_compatible"] = "disabled"
    base_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3:8b"
    reasoning_model: str = "qwen3:8b"
    embedding_model: str = ""
    api_key: str = ""
    trust_env: bool = False
    proxy_url: str = ""


class ApiResult(BaseModel):
    ok: bool = True
    data: Any = None
