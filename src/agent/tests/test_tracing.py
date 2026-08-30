"""Tests for Langfuse tracing of the reasoning path (ROADMAP P19 gap E).

Two properties matter more than the happy path:

1. **Off by default.** This instruments the hottest code path in the system with
   an SDK surface that cannot be exercised on the dev box (langfuse v4 needs
   Python >= 3.10). It ships dormant, so the disabled path must be provably
   inert — no client construction, no SDK calls.
2. **A tracing failure never breaks a query.** If Langfuse throws, the answer
   must still be produced. Observability is not allowed to take down the thing
   it observes.
"""

from types import SimpleNamespace

import pytest

from k8fy import langfuse_client, tracing


@pytest.fixture(autouse=True)
def _reset():
    tracing.reset_for_tests()
    langfuse_client.reset_for_tests()
    yield
    tracing.reset_for_tests()
    langfuse_client.reset_for_tests()


class _Span:
    def __init__(self):
        self.updates = {}

    def update(self, **kw):
        self.updates.update(kw)


class _FakeClient:
    """Minimal stand-in for the v4 observation API."""

    def __init__(self, span=None, raises=None):
        self.span = span or _Span()
        self.raises = raises
        self.calls = []

    def start_as_current_observation(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises

        class _CM:
            def __init__(self, span):
                self.span = span

            def __enter__(inner):
                return inner.span

            def __exit__(inner, *exc):
                return False

        return _CM(self.span)


# ---------------------------------------------------------------------------
# Disabled path
# ---------------------------------------------------------------------------

def test_disabled_makes_no_sdk_calls(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    called = []
    monkeypatch.setattr(langfuse_client, "get_client", lambda: called.append(1))

    with tracing.observe("skill:diagnose", model="m") as span:
        span.update(output="anything")  # must be a no-op, not an error

    assert called == [], "disabled tracing must not even reach for a client"


def test_disabled_when_flag_off_even_with_a_client(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    monkeypatch.setattr(tracing, "get_client", lambda: _FakeClient())
    assert tracing.enabled() is False


def test_enabled_requires_both_flag_and_client(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "get_client", lambda: None)
    assert tracing.enabled() is False, "flag on but no client must stay disabled"


# ---------------------------------------------------------------------------
# Enabled path
# ---------------------------------------------------------------------------

def test_enabled_records_observation_with_prompt_and_model(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "get_client", lambda: client)
    prompt_obj = object()

    with tracing.observe(
        "skill:diagnose", model="claude-opus-4-8", input="q", prompt=prompt_obj
    ) as span:
        span.update(output="a")

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["as_type"] == "generation"
    assert call["name"] == "skill:diagnose"
    assert call["model"] == "claude-opus-4-8"
    # The prompt link is what makes quality comparable across prompt versions.
    assert call["prompt"] is prompt_obj
    assert client.span.updates.get("output") == "a"


def test_no_prompt_kwarg_when_prompt_is_none(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "get_client", lambda: client)

    with tracing.observe("skill:x", model="m", prompt=None):
        pass

    # Passing prompt=None explicitly could be rejected by the SDK; omit it.
    assert "prompt" not in client.calls[0]


# ---------------------------------------------------------------------------
# Failure isolation — the property that protects production
# ---------------------------------------------------------------------------

def test_sdk_failure_does_not_propagate(monkeypatch):
    client = _FakeClient(raises=RuntimeError("langfuse exploded"))
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "get_client", lambda: client)

    # Must not raise, and must still yield something usable.
    with tracing.observe("skill:diagnose", model="m") as span:
        span.update(output="the answer still happened")


def test_span_update_failure_is_swallowed(monkeypatch):
    class Hostile:
        def update(self, **kw):
            raise RuntimeError("update blew up")

    client = _FakeClient(span=Hostile())
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "get_client", lambda: client)

    with tracing.observe("skill:x", model="m") as span:
        tracing._safe_update(span, output="x")  # must not raise


# ---------------------------------------------------------------------------
# Usage extraction
# ---------------------------------------------------------------------------

def test_usage_from_response():
    resp = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=2, cache_creation_input_tokens=3,
    ))
    assert tracing.usage_from(resp) == {
        "input": 10, "output": 5,
        "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3,
    }


def test_usage_from_response_without_usage():
    assert tracing.usage_from(SimpleNamespace()) == {}
