"""Tests for ADR 0007's 2026-09-14 amendment, Decision #1: the operator's
free-text question is no longer exempt from redaction before it reaches
either egress point (the Claude prompt, the Langfuse trace).

Two call sites are covered directly:
  - `_build_user_message` (shared by `_reason_single`, `_reason_advisor_executor`,
    `_reason_pattern_a` — redacting `context["question"]` here covers all three).
  - `_traced_chat` (wraps `reason_chat`) — redacts BEFORE the trace's `input=`
    is captured, not after, since the tracing span opens before the wrapped
    method body runs.

See test_claude_client_redaction.py for Decision #4's separate backstop at
the Anthropic client itself.
"""

import pytest

from k8fy import tracing
from k8fy.agent import K8fyAgent, _redact_context_question, _traced_chat
from models.response import AgentResponse

_LEAKED_KEY = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def _bare_agent() -> K8fyAgent:
    agent = K8fyAgent.__new__(K8fyAgent)
    agent.model = "test-model"
    return agent


def test_redact_context_question_masks_a_leaked_secret():
    context = {"namespace": "payments", "question": f"my key {_LEAKED_KEY} is leaking"}
    safe = _redact_context_question(context)
    assert _LEAKED_KEY not in safe["question"]
    # Structured fields untouched.
    assert safe["namespace"] == "payments"
    # Original dict is not mutated — callers elsewhere may still hold it.
    assert _LEAKED_KEY in context["question"]


def test_redact_context_question_is_a_noop_without_a_question_field():
    context = {"namespace": "payments", "session_id": "abc"}
    assert _redact_context_question(context) == context


def test_build_user_message_redacts_the_operators_question():
    agent = _bare_agent()
    msg = agent._build_user_message(
        "k8fy/diagnose",
        {"some": "data"},
        {"namespace": "payments", "question": f"can you check {_LEAKED_KEY}?"},
    )
    assert _LEAKED_KEY not in msg


class _Span:
    def __init__(self):
        self.updates = {}

    def update(self, **kw):
        self.updates.update(kw)


class _FakeLangfuseClient:
    """Minimal stand-in for the v4 observation API — same shape as
    test_tracing.py's own fake, reused here rather than imported since that
    module's fixture also resets global tracing state this test needs too."""

    def __init__(self):
        self.span = _Span()

    def start_as_current_observation(self, **kwargs):
        class _CM:
            def __init__(inner, span):
                inner.span = span

            def __enter__(inner):
                return inner.span

            def __exit__(inner, *exc):
                return False

        return _CM(self.span)


@pytest.fixture(autouse=True)
def _reset_tracing():
    tracing.reset_for_tests()
    yield
    tracing.reset_for_tests()


@pytest.mark.asyncio
async def test_traced_chat_redacts_before_the_trace_captures_input(monkeypatch):
    fake_langfuse = _FakeLangfuseClient()
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "get_client", lambda: fake_langfuse)

    captured = {}

    @_traced_chat
    async def fake_reason_chat(self, messages, context=None):
        captured["messages"] = messages
        return AgentResponse(answer="ok", status="ok", confidence=1.0)

    agent = _bare_agent()
    secret_messages = [{"role": "user", "content": f"my key {_LEAKED_KEY} is leaking"}]

    await fake_reason_chat(agent, secret_messages)

    # The wrapped method itself received redacted messages...
    assert _LEAKED_KEY not in captured["messages"][0]["content"]
    # ...and so did the trace, because _traced_chat redacts before opening
    # the span, not after.
    traced_input = fake_langfuse.span.updates.get("input")
    assert traced_input is not None
    assert _LEAKED_KEY not in traced_input[0]["content"]
    # The ORIGINAL list passed in by the caller (e.g. app.py's request.messages)
    # must be untouched — _traced_chat builds a new list, never mutates in place.
    assert _LEAKED_KEY in secret_messages[0]["content"]


@pytest.mark.asyncio
async def test_traced_chat_passes_non_string_content_through(monkeypatch):
    """A message whose content is already a list of blocks (mid-tool-loop
    shape) must not crash the redaction pass — it's simply left as-is."""
    monkeypatch.setattr(tracing, "_enabled", False)

    @_traced_chat
    async def fake_reason_chat(self, messages, context=None):
        return AgentResponse(answer="ok", status="ok", confidence=1.0)

    agent = _bare_agent()
    messages = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
    result = await fake_reason_chat(agent, messages)
    assert result.answer == "ok"
