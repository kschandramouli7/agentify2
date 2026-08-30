"""Tests for request-time prompt resolution (ROADMAP P19 gap B).

The load-bearing assertion is that resolution happens **per request**. Until
2026-08-29 every prompt was resolved once — at module import for agent.py's
constants, and in each skill's __init__ via the process-wide SkillRouter
singleton — so promoting a Langfuse "production" label changed nothing until the
pod restarted, while prompt_manager's docstring claimed the opposite. A
regression here is silent in exactly the same way, hence
test_resolution_happens_on_every_call.

The provenance assertions (prompt_name / prompt_version on the response) are
what lets a trace be attributed to the prompt version that produced it, which is
the evidence a prompt-improvement proposal rests on (gap C).
"""

from types import SimpleNamespace

import pytest

from k8fy import agent as agent_mod
from k8fy import prompt_manager
from k8fy.agent import K8fyAgent
from k8fy.prompt_manager import ResolvedPrompt, resolve
from models.response import AgentResponse

FALLBACK = "local fallback prompt"


@pytest.fixture(autouse=True)
def _clear_cooldown():
    """resolve()'s negative cache is module state — reset it around every test."""
    prompt_manager._cooldown_until.clear()
    yield
    prompt_manager._cooldown_until.clear()


class _FakePrompt:
    def __init__(self, text, version=3, is_fallback=False):
        self._text = text
        self.version = version
        self.is_fallback = is_fallback

    def compile(self):
        return self._text


def _client(monkeypatch, prompt=None, raises=None):
    """Install a fake Langfuse client for prompt_manager."""

    def get_prompt(name, label=None, fallback=None, cache_ttl_seconds=None):
        if raises is not None:
            raise raises
        return prompt

    monkeypatch.setattr(
        prompt_manager, "_get_client", lambda: SimpleNamespace(get_prompt=get_prompt)
    )


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------

def test_no_langfuse_client_uses_local_fallback(monkeypatch):
    monkeypatch.setattr(prompt_manager, "_get_client", lambda: None)
    rp = resolve("k8fy/system", FALLBACK)
    assert rp.text == FALLBACK
    assert rp.is_fallback is True
    # No version to attribute an answer to — must not be coerced to 0.
    assert rp.version is None


def test_langfuse_prompt_carries_text_and_version(monkeypatch):
    _client(monkeypatch, prompt=_FakePrompt("live prompt", version=12))
    rp = resolve("k8fy/diagnose", FALLBACK)
    assert rp.text == "live prompt"
    assert rp.version == 12
    assert rp.is_fallback is False
    assert rp.name == "k8fy/diagnose"


def test_api_error_falls_back_without_raising(monkeypatch):
    _client(monkeypatch, raises=RuntimeError("langfuse unreachable"))
    rp = resolve("k8fy/system", FALLBACK)
    assert rp.text == FALLBACK
    assert rp.is_fallback is True
    assert rp.version is None


def test_sdk_served_fallback_is_reported_as_fallback(monkeypatch):
    # The SDK can return its own fallback object when the cache is cold and the
    # API is unreachable; that is not a real version.
    _client(monkeypatch, prompt=_FakePrompt(FALLBACK, version=None, is_fallback=True))
    rp = resolve("k8fy/system", FALLBACK)
    assert rp.is_fallback is True
    assert rp.version is None


def test_uncompilable_prompt_falls_back(monkeypatch):
    class Broken(_FakePrompt):
        def compile(self):
            raise ValueError("bad template variable")

    _client(monkeypatch, prompt=Broken("unused"))
    rp = resolve("k8fy/system", FALLBACK)
    assert rp.text == FALLBACK
    assert rp.is_fallback is True


# ---------------------------------------------------------------------------
# K8fyAgent wiring
# ---------------------------------------------------------------------------

def test_resolution_happens_on_every_call(monkeypatch):
    """The regression guard: a prompt must not be frozen for the process."""
    versions = iter(range(1, 100))

    def fake_resolve(name, fallback):
        v = next(versions)
        return ResolvedPrompt(name=name, text=f"prompt v{v}", version=v, is_fallback=False)

    monkeypatch.setattr(agent_mod, "resolve_prompt", fake_resolve)

    a = K8fyAgent(prompt_name="k8fy/health-check", prompt_fallback=FALLBACK)
    first = a._resolve_system_prompt()
    second = a._resolve_system_prompt()

    assert (first.text, first.version) == ("prompt v1", 1)
    assert (second.text, second.version) == ("prompt v2", 2), (
        "system prompt was cached for the life of the agent — a Langfuse label "
        "promotion would not reach live traffic until restart"
    )


