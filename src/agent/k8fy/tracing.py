"""Langfuse tracing for the reasoning path (ROADMAP P19 gap E).

Why this exists: until now Langfuse was wired for prompt management only — the
agent emitted no traces or observations at all. That has two consequences. There
is no production quality/cost/latency view per prompt version, and Langfuse's
LLM-as-a-Judge evaluators attach to *observations*, so with none emitted there is
nothing for a judge to run on. Anything that reviews live traffic (P19) is
blocked on this.

Design constraints:

- **Off by default.** `LANGFUSE_TRACING_ENABLED` gates every code path here.
  This wraps the hottest path in the system using an SDK surface that cannot be
  exercised on a Python 3.9 dev box (langfuse v4 needs >=3.10), so it ships
  dormant and is switched on deliberately after being watched.
- **Never break a query.** Every function swallows its own exceptions. A tracing
  failure must degrade to "no trace", never to a failed answer.
- **Observation-level, not trace-level.** Langfuse's trace-level evaluators are
  legacy and stop producing results on Cloud after 2026-11-16, so observations
  are the only sensible target.
- **Session-aware.** `session_id` is propagated so the turns of one conversation
  group together. Judging a conversation — and specifically spotting context that
  should have been fetched earlier — is impossible turn-by-turn.

The whole conversation history is written onto the chat observation on purpose:
Langfuse evaluators only see data on the observation they match, so a judge that
needs the conversation needs the conversation *there*.
"""

import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional

from k8fy.langfuse_client import get_client

logger = logging.getLogger(__name__)

_enabled: Optional[bool] = None


def enabled() -> bool:
    """True when tracing is switched on and a Langfuse client is available."""
    global _enabled
    if _enabled is None:
        try:
            from config.settings import get_settings
            _enabled = bool(get_settings().langfuse_tracing_enabled)
        except Exception:
            _enabled = False
        if _enabled:
            logger.info("Langfuse tracing enabled")
    return bool(_enabled) and get_client() is not None


class _NullSpan:
    """Stand-in returned when tracing is off, so callers need no conditionals."""

    def update(self, **_kwargs: Any) -> None:
        return None


@contextmanager
def observe(
    name: str,
    *,
    model: str,
    input: Any = None,
    prompt: Any = None,
    session_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
):
    """Record one reasoning call as a Langfuse generation observation.

    Yields an object with `.update(output=..., usage_details=...)`. When tracing
    is disabled — or anything at all goes wrong — yields a no-op so the caller's
    code path is identical either way.

    `prompt` should be the Langfuse prompt object, which links the observation to
    the exact prompt version that produced the answer. That link is what makes
    quality comparable across prompt versions.
    """
    if not enabled():
        yield _NullSpan()
        return

    client = get_client()
    try:
        from langfuse import propagate_attributes
    except Exception:  # pragma: no cover - SDK too old for propagate_attributes
        propagate_attributes = None

    try:
        kwargs: Dict[str, Any] = {"as_type": "generation", "name": name, "model": model}
        if prompt is not None:
            kwargs["prompt"] = prompt
        # session_id is propagated rather than set on the observation so that any
        # nested observation inherits it too (SDK >= 4.14).
        if session_id and propagate_attributes is not None:
            with propagate_attributes(session_id=session_id):
                with client.start_as_current_observation(**kwargs) as span:
                    _safe_update(span, input=input, metadata=metadata)
                    yield span
                    return
        with client.start_as_current_observation(**kwargs) as span:
            _safe_update(span, input=input, metadata=metadata)
            yield span
            return
    except Exception as exc:
        # Tracing must never be the reason a query fails.
        logger.warning("Langfuse tracing failed for %r — continuing untraced: %s", name, exc)
        yield _NullSpan()


def _safe_update(span: Any, **fields: Any) -> None:
    """Best-effort span.update(); swallows SDK signature differences."""
    payload = {k: v for k, v in fields.items() if v is not None}
    if not payload:
        return
    try:
        span.update(**payload)
    except Exception as exc:
        logger.debug("span.update failed (ignored): %s", exc)


def usage_from(response: Any) -> Dict[str, int]:
    """Extract token usage from an Anthropic response for `usage_details`."""
    u = getattr(response, "usage", None)
    if u is None:
        return {}
    return {
        "input": getattr(u, "input_tokens", 0) or 0,
        "output": getattr(u, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def reset_for_tests() -> None:
    """Clear the cached enabled flag."""
    global _enabled
    _enabled = None
