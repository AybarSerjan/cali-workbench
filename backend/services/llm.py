from __future__ import annotations

from typing import Any

import httpx

from backend.config import settings
from backend.config import ROOT


def public_config() -> dict[str, Any]:
    return {
        "provider": settings.llm_provider,
        "base_url": settings.llm_base_url,
        "chat_model": settings.llm_chat_model,
        "reasoning_model": settings.llm_reasoning_model,
        "embedding_model": settings.llm_embedding_model,
        "trust_env": settings.llm_trust_env,
        "has_proxy_url": bool(settings.llm_proxy_url),
        "has_api_key": bool(settings.llm_api_key or (settings.llm_provider == "zhipu" and settings.wiki_agent_api_key)),
    }


def configure(value) -> None:
    settings.llm_provider = value.provider
    settings.llm_base_url = value.base_url.rstrip("/")
    settings.llm_chat_model = value.chat_model
    settings.llm_reasoning_model = value.reasoning_model
    settings.llm_embedding_model = value.embedding_model
    settings.llm_trust_env = value.trust_env
    if value.proxy_url:
        settings.llm_proxy_url = value.proxy_url.strip()
    if value.api_key:
        settings.llm_api_key = value.api_key
    env_path = ROOT / ".env"
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, raw = line.split("=", 1)
                existing[key.strip()] = raw.strip()
    existing.update({
        "CALI_LLM_PROVIDER": settings.llm_provider,
        "CALI_LLM_BASE_URL": settings.llm_base_url,
        "CALI_LLM_CHAT_MODEL": settings.llm_chat_model,
        "CALI_LLM_REASONING_MODEL": settings.llm_reasoning_model,
        "CALI_LLM_EMBEDDING_MODEL": settings.llm_embedding_model,
        "CALI_LLM_API_KEY": settings.llm_api_key,
        "CALI_LLM_TRUST_ENV": "1" if settings.llm_trust_env else "0",
        "CALI_LLM_PROXY_URL": settings.llm_proxy_url,
    })
    env_path.write_text("\n".join(f"{key}={value}" for key, value in existing.items()) + "\n", encoding="utf-8")


def _headers() -> dict[str, str]:
    api_key = settings.llm_api_key or (settings.wiki_agent_api_key if settings.llm_provider == "zhipu" else "")
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _wiki_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.wiki_agent_api_key}"} if settings.wiki_agent_api_key else {}


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _client_kwargs(*, timeout: int, trust_env: bool, proxy_url: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"timeout": timeout, "trust_env": trust_env}
    if proxy_url.strip():
        kwargs["proxy"] = proxy_url.strip()
    return kwargs


def _llm_client_kwargs(timeout: int) -> dict[str, Any]:
    return _client_kwargs(timeout=timeout, trust_env=settings.llm_trust_env, proxy_url=settings.llm_proxy_url)


def _wiki_client_kwargs(timeout: int) -> dict[str, Any]:
    return _client_kwargs(
        timeout=timeout,
        trust_env=settings.wiki_agent_trust_env,
        proxy_url=settings.wiki_agent_proxy_url,
    )


def wiki_agent_public_config() -> dict[str, Any]:
    return {
        "provider": settings.wiki_agent_provider,
        "base_url": settings.wiki_agent_base_url,
        "model": settings.wiki_agent_model,
        "trust_env": settings.wiki_agent_trust_env,
        "has_proxy_url": bool(settings.wiki_agent_proxy_url),
        "has_api_key": bool(settings.wiki_agent_api_key),
    }


def wiki_agent_enabled() -> bool:
    return settings.wiki_agent_provider != "disabled"


def wiki_agent_chat_sync(messages: list[dict[str, str]]) -> str | None:
    """Wiki 起草专用模型。openai_compatible 的 base_url 需含版本段，代码拼接 /chat/completions。"""
    if settings.wiki_agent_provider == "disabled":
        return None
    model = settings.wiki_agent_model
    base = settings.wiki_agent_base_url.rstrip("/")
    with httpx.Client(**_wiki_client_kwargs(timeout=120)) as client:
        if settings.wiki_agent_provider == "ollama":
            response = client.post(
                _join_url(base, "api/chat"),
                json={"model": model, "messages": messages, "stream": False},
            )
            response.raise_for_status()
            return str(response.json().get("message", {}).get("content", "")).strip()
        response = client.post(
            _join_url(base, "chat/completions"),
            headers=_wiki_headers(),
            json={"model": model, "messages": messages, "stream": False},
        )
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"]).strip()