def test_pinned_system_prompt_skips_langfuse(monkeypatch):
    calls = []
    monkeypatch.setattr(
        agent_mod, "resolve_prompt", lambda n, f: calls.append(n) or ResolvedPrompt(n, "x")
    )

    a = K8fyAgent(system_prompt="exact text")
    rp = a._resolve_system_prompt()

    assert rp.text == "exact text"
    assert rp.version is None  # nothing to attribute
    assert calls == []


@pytest.mark.asyncio
async def test_entry_point_stamps_prompt_provenance(monkeypatch):
    monkeypatch.setattr(
        agent_mod,
        "resolve_prompt",
        lambda n, f: ResolvedPrompt(name=n, text="live", version=9, is_fallback=False),
    )

    @agent_mod._with_system_prompt
    async def entry(self):
        # Inside the wrapper the resolved text is what the request should use.
        assert self._system_text() == "live"
        return AgentResponse(answer="ok")

    a = K8fyAgent(prompt_name="k8fy/diagnose", prompt_fallback=FALLBACK)
    resp = await entry(a)

    assert resp.prompt_name == "k8fy/diagnose"
    assert resp.prompt_version == 9


@pytest.mark.asyncio
async def test_fallback_answer_records_no_version(monkeypatch):
    monkeypatch.setattr(prompt_manager, "_get_client", lambda: None)

    @agent_mod._with_system_prompt
    async def entry(self):
        return AgentResponse(answer="ok")

    a = K8fyAgent(prompt_name="k8fy/diagnose", prompt_fallback=FALLBACK)
    resp = await entry(a)

    assert resp.prompt_name == "k8fy/diagnose"
    assert resp.prompt_version is None


# ---------------------------------------------------------------------------
# Negative cache
# ---------------------------------------------------------------------------
# resolve() is synchronous and called from async request handlers, so a failed
# fetch against an unreachable Langfuse blocks the event loop for every
# concurrent request (measured 0.9-3.1 s per call with the API blackholed).
# Per-request resolution therefore must not re-attempt a known-failing prompt.

def test_failed_resolve_is_not_retried_during_cooldown(monkeypatch):
    calls = []

    def get_prompt(name, label=None, fallback=None, cache_ttl_seconds=None):
        calls.append(name)
        raise RuntimeError("langfuse unreachable")

    monkeypatch.setattr(
        prompt_manager, "_get_client", lambda: SimpleNamespace(get_prompt=get_prompt)
    )

    first = resolve("k8fy/diagnose", FALLBACK)
    second = resolve("k8fy/diagnose", FALLBACK)
    third = resolve("k8fy/diagnose", FALLBACK)

    assert first.text == second.text == third.text == FALLBACK
    assert len(calls) == 1, (
        f"expected 1 network attempt then cooldown, got {len(calls)} — an outage "
        "would stall the event loop on every request"
    )


def test_cooldown_is_per_prompt_name(monkeypatch):
    calls = []

    def get_prompt(name, label=None, fallback=None, cache_ttl_seconds=None):
        calls.append(name)
        raise RuntimeError("langfuse unreachable")

    monkeypatch.setattr(
        prompt_manager, "_get_client", lambda: SimpleNamespace(get_prompt=get_prompt)
    )

    resolve("k8fy/diagnose", FALLBACK)
    resolve("k8fy/health-check", FALLBACK)

    # One prompt failing must not suppress attempts for a different prompt.
    assert calls == ["k8fy/diagnose", "k8fy/health-check"]


def test_cooldown_expires_and_allows_retry(monkeypatch):
    calls = []

    def get_prompt(name, label=None, fallback=None, cache_ttl_seconds=None):
        calls.append(name)
        raise RuntimeError("langfuse unreachable")

    monkeypatch.setattr(
        prompt_manager, "_get_client", lambda: SimpleNamespace(get_prompt=get_prompt)
    )
    monkeypatch.setattr(prompt_manager, "FAILURE_COOLDOWN_SECONDS", 0.0)

    resolve("k8fy/diagnose", FALLBACK)
    resolve("k8fy/diagnose", FALLBACK)

    assert len(calls) == 2, "a zero-length cooldown must not suppress the retry"


