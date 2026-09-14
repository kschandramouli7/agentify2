from typing import Any

from anthropic import AsyncAnthropic

from config.settings import get_settings
from k8fy.log_redaction import redact_log_text

settings = get_settings()

# Async client — the FastAPI handlers and the agent loop are async, so use the
# async SDK to avoid blocking the event loop on each API call.
# Falls back to ANTHROPIC_API_KEY from the environment when claude_api_key is unset.
_real_client = AsyncAnthropic(api_key=settings.claude_api_key or None)


def _redact_text_blocks(value: Any) -> Any:
    """Recursively redact string content inside a `messages`/`system` param.
    Anthropic's `content` shape varies — a plain string, a list of content
    blocks (`{"type": "text", "text": ...}`), or a tool-result dict wrapping
    a JSON string — so this walks whatever shape shows up rather than
    assuming one. SDK response objects re-appended into a later turn
    (assistant turns, already model-authored) are neither dict nor list and
    fall through unchanged via the final branch — deliberate: this backstop
    targets the outbound prompt text a call site assembled, not every object
    a prior turn happens to carry forward.
    """
    if isinstance(value, str):
        return redact_log_text(value)
    if isinstance(value, list):
        return [_redact_text_blocks(v) for v in value]
    if isinstance(value, dict):
        out = dict(value)
        if isinstance(out.get("text"), str):
            out["text"] = redact_log_text(out["text"])
        elif "content" in out:
            out["content"] = _redact_text_blocks(out["content"])
        return out
    return value


class _RedactingMessages:
    """Wraps AsyncAnthropic's `.messages` resource, redacting `messages`/
    `system` immediately before the real call (ADR 0007's 2026-09-14
    amendment, Decision #4) — a defense-in-depth backstop, not the primary
    control. The primary redaction still happens upstream, where the
    data/question is assembled (agent.py's `_build_user_message` and
    `_traced_chat`); this catches what a future call site forgets to apply,
    the same "defense in depth, not the primary control" framing
    investigator.go's double-redaction already established on the Go side.
    """

    def __init__(self, real_messages: Any) -> None:
        self._real = real_messages

    async def create(self, **kwargs: Any) -> Any:
        if "messages" in kwargs:
            kwargs["messages"] = _redact_text_blocks(kwargs["messages"])
        if "system" in kwargs:
            kwargs["system"] = _redact_text_blocks(kwargs["system"])
        return await self._real.create(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _RedactingBeta:
    """Wraps AsyncAnthropic's `.beta` namespace the same way, for the
    advisor/executor path's `client.beta.messages.create(...)` call."""

    def __init__(self, real_beta: Any) -> None:
        self._real = real_beta
        self.messages = _RedactingMessages(real_beta.messages)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class RedactingAnthropicClient:
    """Thin wrapper around AsyncAnthropic exposing the same `.messages`/
    `.beta` surface K8fyAgent uses, with a denylist redaction backstop
    applied to every outbound call. Falls through to the real client for
    anything else via `__getattr__`."""

    def __init__(self, real_client: AsyncAnthropic) -> None:
        self._real = real_client
        self.messages = _RedactingMessages(real_client.messages)
        self.beta = _RedactingBeta(real_client.beta)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


client = RedactingAnthropicClient(_real_client)


def get_claude_client() -> RedactingAnthropicClient:
    """Get the async Anthropic client instance, wrapped with a denylist
    redaction backstop (see RedactingAnthropicClient's docstring)."""
    return client
