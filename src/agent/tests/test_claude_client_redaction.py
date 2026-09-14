"""Tests for ADR 0007's 2026-09-14 amendment, Decision #4: a defense-in-depth
redaction backstop wrapping the Anthropic client itself, catching whatever a
future call site forgets to redact upstream. Exercises RedactingAnthropicClient
directly against a fake "real" Anthropic client, without touching network or
API keys.
"""

import pytest

from config.claude_client import RedactingAnthropicClient

_LEAKED_KEY = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


class _FakeMessages:
    def __init__(self):
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return "fake-response"


class _FakeBeta:
    def __init__(self):
        self.messages = _FakeMessages()


class _FakeRealClient:
    def __init__(self):
        self.messages = _FakeMessages()
        self.beta = _FakeBeta()
        self.some_other_attr = "untouched"


@pytest.mark.asyncio
async def test_messages_create_redacts_leaked_secret_in_messages():
    real = _FakeRealClient()
    client = RedactingAnthropicClient(real)

    result = await client.messages.create(
        model="test",
        messages=[{"role": "user", "content": f"leaking {_LEAKED_KEY} here"}],
    )

    assert result == "fake-response"
    sent = real.messages.last_kwargs["messages"]
    assert _LEAKED_KEY not in sent[0]["content"]


@pytest.mark.asyncio
async def test_messages_create_redacts_system_prompt_blocks():
    real = _FakeRealClient()
    client = RedactingAnthropicClient(real)

    await client.messages.create(
        model="test",
        system=[{"type": "text", "text": f"context includes {_LEAKED_KEY}", "cache_control": {"type": "ephemeral"}}],
        messages=[],
    )

    sent_system = real.messages.last_kwargs["system"]
    assert _LEAKED_KEY not in sent_system[0]["text"]
    # Non-text keys survive untouched.
    assert sent_system[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_beta_messages_create_also_redacts():
    """The advisor/executor path calls client.beta.messages.create — the
    backstop must cover this surface too, not just the plain .messages one."""
    real = _FakeRealClient()
    client = RedactingAnthropicClient(real)

    await client.beta.messages.create(
        betas=["advisor-beta"],
        model="test",
        messages=[{"role": "user", "content": f"advisor sees {_LEAKED_KEY} too"}],
    )

    sent = real.beta.messages.last_kwargs["messages"]
    assert _LEAKED_KEY not in sent[0]["content"]
    # Non-text kwargs pass through unchanged.
    assert real.beta.messages.last_kwargs["betas"] == ["advisor-beta"]


@pytest.mark.asyncio
async def test_tool_result_content_strings_are_also_redacted():
    """A tool_result block's `content` is a JSON string built from fetched
    data — defense in depth means the backstop also screens it, on the same
    "catches what upstream forgets" logic, not just the top-level prompt."""
    real = _FakeRealClient()
    client = RedactingAnthropicClient(real)

    await client.messages.create(
        model="test",
        messages=[{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "abc",
                "content": f'{{"note": "leaked {_LEAKED_KEY}"}}',
            }],
        }],
    )

    sent = real.messages.last_kwargs["messages"]
    assert _LEAKED_KEY not in sent[0]["content"][0]["content"]


def test_getattr_falls_through_to_the_real_client():
    real = _FakeRealClient()
    client = RedactingAnthropicClient(real)
    assert client.some_other_attr == "untouched"


@pytest.mark.asyncio
async def test_no_secret_present_leaves_text_unchanged():
    real = _FakeRealClient()
    client = RedactingAnthropicClient(real)

    await client.messages.create(
        model="test",
        messages=[{"role": "user", "content": "how many replicas does payment-api have?"}],
    )

    sent = real.messages.last_kwargs["messages"]
    assert sent[0]["content"] == "how many replicas does payment-api have?"