def test_recovery_clears_the_cooldown(monkeypatch):
    state = {"fail": True}
    calls = []

    def get_prompt(name, label=None, fallback=None, cache_ttl_seconds=None):
        calls.append(name)
        if state["fail"]:
            raise RuntimeError("langfuse unreachable")
        return _FakePrompt("recovered prompt", version=4)

    monkeypatch.setattr(
        prompt_manager, "_get_client", lambda: SimpleNamespace(get_prompt=get_prompt)
    )
    monkeypatch.setattr(prompt_manager, "FAILURE_COOLDOWN_SECONDS", 0.0)

    assert resolve("k8fy/diagnose", FALLBACK).is_fallback is True
    state["fail"] = False
    good = resolve("k8fy/diagnose", FALLBACK)

    assert good.text == "recovered prompt"
    assert good.version == 4
    assert "k8fy/diagnose" not in prompt_manager._cooldown_until


def test_sdk_served_fallback_also_arms_the_cooldown(monkeypatch):
    # is_fallback means the SDK's own fetch failed, so it must count as a failure.
    calls = []

    def get_prompt(name, label=None, fallback=None, cache_ttl_seconds=None):
        calls.append(name)
        return _FakePrompt(FALLBACK, version=None, is_fallback=True)

    monkeypatch.setattr(
        prompt_manager, "_get_client", lambda: SimpleNamespace(get_prompt=get_prompt)
    )

    resolve("k8fy/system", FALLBACK)
    resolve("k8fy/system", FALLBACK)

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Version-pinned resolution (ADR 0030 / P19 gap D1)
# ---------------------------------------------------------------------------
# The eval endpoint pins a candidate so it can be gated BEFORE the production
# label moves. Two properties matter: the pin must actually reach the SDK, and a
# pinned failure must not poison production's cooldown (or vice versa) — they are
# different prompts as far as availability is concerned.

def test_pinned_version_is_passed_to_the_sdk(monkeypatch):
    seen = {}

    def get_prompt(name, label=None, version=None, fallback=None, cache_ttl_seconds=None):
        seen.update(name=name, label=label, version=version, ttl=cache_ttl_seconds)
        return _FakePrompt("candidate text", version=version or 0)

    monkeypatch.setattr(
        prompt_manager, "_get_client", lambda: SimpleNamespace(get_prompt=get_prompt)
    )

    rp = resolve("k8fy/diagnose", FALLBACK, version=7)

    assert rp.text == "candidate text"
    assert seen["version"] == 7
    assert seen["label"] is None, "a version pin must not also send a label"
    assert seen["ttl"] == 0, "a pinned resolve must bypass the cache"


def test_pinned_label_is_passed_to_the_sdk(monkeypatch):
    seen = {}

    def get_prompt(name, label=None, version=None, fallback=None, cache_ttl_seconds=None):
        seen.update(label=label, version=version, ttl=cache_ttl_seconds)
        return _FakePrompt("staging text", version=4)

    monkeypatch.setattr(
        prompt_manager, "_get_client", lambda: SimpleNamespace(get_prompt=get_prompt)
    )

    rp = resolve("k8fy/diagnose", FALLBACK, label="staging")

    assert rp.version == 4
    assert seen["label"] == "staging"
    assert seen["version"] is None
    assert seen["ttl"] == 0


def test_unpinned_resolution_still_uses_production_and_the_cache(monkeypatch):
    seen = {}

    def get_prompt(name, label=None, version=None, fallback=None, cache_ttl_seconds=None):
        seen.update(label=label, version=version, ttl=cache_ttl_seconds)
        return _FakePrompt("prod text", version=2)

    monkeypatch.setattr(
        prompt_manager, "_get_client", lambda: SimpleNamespace(get_prompt=get_prompt)
    )

    resolve("k8fy/diagnose", FALLBACK)

    assert seen["label"] == "production"
    assert seen["version"] is None
    assert seen["ttl"] is None, "normal traffic must keep the SDK cache"


def test_a_broken_candidate_does_not_suppress_production(monkeypatch):
    calls = []

    def get_prompt(name, label=None, version=None, fallback=None, cache_ttl_seconds=None):
        calls.append(("version" if version else "label", version or label))
        if version:
            raise RuntimeError("candidate version is broken")
        return _FakePrompt("prod text", version=2)

    monkeypatch.setattr(
        prompt_manager, "_get_client", lambda: SimpleNamespace(get_prompt=get_prompt)
    )

    # An eval against a broken candidate arms a cooldown...
    assert resolve("k8fy/diagnose", FALLBACK, version=99).is_fallback is True
    assert resolve("k8fy/diagnose", FALLBACK, version=99).is_fallback is True  # cooled down

    # ...but production traffic for the same prompt must be unaffected.
    prod = resolve("k8fy/diagnose", FALLBACK)
    assert prod.is_fallback is False
    assert prod.text == "prod text"
    assert calls.count(("version", 99)) == 1, "candidate retried despite cooldown"
