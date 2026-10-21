from __future__ import annotations

from backend.config import settings
from backend.services import llm


def test_zhipu_provider_uses_openai_compatible_endpoint_and_existing_key(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "OK"}}]}

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(settings, "llm_provider", "zhipu")
    monkeypatch.setattr(settings, "llm_base_url", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setattr(settings, "llm_chat_model", "glm-4.5-air")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "wiki_agent_api_key", "existing-zhipu-key")
    monkeypatch.setattr(llm.httpx, "Client", Client)

    assert llm.chat_sync([{"role": "user", "content": "test"}]) == "OK"
    assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer existing-zhipu-key"
    assert captured["json"]["model"] == "glm-4.5-air"
