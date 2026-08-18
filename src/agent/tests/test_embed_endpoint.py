"""Tests for POST /embed (P8 — semantic memory layer, ADR 0018): the route
the Go backend's async embed goroutine calls after every Tier-2 trace.
Voyage AI's client is monkeypatched rather than called for real — same
approach as test_similar_incidents.py on the tool-call side of this feature.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture
def client():
    return TestClient(app_module.app)


def test_embed_unavailable_when_no_voyage_key(client, monkeypatch):
    monkeypatch.setattr(app_module.settings, "voyage_api_key", "")

    resp = client.post("/embed", json={"text": "pod crash looping, OOMKilled"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["embedding"] is None


def test_embed_returns_vector_when_voyage_configured(client, monkeypatch):
    monkeypatch.setattr(app_module.settings, "voyage_api_key", "test-key")
    monkeypatch.setattr(app_module.settings, "voyage_model", "voyage-3-lite")

    class _FakeEmbedResult:
        embeddings = [[0.1, 0.2, 0.3, 0.4]]

    class _FakeVoyageClient:
        def __init__(self, api_key):
            assert api_key == "test-key"

        def embed(self, texts, model):
            assert texts == ["pod crash looping, OOMKilled"]
            assert model == "voyage-3-lite"
            return _FakeEmbedResult()

    import voyageai
    monkeypatch.setattr(voyageai, "Client", _FakeVoyageClient)

    resp = client.post("/embed", json={"text": "pod crash looping, OOMKilled"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["embedding"] == [0.1, 0.2, 0.3, 0.4]
    assert body["dim"] == 4
    assert body["model"] == "voyage-3-lite"


def test_embed_request_can_override_model(client, monkeypatch):
    monkeypatch.setattr(app_module.settings, "voyage_api_key", "test-key")
    monkeypatch.setattr(app_module.settings, "voyage_model", "voyage-3-lite")

    class _FakeEmbedResult:
        embeddings = [[0.5]]

    class _FakeVoyageClient:
        def __init__(self, api_key):
            pass

        def embed(self, texts, model):
            assert model == "voyage-3-large"
            return _FakeEmbedResult()

    import voyageai
    monkeypatch.setattr(voyageai, "Client", _FakeVoyageClient)

    resp = client.post("/embed", json={"text": "text", "model": "voyage-3-large"})

    assert resp.status_code == 200
    assert resp.json()["model"] == "voyage-3-large"


def test_embed_swallows_voyage_errors_and_reports_unavailable(client, monkeypatch):
    monkeypatch.setattr(app_module.settings, "voyage_api_key", "test-key")

    class _FakeVoyageClient:
        def __init__(self, api_key):
            pass

        def embed(self, texts, model):
            raise RuntimeError("voyage API rate limited")

    import voyageai
    monkeypatch.setattr(voyageai, "Client", _FakeVoyageClient)

    resp = client.post("/embed", json={"text": "pod crash looping"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["embedding"] is None
