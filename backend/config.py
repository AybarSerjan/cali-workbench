from __future__ import annotations

from pathlib import Path
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("CALI_WORKSPACE_ROOT", PROJECT_ROOT)).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_prefix="CALI_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    llm_provider: str = "disabled"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_chat_model: str = "qwen3:8b"
    llm_reasoning_model: str = "qwen3:8b"
    llm_embedding_model: str = ""
    llm_api_key: str = ""
    llm_trust_env: bool = False
    llm_proxy_url: str = ""
    wiki_agent_provider: str = "disabled"
    wiki_agent_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    wiki_agent_model: str = "glm-4.5-air"
    wiki_agent_api_key: str = ""
    wiki_agent_trust_env: bool = False
    wiki_agent_proxy_url: str = ""

    @property
    def catalog_db(self) -> Path:
        return ROOT / "data" / "catalog.db"

    @property
    def checkpoint_db(self) -> Path:
        return ROOT / "memory" / "checkpoints.db"


settings = Settings()


def ensure_directories() -> None:
    for path in (
        ROOT / "raw" / "files",
        ROOT / "wiki",
        ROOT / "outputs" / "runs",
        ROOT / "outputs" / "wiki-revisions",
        ROOT / "data" / "files",
        ROOT / "memory",
    ):
        path.mkdir(parents=True, exist_ok=True)