def wiki_agent_test() -> dict[str, Any]:
    if settings.wiki_agent_provider == "disabled":
        return {"ok": False, "detail": "Wiki Agent 未启用。"}
    result = wiki_agent_chat_sync([{"role": "user", "content": "只回复 OK"}])
    return {"ok": bool(result), "detail": result or "模型未返回内容"}


async def chat(messages: list[dict[str, str]], *, reasoning: bool = False) -> str | None:
    if settings.llm_provider == "disabled":
        return None
    model = settings.llm_reasoning_model if reasoning else settings.llm_chat_model
    base = settings.llm_base_url.rstrip("/")
    async with httpx.AsyncClient(**_llm_client_kwargs(timeout=90)) as client:
        if settings.llm_provider == "ollama":
            response = await client.post(
                _join_url(base, "api/chat"),
                json={"model": model, "messages": messages, "stream": False},
            )
            response.raise_for_status()
            return str(response.json().get("message", {}).get("content", "")).strip()
        response = await client.post(
            _join_url(base, "chat/completions"),
            headers=_headers(),
            json={"model": model, "messages": messages, "stream": False},
        )
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"]).strip()


async def embed(text: str) -> list[float] | None:
    if settings.llm_provider == "disabled" or not settings.llm_embedding_model:
        return None
    base = settings.llm_base_url.rstrip("/")
    async with httpx.AsyncClient(**_llm_client_kwargs(timeout=60)) as client:
        if settings.llm_provider == "ollama":
            response = await client.post(
                _join_url(base, "api/embed"),
                json={"model": settings.llm_embedding_model, "input": text},
            )
            response.raise_for_status()
            vectors = response.json().get("embeddings") or []
            return vectors[0] if vectors else None
        response = await client.post(
            _join_url(base, "embeddings"),
            headers=_headers(),
            json={"model": settings.llm_embedding_model, "input": text},
        )
        response.raise_for_status()
        return response.json()["data"][0]["embedding"]


async def test_connection() -> dict[str, Any]:
    if settings.llm_provider == "disabled":
        return {"ok": True, "detail": "模型已禁用，系统使用确定性降级路径。"}
    result = await chat([{"role": "user", "content": "只回复 OK"}])
    return {"ok": bool(result), "detail": result or "模型未返回内容"}


def chat_sync(messages: list[dict[str, str]], *, reasoning: bool = False) -> str | None:
    if settings.llm_provider == "disabled":
        return None
    model = settings.llm_reasoning_model if reasoning else settings.llm_chat_model
    base = settings.llm_base_url.rstrip("/")
    with httpx.Client(**_llm_client_kwargs(timeout=90)) as client:
        if settings.llm_provider == "ollama":
            response = client.post(
                _join_url(base, "api/chat"),
                json={"model": model, "messages": messages, "stream": False},
            )
            response.raise_for_status()
            return str(response.json().get("message", {}).get("content", "")).strip()
        response = client.post(
            _join_url(base, "chat/completions"),
            headers=_headers(),
            json={"model": model, "messages": messages, "stream": False},
        )
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"]).strip()


def embed_sync(text: str) -> list[float] | None:
    if settings.llm_provider == "disabled" or not settings.llm_embedding_model:
        return None
    base = settings.llm_base_url.rstrip("/")
    with httpx.Client(**_llm_client_kwargs(timeout=60)) as client:
        if settings.llm_provider == "ollama":
            response = client.post(
                _join_url(base, "api/embed"),
                json={"model": settings.llm_embedding_model, "input": text},
            )
            response.raise_for_status()
            vectors = response.json().get("embeddings") or []
            return vectors[0] if vectors else None
        response = client.post(
            _join_url(base, "embeddings"),
            headers=_headers(),
            json={"model": settings.llm_embedding_model, "input": text},
        )
        response.raise_for_status()
        return response.json()["data"][0]["embedding"]
