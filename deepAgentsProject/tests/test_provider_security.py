from __future__ import annotations

import httpx
import pytest

from packages.http_security import EndpointSecurityError, async_provider_client, provider_client
from packages.knowledge.embedding import OpenAICompatibleEmbeddingProvider
from packages.knowledge.errors import KnowledgeStorageError
from packages.runtime.coding_model import close_coding_chat_model, create_coding_chat_model
from packages.runtime.model_gateway import ModelGatewayError, OpenAICompatibleConfig, OpenAICompatibleModelGateway


@pytest.mark.parametrize("api_style", ["chat_completions", "responses", "anthropic_messages"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_model_redirects_never_forward_credentials_or_prompt(api_style, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://unapproved.test/receive"})

    gateway = OpenAICompatibleModelGateway(
        OpenAICompatibleConfig(base_url="https://model.test/v1", api_key="synthetic-key",
            model="test", api_style=api_style, auth_style="anthropic"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelGatewayError, match="request failed"):
        await gateway.complete([{"role": "user", "content": "synthetic-private-prompt"}])
    assert len(requests) == 1
    assert requests[0].url.host == "model.test"


def test_embedding_redirect_is_rejected_without_leaking_secret_in_error():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://unapproved.test/synthetic-key"})

    provider = OpenAICompatibleEmbeddingProvider("https://embedding.test/v1", "synthetic-key", "test", 2,
        transport=httpx.MockTransport(handler))
    with pytest.raises(KnowledgeStorageError) as captured:
        provider.embed_query("synthetic-private-document")
    assert "synthetic-key" not in str(captured.value)
    assert "synthetic-private-document" not in str(captured.value)
    assert len(requests) == 1


def test_embedding_slow_stream_cannot_outlive_its_metering_reservation(monkeypatch):
    ticks=iter([0,61])
    monkeypatch.setattr("packages.knowledge.embedding.monotonic",lambda:next(ticks))
    provider=OpenAICompatibleEmbeddingProvider("https://embedding.test/v1","synthetic-key","test",2,
        transport=httpx.MockTransport(lambda request:httpx.Response(200,json={
            "model":"test","data":[{"index":0,"embedding":[.1,.2]}],"usage":{"prompt_tokens":1,"total_tokens":1}})))
    with pytest.raises(KnowledgeStorageError,match="deadline"):
        provider.embed_query("synthetic-document")


@pytest.mark.parametrize("base_url", ["http://model.test/v1", "https://unapproved.test/v1"])
def test_production_model_and_embedding_reject_unapproved_origins(monkeypatch, base_url):
    monkeypatch.setenv("DEEPAGENT_ENVIRONMENT", "production")
    monkeypatch.setenv("DEEPAGENT_MODEL_ALLOWED_ORIGINS", "https://model.test")
    monkeypatch.setenv("KNOWLEDGE_EMBEDDING_ALLOWED_ORIGINS", "https://model.test")
    with pytest.raises(ModelGatewayError):
        OpenAICompatibleConfig(base_url=base_url, api_key="test-key", model="test")
    with pytest.raises(EndpointSecurityError):
        OpenAICompatibleEmbeddingProvider(base_url, "test-key", "test", 2)
    assert OpenAICompatibleConfig(base_url="https://model.test/v1", api_key="test-key", model="test")


def test_production_requires_explicit_origin_configuration(monkeypatch):
    monkeypatch.setenv("DEEPAGENT_ENVIRONMENT", "production")
    monkeypatch.delenv("DEEPAGENT_MODEL_ALLOWED_ORIGINS", raising=False)
    with pytest.raises(ModelGatewayError, match="ALLOWED_ORIGINS"):
        OpenAICompatibleConfig(base_url="https://model.test", api_key="test-key", model="test")


async def test_transport_cannot_be_redirected_or_retargeted_by_sdk():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://model.test/another-path"})

    async with async_provider_client("https://model.test", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.RequestError, match="unapproved origin"):
            await client.post("https://other.test", content="private")
        assert not requests
        with pytest.raises(httpx.RequestError, match="redirects are disabled"):
            await client.post("https://model.test", follow_redirects=True)
        assert len(requests) == 1


@pytest.mark.parametrize("api_style", ["chat_completions", "anthropic_messages"])
async def test_native_coding_model_uses_the_same_origin_boundary(monkeypatch, api_style):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://other.test/receive"})

    monkeypatch.setattr("packages.runtime.coding_model.provider_client", lambda base_url, **kwargs:
        provider_client(base_url, transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr("packages.runtime.coding_model.async_provider_client", lambda base_url, **kwargs:
        async_provider_client(base_url, transport=httpx.MockTransport(handler), **kwargs))
    if api_style == "anthropic_messages":
        import httpx2

        def anthropic_handler(request):
            requests.append(request)
            return httpx2.Response(307, headers={"location": "https://other.test/receive"})

        monkeypatch.setattr("packages.runtime.coding_model.anthropic.DefaultAsyncHttpxClient", lambda **kwargs:
            httpx2.AsyncClient(transport=httpx2.MockTransport(anthropic_handler), **kwargs))
    gateway = OpenAICompatibleModelGateway(OpenAICompatibleConfig(
        base_url="https://model.test/v1", api_key="synthetic-key", model="test", api_style=api_style))
    model = create_coding_chat_model(gateway)
    try:
        with pytest.raises(Exception, match="Connection error"):
            await model.ainvoke("synthetic-private-prompt")
        assert len(requests) == 1
        assert requests[0].url.host == "model.test"
    finally:
        await close_coding_chat_model(model)
