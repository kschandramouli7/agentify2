"""Shared Langfuse client.

One process-wide client, used by both prompt management (`prompt_manager`) and
tracing (`tracing`). Kept in its own module so neither imports the other, and so
there is exactly one place that knows how to construct the client and how to
degrade when Langfuse is not configured.

Everything here fails soft: if credentials are absent or construction raises,
`get_client()` returns None and callers fall back to behaviour that does not
need Langfuse. Nothing in the request path may depend on Langfuse being up.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_client = None
_initialised = False


def get_client():
    """Return the shared Langfuse client, or None if it is not configured.

    Constructed once per process and cached, including the None result — a
    missing or broken configuration is not retried on every request.
    """
    global _client, _initialised
    if _initialised:
        return _client

    _initialised = True
    from config.settings import get_settings  # lazy: avoids a circular import
    settings = get_settings()

    if not settings.langfuse_public_key:
        logger.info("LANGFUSE_PUBLIC_KEY not set — Langfuse features disabled")
        return None

    try:
        from langfuse import Langfuse

        creds = {
            "public_key": settings.langfuse_public_key,
            "secret_key": settings.langfuse_secret_key,
        }
        # The server-URL kwarg was renamed across SDK majors: v2 takes `host=`,
        # v3+ take `base_url=`. requirements.txt pins >=4.14,<5, so base_url is
        # correct — the fallback exists only so a local env on an older SDK
        # degrades to a warning instead of failing closed into no-Langfuse.
        try:
            _client = Langfuse(base_url=settings.langfuse_base_url, **creds)
        except TypeError:
            _client = Langfuse(host=settings.langfuse_base_url, **creds)

        logger.info(
            "Langfuse client ready", extra={"langfuse_base_url": settings.langfuse_base_url}
        )
    except Exception as exc:
        logger.warning("Langfuse init failed — Langfuse features disabled: %s", exc)
        _client = None

    return _client


def reset_for_tests() -> None:
    """Drop the cached client so a test can install its own."""
    global _client, _initialised
    _client = None
    _initialised = False
