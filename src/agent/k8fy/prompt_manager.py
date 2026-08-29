"""Langfuse-backed prompt management with local fallback.

Prompts are stored and versioned in Langfuse under the label "production".
`resolve()` is called **per request**, not once at import: the Langfuse SDK
caches prompts client-side with a stale-while-revalidate strategy (default TTL
60 s), so a cache hit returns with no network call and an expired entry returns
the stale value *immediately* while refreshing in the background.  Resolving
per request therefore costs ~nothing after the first call and is what makes
promoting the "production" label in the Langfuse UI actually take effect on
live traffic.

  History: until 2026-08-29 this module was called once at module import
  (`_DEFAULT_SYSTEM_PROMPT = get_prompt(...)`) and inside each skill's
  `__init__` via the process-wide SkillRouter singleton.  Both froze the prompt
  for the life of the process, so a label promotion did nothing until the pod
  restarted — while this docstring claimed the opposite.  Resolution is now
  request-time.  See ROADMAP P19 gap B.

If credentials are absent, or a prompt name does not exist in Langfuse, or the
API is unreachable with a cold cache, the local fallback string from prompts.py
is used and `ResolvedPrompt.is_fallback` is True.  The service therefore starts
and runs cleanly with or without Langfuse configured.

Prompt names used by this codebase (all 11 are fetched at runtime; keep
`scripts/migrate_prompts_to_langfuse.py` in step with this list):
  k8fy/system              — general-purpose fallback (K8fyAgent)
  k8fy/health-check        — HealthSkill
  k8fy/cert-audit          — CertAuditSkill
  k8fy/change-history      — ChangeHistorySkill
  k8fy/restart-trend       — RestartTrendSkill
  k8fy/diagnose            — DiagnoseSkill
  k8fy/vault-cert          — VaultCertSkill
  k8fy/incident-responder  — IncidentResponderSkill
  k8fy/deployment-guardian — DeploymentGuardianSkill
  k8fy/chat                — multi-turn Chat page (free-form tool-calling loop)
  k8fy/chat-structure      — reason_chat()'s second call, restructures the
                             free-form answer into the sectioned Chat UI fields
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_langfuse = None
_initialised = False

# Label whose version serves live traffic. The Evaluator Agent (P19) will push
# new *versions* without moving this label; a human promotes it.
PRODUCTION_LABEL = "production"

# How long to stop re-attempting a prompt that just failed to resolve.
#
# Negative caching matters here because resolve() is synchronous and called from
# async request handlers: a failed fetch against an unreachable Langfuse costs
# ~1-3 s of SDK connect/retry, and that blocks the event loop for *every*
# concurrent request, not just the one that triggered it (measured: 0.9-3.1 s per
# call with the API blackholed). Without this, a Langfuse outage would turn into
# a per-request stall. With it, an outage costs one attempt per prompt per
# window; the rest return the local fallback immediately.
#
# The window is deliberately short so that recovery — and a newly promoted
# `production` label — is picked up within a minute, matching the SDK's own
# default cache TTL.
FAILURE_COOLDOWN_SECONDS = 60.0

# name -> monotonic timestamp after which resolution may be retried.
_cooldown_until: Dict[str, float] = {}


@dataclass(frozen=True)
class ResolvedPrompt:
    """A prompt resolved for one request, with the provenance to record on a trace.

    `version` is None when the text came from the local fallback — there is no
    Langfuse version to attribute the answer to in that case.
    """

    name: str
    text: str
    version: Optional[int] = None
    is_fallback: bool = True


def _get_client():
    """Return a Langfuse client, or None if credentials are not configured."""
    global _langfuse, _initialised
    if _initialised:
        return _langfuse

    _initialised = True
    from config.settings import get_settings  # imported lazily to avoid circular deps
    settings = get_settings()

    if not settings.langfuse_public_key:
        logger.info(
            "LANGFUSE_PUBLIC_KEY not set — prompt management disabled; using local prompts"
        )
        return None

    try:
        from langfuse import Langfuse

        creds = {
            "public_key": settings.langfuse_public_key,
            "secret_key": settings.langfuse_secret_key,
        }
        # The server-URL kwarg was renamed between SDK majors: v2 takes `host=`,
        # v3+ take `base_url=` (which also outranks env vars). requirements.txt
        # does not pin a major, so accept either rather than failing closed into
        # permanent local-fallback on a TypeError.
        try:
            _langfuse = Langfuse(base_url=settings.langfuse_base_url, **creds)
        except TypeError:
            _langfuse = Langfuse(host=settings.langfuse_base_url, **creds)

        logger.info(
            "Langfuse prompt management enabled",
            extra={"langfuse_base_url": settings.langfuse_base_url},
        )
    except Exception as exc:
        logger.warning("Langfuse init failed — using local prompts: %s", exc)

    return _langfuse


def resolve(name: str, fallback: str) -> ResolvedPrompt:
    """Resolve *name* from Langfuse (production label) for the current request.

    Safe to call on every request: the SDK serves from its client-side cache and
    revalidates in the background. Falls back to *fallback* if Langfuse is not
    configured, the prompt does not exist, or the API is unreachable with a cold
    cache.
    """
    client = _get_client()
    if client is None:
        return ResolvedPrompt(name=name, text=fallback)

    now = time.monotonic()
    if now < _cooldown_until.get(name, 0.0):
        # A recent attempt failed; skip the network entirely this time.
        return ResolvedPrompt(name=name, text=fallback)

    try:
        prompt = client.get_prompt(name, label=PRODUCTION_LABEL, fallback=fallback)
    except TypeError:
        # Older SDKs without the `fallback=` kwarg.
        prompt = client.get_prompt(name, label=PRODUCTION_LABEL)
    except Exception as exc:
        _cooldown_until[name] = now + FAILURE_COOLDOWN_SECONDS
        logger.warning(
            "Langfuse resolve('%s') failed — using local fallback, not retrying for %.0fs: %s",
            name, FAILURE_COOLDOWN_SECONDS, exc,
        )
        return ResolvedPrompt(name=name, text=fallback)

    # When the SDK serves its own fallback it sets is_fallback and has no version.
    # That means its own fetch failed, so treat it like any other failure.
    if getattr(prompt, "is_fallback", False):
        _cooldown_until[name] = now + FAILURE_COOLDOWN_SECONDS
        return ResolvedPrompt(name=name, text=fallback)

    try:
        text = prompt.compile()
    except Exception as exc:
        logger.warning(
            "Langfuse prompt '%s' failed to compile — using local fallback: %s", name, exc
        )
        return ResolvedPrompt(name=name, text=fallback)

    _cooldown_until.pop(name, None)  # healthy again
    return ResolvedPrompt(
        name=name,
        text=text,
        version=getattr(prompt, "version", None),
        is_fallback=False,
    )


def get_prompt(name: str, fallback: str) -> str:
    """Resolve *name* and return only the prompt text.

    Retained for callers that do not record provenance. Prefer `resolve()`,
    which also returns the version an answer should be attributed to.
    """
    return resolve(name, fallback).text


def prefetch(pairs) -> None:
    """Warm the SDK cache at startup so the first real request never blocks.

    *pairs* is an iterable of (name, fallback). Best-effort: every failure is
    already handled by resolve()'s fallback path, so this never raises.
    """
    if _get_client() is None:
        return
    for name, fallback in pairs:
        rp = resolve(name, fallback)
        logger.info(
            "prompt prefetch: %s → %s",
            name,
            "local fallback" if rp.is_fallback else f"Langfuse v{rp.version}",
        )
