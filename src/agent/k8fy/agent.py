"""K8fy agent: Claude-powered Kubernetes operations reasoning."""

import functools
import json
import logging
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic
from pydantic import ValidationError

import metrics
from config.claude_client import get_claude_client
from config.settings import get_settings
from k8fy import tracing
from k8fy.prompt_manager import ResolvedPrompt
from k8fy.prompt_manager import resolve as resolve_prompt
from k8fy.prompts import CHAT_STRUCTURE_PROMPT, CHAT_SYSTEM_PROMPT, SYSTEM_PROMPT
from k8fy.live_diagnostics import LIVE_DIAGNOSTIC_TOOLS
from k8fy.tools import TOOLS, process_tool_call
from models.response import AgentResponse, ReasoningOutput, ToolCall

_DEFAULT_TOOLS = TOOLS

# Prompts are resolved per request, not here. Resolving at import froze every
# prompt for the life of the process, so promoting a Langfuse "production" label
# had no effect until the pod restarted (ROADMAP P19 gap B).

# The prompt resolved for the in-flight request. A ContextVar rather than an
# attribute on `self` because K8fyAgent instances are process-wide singletons
# (see SkillRouter): per-request state on the instance would race across
# concurrent queries.
_active_prompt: ContextVar[Optional[ResolvedPrompt]] = ContextVar(
    "k8fy_active_prompt", default=None
)


def _with_system_prompt(method):
    """Resolve this agent's system prompt for the request, then stamp provenance.

    Wraps the reasoning entry points so that (a) the prompt is fetched once per
    request — cheap, since the Langfuse SDK serves from a client-side cache and
    revalidates in the background — and (b) the returned AgentResponse carries
    the prompt name/version that produced it, which is what lets a trace be
    attributed to a specific prompt version.
    """

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        rp = self._resolve_system_prompt(_pin_from(args, kwargs))
        token = _active_prompt.set(rp)
        try:
            result = await method(self, *args, **kwargs)
        finally:
            _active_prompt.reset(token)
        if isinstance(result, AgentResponse):
            result.prompt_name = rp.name
            result.prompt_version = rp.version
        return result

    return wrapper


def _pin_from(args, kwargs) -> Dict[str, Any]:
    """Extract a prompt pin from the request context, if one is present.

    Set only by the version-pinned evaluation endpoint (ADR 0030), which puts
    prompt_label / prompt_version into the query context so a candidate version
    can be gated before promotion. Read here — one place — so every skill honours
    it without its own plumbing.

    Returns {} for ordinary traffic, which resolves the production label as usual.
    """
    ctx = kwargs.get("context")
    if not isinstance(ctx, dict):
        # Positional: the reasoning entry points all take (intent, data, context, ...)
        # or (messages, context, ...). Pick the last dict that looks like a context.
        for a in reversed(args):
            if isinstance(a, dict) and ("prompt_label" in a or "prompt_version" in a):
                ctx = a
                break
    if not isinstance(ctx, dict):
        return {}
    pin: Dict[str, Any] = {}
    if ctx.get("prompt_version"):
        try:
            pin["version"] = int(ctx["prompt_version"])
        except (TypeError, ValueError):
            logger.warning("ignoring non-integer prompt_version %r", ctx.get("prompt_version"))
    elif ctx.get("prompt_label"):
        pin["label"] = str(ctx["prompt_label"])
    return pin


def _traced_chat(method):
    """Record one chat turn as a single Langfuse observation (P19 gap E).

    A whole turn rather than each tool-loop iteration, and `input` is the FULL
    conversation history on purpose: Langfuse evaluators only see data on the
    observation they match, and judging a conversation — or spotting context that
    should have been fetched earlier — needs the conversation to be there.

    Must sit BELOW @_with_system_prompt so that decorator has already resolved
    the prompt into the ContextVar by the time this one reads it; decorators
    apply bottom-up, so the outer one runs first.
    """

    @functools.wraps(method)
    async def wrapper(self, messages, context=None, *args, **kwargs):
        ctx = context or {}
        rp = _active_prompt.get()
        with tracing.observe(
            "chat:turn",
            model=self.model,
            input=messages,
            prompt=rp.raw if rp else None,
            session_id=str(ctx.get("session_id") or ""),
            metadata={"turns": len(messages) if messages else 0},
        ) as span:
            result = await method(self, messages, context, *args, **kwargs)
            if isinstance(result, AgentResponse):
                tracing._safe_update(span, output=result.answer)
            return result

    return wrapper


# Model pair for the advisor/executor strategy.
# Executor (EXECUTOR_MODEL) is the primary model — handles all tool calls cheaply.
# Advisor (ADVISOR_MODEL) is a server-side tool the executor consults mid-generation.
ADVISOR_MODEL = "claude-opus-4-8"
EXECUTOR_MODEL = "claude-sonnet-4-6"

# Indicative retail pricing (USD per million tokens) used for cost estimates.
# Source: https://platform.claude.com/docs/en/about-claude/pricing (June 2026)
# cache_write = 5-minute TTL rate (1.25× base input); cache_read = 0.1× base input.
#
# These defaults are overridden at startup by rates fetched from the backend DB
# (GET /admin/pricing). Keeping them here means the agent still works if the
# backend is temporarily unreachable.
_COST_PER_M: Dict[str, Dict[str, float]] = {
    "claude-fable-5":    {"input": 10.0, "output": 50.0,  "cache_write": 12.50, "cache_read": 1.00},
    "claude-mythos-5":   {"input": 10.0, "output": 50.0,  "cache_write": 12.50, "cache_read": 1.00},
    "claude-opus-4-8":   {"input":  5.0, "output": 25.0,  "cache_write":  6.25, "cache_read": 0.50},
    "claude-opus-4-7":   {"input":  5.0, "output": 25.0,  "cache_write":  6.25, "cache_read": 0.50},
    "claude-opus-4-6":   {"input":  5.0, "output": 25.0,  "cache_write":  6.25, "cache_read": 0.50},
    "claude-opus-4-5":   {"input":  5.0, "output": 25.0,  "cache_write":  6.25, "cache_read": 0.50},
    "claude-sonnet-4-6": {"input":  3.0, "output": 15.0,  "cache_write":  3.75, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input":  3.0, "output": 15.0,  "cache_write":  3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input":  1.0, "output":  5.0,  "cache_write":  1.25, "cache_read": 0.10},
    "claude-haiku-3-5":  {"input":  0.8, "output":  4.0,  "cache_write":  1.00, "cache_read": 0.08},
}


def refresh_pricing_from_backend(backend_url: str) -> None:
    """Fetch live pricing rates from the backend DB and update _COST_PER_M in-place.

    Called once at startup. Falls back silently to the hardcoded defaults if the
    backend is unavailable (cold start, network issue, local dev without DB).
    """
    import httpx  # imported lazily — not needed outside startup

    try:
        resp = httpx.get(f"{backend_url}/admin/pricing", timeout=5.0)
        resp.raise_for_status()
        for entry in resp.json():
            mid = entry.get("model_id", "")
            if not mid:
                continue
            _COST_PER_M[mid] = {
                "input":       float(entry.get("input_per_mtok", 0)),
                "output":      float(entry.get("output_per_mtok", 0)),
                "cache_write": float(entry.get("cache_write_per_mtok", 0)),
                "cache_read":  float(entry.get("cache_read_per_mtok", 0)),
            }
        logger.info("pricing refreshed from backend", extra={"models": len(_COST_PER_M)})
    except Exception as exc:
        logger.warning(
            "could not fetch pricing from backend — using hardcoded defaults: %s", exc
        )

_ADVISOR_BETA = "advisor-tool-2026-03-01"

# Timing guidance prepended to the executor's system prompt when the advisor
# tool is active. Based on the recommended system prompt from the advisor tool
# docs: https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool
# Condensed for the K8fy diagnostic use case (research tasks, not coding).
_ADVISOR_TIMING_GUIDANCE = """\
You have access to an `advisor` tool backed by a stronger model. It takes NO \
parameters — when you call advisor(), your entire conversation history is \
automatically forwarded.

Call advisor BEFORE committing to a diagnostic approach, and again before \
producing your final answer. If orientation is needed first (checking which data \
is already available), do that, then call advisor.

Also call advisor when stuck or when considering a change of approach.

Give the advice serious weight. If empirical evidence from tool results contradicts \
a specific claim, surface the conflict in a follow-up advisor call rather than \
silently switching approach."""

logger = logging.getLogger(__name__)
settings = get_settings()

# JSON schema the model's final answer is constrained to (output_config.format).
# Mirrors models.ReasoningOutput. Structured outputs require additionalProperties
# to be false and don't support numeric min/max, so confidence is a bare number
# (the system prompt asks for 0.0–1.0; we clamp/normalize on the way out).
# Schema for skills that still use the original answer-centric format.
REASONING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Concise operator-facing answer."},
        "status": {
            "type": "string",
            "enum": ["healthy", "degraded", "unhealthy", "unknown", "not_applicable"],
        },
        "confidence": {"type": "number", "description": "0.0–1.0; lower if data is incomplete."},
        "recommendations": {"type": "array", "items": {"type": "string"}, "description": "Prioritized operator actions."},
        # Correlation fields (spec 005); empty/null for single-signal answers.
        "findings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "One short bullet per signal considered (health, cert, …). Empty if not diagnosing.",
        },
        "likely_cause": {
            "type": ["string", "null"],
            "description": "Best-supported hypothesis for a diagnosis; null when signals are insufficient or N/A.",
        },
        "severity": {
            "type": "string",
            "enum": ["info", "warning", "critical"],
        },
    },
    "required": ["answer", "status", "confidence", "recommendations", "findings", "likely_cause", "severity"],
    "additionalProperties": False,
}


# Schema for the updated k8fy/diagnose prompt.
# Adds headline, incident_summary, and timeline alongside the existing answer field.
DIAGNOSE_REASONING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["healthy", "degraded", "unhealthy", "unknown"],
        },
        "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
        "confidence": {"type": "number"},
        "headline": {"type": "string", "description": "One sentence with emoji status indicator."},
        "answer": {"type": "string", "description": "≤15 words: affected pods — confirmed cause or unconfirmed."},
        "incident_summary": {"type": "string", "description": "≤50-word description of impact and affected replicas."},
        "timeline": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Chronological observed evidence trail.",
        },
        "findings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "One evidence item per entry, ordered by severity.",
        },
        "likely_cause": {"type": ["string", "null"]},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "status", "severity", "confidence",
        "headline", "answer", "incident_summary",
        "timeline", "findings", "likely_cause", "recommendations",
    ],
    "additionalProperties": False,
}

# Schema for the updated k8fy/health-check prompt.  headline + summary replace
# answer; findings are structured objects; service_health is a new summary block.
HEALTH_REASONING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["healthy", "degraded", "unhealthy", "unknown"],
        },
        "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
        "confidence": {"type": "number"},
        "headline": {"type": "string", "description": "One sentence with emoji indicator."},
        "summary": {"type": "string", "description": "≤40-word prose (pod count, endpoints, restarts)."},
        "service_health": {
            "type": "object",
            "properties": {
                "service":        {"type": "string"},
                "ready_replicas": {"type": "number"},
                "total_replicas": {"type": "number"},
                "ready_percent":  {"type": "number"},
                "endpoints":      {"type": "number"},
            },
            "required": ["service", "ready_replicas", "total_replicas", "ready_percent", "endpoints"],
            "additionalProperties": False,
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource": {"type": "string"},
                    "status":   {"type": "string", "enum": ["HEALTHY", "DEGRADED", "UNHEALTHY"]},
                    "reason":   {"type": "string"},
                },
                "required": ["resource", "status", "reason"],
                "additionalProperties": False,
            },
        },
        "likely_cause":    {"type": ["string", "null"]},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "status", "severity", "confidence",
        "headline", "summary", "service_health",
        "findings", "likely_cause", "recommendations",
    ],
    "additionalProperties": False,
}


# Schema for the remediation-proposal skills (ADR 0020 / spec 011 Use Cases
# 1+2 — IncidentResponderSkill, DeploymentGuardianSkill). This call NEVER
# executes anything; it only produces the fields a human reviews before
# approving. action_params is flattened into explicit typed fields
# (target_deployment/target_replicas) rather than a free-form object because
# Claude's structured-output schema requires additionalProperties: false.
REMEDIATION_REASONING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "One-sentence summary of the proposed remediation."},
        "status": {"type": "string", "description": "Always 'proposed' — this call never executes anything."},
        "confidence": {"type": "number", "description": "0.0-1.0; lower if evidence is thin."},
        "degraded": {
            "type": "boolean",
            "description": "Whether this situation actually warrants a remediation proposal. IncidentResponderSkill: always true (the incident is already confirmed). DeploymentGuardianSkill: true only if the post-deploy snapshot is measurably worse and attributable to the deploy.",
        },
        "proposed_action": {
            "type": "string",
            "enum": ["restart_deployment", "scale_deployment", "rollback_deployment", "rotate_cert", "human_escalation"],
        },
        "target_deployment": {"type": "string", "description": "Deployment name the action applies to. Empty for human_escalation."},
        "target_replicas": {"type": ["number", "null"], "description": "Desired replica count. Only set for scale_deployment; null otherwise."},
        "blast_radius": {"type": "string", "description": "One sentence: what could go wrong if this action is approved and executed."},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "Bullet list of evidence supporting the decision."},
        "reasoning": {"type": "string", "description": "Why this action was chosen over the alternatives."},
    },
    "required": [
        "answer", "status", "confidence", "degraded", "proposed_action",
        "target_deployment", "target_replicas", "blast_radius", "evidence", "reasoning",
    ],
    "additionalProperties": False,
}


# Schema for reason_chat()'s second, structuring-only call (no tools attached
# — see _structure_chat_answer). Restructures the free-form prose answer the
# unconstrained tool-calling loop already produced into the same sectioned
# shape the dedicated diagnose skill uses, so the Chat UI can render distinct
# sections instead of a wall of markdown text. `recommended_actions` items use
# a nested, explicitly-keyed `arguments` object (rather than a free-form dict)
# because structured outputs require additionalProperties: false at every
# level — same constraint REMEDIATION_REASONING_SCHEMA above works around by
# flattening action_params into named fields.
CHAT_REASONING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["healthy", "degraded", "unhealthy", "unknown"]},
        "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
        "confidence": {"type": "number"},
        "incident_summary": {"type": "string", "description": "One-sentence headline summarizing the current state."},
        "timeline": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Chronological evidence trail — key events with timestamps where known.",
        },
        "findings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Key facts supporting the answer, one per bullet.",
        },
        "likely_cause": {"type": ["string", "null"]},
        "recommendations": {"type": "array", "items": {"type": "string"}, "description": "Plain-text next steps."},
        "recommended_actions": {
            "type": "array",
            "description": (
                "Runnable live-diagnostic commands the UI can execute directly. "
                "Only suggest these when a live (not cached) look would help."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Human-readable action description."},
                    "tool": {"type": "string", "enum": sorted(LIVE_DIAGNOSTIC_TOOLS)},
                    "arguments": {
                        "type": "object",
                        "properties": {
                            "namespace": {"type": ["string", "null"]},
                            "pod": {"type": ["string", "null"]},
                            "container": {"type": ["string", "null"]},
                            "tail_lines": {"type": ["number", "null"]},
                            "previous": {"type": ["boolean", "null"]},
                        },
                        "required": ["namespace", "pod", "container", "tail_lines", "previous"],
                        "additionalProperties": False,
                    },
                },
                "required": ["label", "tool", "arguments"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "status", "severity", "confidence", "incident_summary",
        "timeline", "findings", "likely_cause", "recommendations", "recommended_actions",
    ],
    "additionalProperties": False,
}


class K8fyAgent:
    """K8fy agent for Kubernetes operations reasoning.

    Skills (spec 010) instantiate this with a focused system_prompt and a
    narrower tools list. Pass advisor_model + executor_model to enable the
    advisor/executor strategy via the built-in advisor_20260301 server-side
    tool. Omit them (leave advisor_model=None) for the single-model path.
    """

    def __init__(
        self,
        system_prompt: Optional[str] = None,
        tools: List[Dict[str, Any]] = _DEFAULT_TOOLS,
        advisor_model: Optional[str] = None,
        executor_model: Optional[str] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        prompt_name: str = "k8fy/system",
        prompt_fallback: Optional[str] = None,
    ):
        """Configure the agent.

        Pass `prompt_name` + `prompt_fallback` (the normal case, used by every
        skill) to resolve that Langfuse prompt on each request, falling back to
        the local string. Pass `system_prompt` instead to pin an exact string and
        skip Langfuse entirely — for tests and callers that supply their own text.
        """
        self.client: AsyncAnthropic = get_claude_client()
        self.model = settings.claude_model
        self.max_tokens = settings.claude_max_tokens
        self.effort = settings.claude_effort
        self.backend_url = settings.backend_url
        self.max_iterations = settings.agent_max_tool_iterations
        self._static_system_prompt = system_prompt
        self._prompt_name = prompt_name
        self._prompt_fallback = prompt_fallback or SYSTEM_PROMPT
        self._tools = tools
        self._output_schema = output_schema or REASONING_SCHEMA
        # Advisor/executor mode (advisor_model=None → single-model path).
        self.advisor_model = advisor_model
        self.executor_model = executor_model or self.model

    def _resolve_system_prompt(self, pin: Optional[Dict[str, Any]] = None) -> ResolvedPrompt:
        """Resolve this agent's system prompt for the current request.

        *pin* (from the eval endpoint, ADR 0030) overrides the production label
        with a specific candidate label or version.
        """
        if self._static_system_prompt is not None:
            # Caller pinned an exact string; there is no Langfuse version to
            # attribute an answer to.
            return ResolvedPrompt(name=self._prompt_name, text=self._static_system_prompt)
        return resolve_prompt(self._prompt_name, self._prompt_fallback, **(pin or {}))

    def _system_text(self) -> str:
        """System prompt text for the in-flight request.

        Reads the ContextVar set by @_with_system_prompt; resolves directly if a
        method is called outside that wrapper (e.g. from a test).
        """
        rp = _active_prompt.get()
        if rp is None:
            rp = self._resolve_system_prompt()
        return rp.text

    async def reason(
        self, intent: str, data: Dict[str, Any], context: Optional[Dict[str, Any]] = None
    ) -> AgentResponse:
        """Reason about K8s operations given data and intent."""
        if context is None:
            context = {}
        if self.advisor_model:
            return await self._reason_advisor_executor(intent, data, context)
        return await self._reason_single(intent, data, context)

    # ------------------------------------------------------------------
    # Single-model path (original behaviour, unchanged)
    # ------------------------------------------------------------------

    @_with_system_prompt
    async def _reason_single(
        self, intent: str, data: Dict[str, Any], context: Dict[str, Any]
    ) -> AgentResponse:
        """Agentic loop using one model for both reasoning and tool execution.

        Runs an agentic loop: Claude may call tools (which fetch more data from
        the backend) until it produces a final, schema-constrained answer.
        """
        # System prompt + tools are the stable cache prefix; cache_control caches
        # them together (tools render before system). Note: the prompt is small,
        # so on Opus 4.8 (4096-token cache minimum) it may not actually cache
        # until it grows — the markers are correct and cost nothing meanwhile.
        system = [{"type": "text", "text": self._system_text(), "cache_control": {"type": "ephemeral"}}]
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": self._build_user_message(intent, data, context)}
        ]
        tool_calls_made: List[ToolCall] = []
        iterations = 0
        total_in_tok         = 0
        total_out_tok        = 0
        total_cache_write    = 0
        total_cache_read     = 0

        try:
            for _ in range(self.max_iterations):
                iterations += 1
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": self._output_schema}},
                    tools=self._tools,
                    messages=messages,
                )
                _record_loop_usage(response, self.model)
                usage = getattr(response, "usage", None)
                if usage:
                    total_in_tok      += getattr(usage, "input_tokens",               0) or 0
                    total_out_tok     += getattr(usage, "output_tokens",              0) or 0
                    total_cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
                    total_cache_read  += getattr(usage, "cache_read_input_tokens",     0) or 0

                if response.stop_reason == "tool_use":
                    # Preserve the full assistant turn (incl. thinking blocks) before
                    # appending tool results, then run each requested tool.
                    messages.append({"role": "assistant", "content": response.content})
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            tool_calls_made.append(ToolCall(name=block.name, arguments=block.input))
                            result = await process_tool_call(block.name, block.input, self.backend_url)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result),
                            })
                    messages.append({"role": "user", "content": tool_results})
                    continue

                # Final turn: the text block is schema-valid JSON.
                final_text = next((b.text for b in response.content if b.type == "text"), "")
                metrics.record_request("ok")
                metrics.record_tool_iterations(iterations)
                result = self._to_agent_response(final_text, data, tool_calls_made)
                result.input_tokens                = total_in_tok
                result.output_tokens               = total_out_tok
                result.cache_creation_input_tokens = total_cache_write
                result.cache_read_input_tokens     = total_cache_read
                result.estimated_cost_usd = _estimate_cost(
                    self.model, total_in_tok, total_out_tok,
                    total_cache_write, total_cache_read,
                )
                return result

            logger.warning("agent did not converge within %d iterations", self.max_iterations)
            metrics.record_request("no_converge")
            metrics.record_tool_iterations(iterations)
            return AgentResponse(
                answer="Unable to reach a conclusion within the tool-call budget.",
                status="unknown",
                confidence=0.0,
                sources=_sources_from(data),
                tool_calls=tool_calls_made,
            )

        except Exception as e:  # noqa: BLE001
            logger.error("agent reasoning failed: %s", e)
            metrics.record_request("error")
            return AgentResponse(answer=_user_error_message(e), status="error", confidence=0.0)

    # ------------------------------------------------------------------
    # Advisor/executor path — built-in advisor_20260301 server-side tool
    # ------------------------------------------------------------------

    @_with_system_prompt
    async def _reason_advisor_executor(
        self, intent: str, data: Dict[str, Any], context: Dict[str, Any]
    ) -> AgentResponse:
        """Agentic loop using the built-in advisor_20260301 server-side tool.

        The executor model (self.executor_model, e.g. Sonnet 4.6) is the PRIMARY
        model and handles all K8fy tool calls. The advisor model (self.advisor_model,
        e.g. Opus 4.8) is a server-side tool declared in the tools list; the
        executor consults it mid-generation for strategic guidance.

        Everything runs inside a single /v1/messages call per iteration — no manual
        two-phase split. The server orchestrates the advisor sub-inference; the
        client only executes K8fy tool_use blocks (type=="tool_use"), not the
        advisor (type=="server_tool_use", already handled server-side).

        advisor_tool_result blocks arrive fully formed in response.content and must
        be passed back verbatim on subsequent turns (included automatically when we
        append response.content to messages).

        Cost profile: executor tokens at Sonnet rates + advisor tokens at Opus rates
        (reported separately in usage.iterations, not in top-level usage).
        """
        # Timing guidance must be prepended before the skill prompt so the executor
        # knows when to call the advisor.
        advisor_system_text = _ADVISOR_TIMING_GUIDANCE + "\n\n" + self._system_text()
        system = [{"type": "text", "text": advisor_system_text, "cache_control": {"type": "ephemeral"}}]

        # Soft-limit on advisor output length (docs: ask for ~80% of true ceiling).
        # Placed in the user message so the advisor sees it as a direct instruction.
        user_message = (
            self._build_user_message(intent, data, context)
            + "\n\n(Advisor: please keep your guidance under 150 words — focused diagnosis, not a comprehensive plan.)"
        )

        # Advisor tool definition.
        # max_tokens=2048: docs recommend this as the starting point; reduces mean
        #   advisor output ~7x vs. unset with near-zero truncation.
        # max_uses=3: per-request cap; executor continues without advice once hit.
        # caching: saves cost when the advisor is called 3+ times per conversation;
        #   the cache prefix is stable across calls because each call extends the
        #   previous transcript by one segment.
        advisor_tool: Dict[str, Any] = {
            "type": "advisor_20260301",
            "name": "advisor",
            "model": self.advisor_model,
            "max_tokens": 2048,
            "max_uses": 3,
            "caching": {"type": "ephemeral", "ttl": "5m"},
        }
        tools = [advisor_tool, *self._tools]

        messages: List[Dict[str, Any]] = [{"role": "user", "content": user_message}]
        tool_calls_made: List[ToolCall] = []
        iterations = 0
        # Executor (Sonnet) and advisor (Opus) tokens are billed separately.
        # Top-level usage = executor only; advisor tokens live in usage.iterations.
        exec_in_tok = exec_out_tok = 0
        adv_in_tok  = adv_out_tok  = 0
        exec_cache_write = exec_cache_read = 0
        adv_cache_write  = adv_cache_read  = 0

        try:
            for _ in range(self.max_iterations):
                iterations += 1
                response = await self.client.beta.messages.create(
                    betas=[_ADVISOR_BETA],
                    model=self.executor_model,
                    max_tokens=self.max_tokens,
                    system=system,
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": self._output_schema}},
                    tools=tools,
                    messages=messages,
                )
                # Advisor tokens are in usage.iterations (type:"advisor_message"),
                # not in the top-level usage totals.
                _record_loop_usage(response, self.executor_model, self.advisor_model)
                usage = getattr(response, "usage", None)
                if usage:
                    exec_in_tok      += getattr(usage, "input_tokens",               0) or 0
                    exec_out_tok     += getattr(usage, "output_tokens",              0) or 0
                    exec_cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
                    exec_cache_read  += getattr(usage, "cache_read_input_tokens",     0) or 0
                    for iter_ in (getattr(usage, "iterations", None) or []):
                        if getattr(iter_, "type", "") == "advisor_message":
                            adv_in_tok      += getattr(iter_, "input_tokens",               0) or 0
                            adv_out_tok     += getattr(iter_, "output_tokens",              0) or 0
                            adv_cache_write += getattr(iter_, "cache_creation_input_tokens", 0) or 0
                            adv_cache_read  += getattr(iter_, "cache_read_input_tokens",     0) or 0

                if response.stop_reason == "tool_use":
                    # Preserve the full assistant turn including any server_tool_use
                    # and advisor_tool_result blocks — they must be passed back
                    # verbatim on the next turn.
                    messages.append({"role": "assistant", "content": response.content})
                    tool_results = []
                    for block in response.content:
                        # Only execute K8fy tool_use blocks. server_tool_use blocks
                        # (type=="server_tool_use") are handled server-side; their
                        # advisor_tool_result counterparts are already in response.content.
                        if block.type == "tool_use":
                            tool_calls_made.append(ToolCall(name=block.name, arguments=block.input))
                            result = await process_tool_call(block.name, block.input, self.backend_url)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result),
                            })
                    messages.append({"role": "user", "content": tool_results})
                    continue

                final_text = next((b.text for b in response.content if b.type == "text"), "")
                metrics.record_request("ok")
                metrics.record_tool_iterations(iterations)
                result = self._to_agent_response(final_text, data, tool_calls_made)
                exec_cost = _estimate_cost(
                    self.executor_model, exec_in_tok, exec_out_tok,
                    exec_cache_write, exec_cache_read,
                )
                adv_cost = _estimate_cost(
                    self.advisor_model, adv_in_tok, adv_out_tok,
                    adv_cache_write, adv_cache_read,
                )
                result.input_tokens                = exec_in_tok + adv_in_tok
                result.output_tokens               = exec_out_tok + adv_out_tok
                result.cache_creation_input_tokens = exec_cache_write + adv_cache_write
                result.cache_read_input_tokens     = exec_cache_read + adv_cache_read
                result.estimated_cost_usd          = exec_cost + adv_cost
                return result

            logger.warning("executor did not converge within %d iterations", self.max_iterations)
            metrics.record_request("no_converge")
            metrics.record_tool_iterations(iterations)
            return AgentResponse(
                answer="Unable to reach a conclusion within the tool-call budget.",
                status="unknown",
                confidence=0.0,
                sources=_sources_from(data),
                tool_calls=tool_calls_made,
            )

        except Exception as e:  # noqa: BLE001
            logger.error("advisor/executor reasoning failed: %s", e)
            metrics.record_request("error")
            return AgentResponse(answer=_user_error_message(e), status="error", confidence=0.0)

    # ------------------------------------------------------------------
    # Pattern A — pre-fetch then single call
    # ------------------------------------------------------------------

    async def _fetch(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call one tool against the backend and return its result dict."""
        return await process_tool_call(tool_name, args, self.backend_url)

    @_with_system_prompt
    async def _reason_pattern_a(
        self,
        intent: str,
        data: Dict[str, Any],
        context: Dict[str, Any],
        prefetched: Dict[str, Any],
    ) -> AgentResponse:
        """Pattern A: one Claude call over pre-assembled data, no tool loop.

        The caller is responsible for fetching whatever additional data is
        needed (via _fetch / asyncio.gather) and passing it as `prefetched`.
        That dict is merged into `data` before the prompt is built, so Claude
        sees the full picture in a single turn.

        No tools are declared in the request — the prompt tells Claude that
        all data has been pre-fetched. This eliminates the agentic loop
        entirely for intents whose data requirements are fully predictable.

        Cost profile: N parallel backend fetches + exactly 1 Claude call.
        """
        merged = {**data, **prefetched}
        rp = _active_prompt.get()
        system = [{"type": "text", "text": self._system_text(), "cache_control": {"type": "ephemeral"}}]
        # Append a direct instruction so Claude doesn't wait for tool calls
        # that will never come.
        user_content = (
            self._build_user_message(intent, merged, context)
            + "\n\nAll relevant data has been pre-fetched and is included above. "
            "Produce your final answer directly."
        )

        try:
            with tracing.observe(
                f"skill:{intent}",
                model=self.model,
                input=user_content,
                prompt=rp.raw if rp else None,
                session_id=str(context.get("session_id") or ""),
                metadata={"intent": intent, "pattern": "A"},
            ) as span:
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": self._output_schema}},
                    messages=[{"role": "user", "content": user_content}],
                )
                tracing._safe_update(
                    span,
                    output=next((b.text for b in response.content if b.type == "text"), ""),
                    usage_details=tracing.usage_from(response) or None,
                )
            _record_loop_usage(response, self.model)
            final_text = next((b.text for b in response.content if b.type == "text"), "")
            metrics.record_request("ok")
            metrics.record_tool_iterations(0)  # 0 = pattern A, no loop
            usage = getattr(response, "usage", None)
            in_tok           = getattr(usage, "input_tokens",               0) or 0
            out_tok          = getattr(usage, "output_tokens",              0) or 0
            cache_write_tok  = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read_tok   = getattr(usage, "cache_read_input_tokens",     0) or 0
            cost = _estimate_cost(self.model, in_tok, out_tok, cache_write_tok, cache_read_tok)
            result = self._to_agent_response(final_text, merged, [])
            result.input_tokens                 = in_tok
            result.output_tokens                = out_tok
            result.cache_creation_input_tokens  = cache_write_tok
            result.cache_read_input_tokens      = cache_read_tok
            result.estimated_cost_usd           = cost
            return result
        except Exception as e:  # noqa: BLE001
            logger.error("pattern-a reasoning failed: %s", e)
            metrics.record_request("error")
            return AgentResponse(answer=_user_error_message(e), status="error", confidence=0.0)

    # ------------------------------------------------------------------
    # Multi-turn chat path (free-form, no JSON schema constraint)
    # ------------------------------------------------------------------

    @_with_system_prompt
    @_traced_chat
    async def reason_chat(
        self,
        messages: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResponse:
        """Agentic reasoning over a full multi-turn conversation history.

        Unlike reason() / _reason_pattern_a(), this method:
        - Takes the raw conversation history instead of wrapping a single query
        - Does NOT apply a JSON schema constraint (free-form prose responses)
        - Lets Claude decide which tools to call based on conversational context

        Ideal for the dedicated Chat page where users ask follow-up questions
        and the agent builds on prior turns.
        """
        if context is None:
            context = {}

        system = [{"type": "text", "text": self._system_text(), "cache_control": {"type": "ephemeral"}}]
        chat_messages = list(messages)  # copy so we can append tool results
        tool_calls_made: List[ToolCall] = []
        total_in_tok = total_out_tok = total_cache_write = total_cache_read = 0
        iterations = 0

        try:
            for _ in range(self.max_iterations):
                iterations += 1
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.effort},  # no json_schema — free-form prose
                    tools=self._tools,
                    messages=chat_messages,
                )
                usage = getattr(response, "usage", None)
                if usage:
                    total_in_tok      += getattr(usage, "input_tokens",               0) or 0
                    total_out_tok     += getattr(usage, "output_tokens",              0) or 0
                    total_cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
                    total_cache_read  += getattr(usage, "cache_read_input_tokens",     0) or 0

                if response.stop_reason == "tool_use":
                    chat_messages.append({"role": "assistant", "content": response.content})
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            tool_calls_made.append(ToolCall(name=block.name, arguments=block.input))
                            result = await process_tool_call(block.name, block.input, self.backend_url)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result),
                            })
                    chat_messages.append({"role": "user", "content": tool_results})
                    continue

                # Final turn — extract prose text (no JSON parsing)
                final_text = next((b.text for b in response.content if b.type == "text"), "")
                metrics.record_request("ok")
                metrics.record_tool_iterations(iterations)

                details, structure_usage = await self._structure_chat_answer(final_text, context)
                total_in_tok      += structure_usage[0]
                total_out_tok     += structure_usage[1]
                total_cache_write += structure_usage[2]
                total_cache_read  += structure_usage[3]
                cost = _estimate_cost(
                    self.model, total_in_tok, total_out_tok, total_cache_write, total_cache_read,
                )
                return AgentResponse(
                    answer=final_text,
                    status=details.get("status", "ok") if details else "ok",
                    confidence=1.0,
                    sources=_sources_from(context),
                    tool_calls=tool_calls_made,
                    details=details,
                    input_tokens=total_in_tok,
                    output_tokens=total_out_tok,
                    cache_creation_input_tokens=total_cache_write,
                    cache_read_input_tokens=total_cache_read,
                    estimated_cost_usd=cost,
                )

            logger.warning("chat agent did not converge within %d iterations", self.max_iterations)
            metrics.record_request("no_converge")
            return AgentResponse(
                answer="I wasn't able to reach a conclusion within the tool-call budget. Please try a more specific question.",
                status="error",
                confidence=0.0,
                tool_calls=tool_calls_made,
            )

        except Exception as e:  # noqa: BLE001
            logger.error("chat reasoning failed: %s", e)
            metrics.record_request("error")
            return AgentResponse(answer=_user_error_message(e), status="error", confidence=0.0)

    async def _structure_chat_answer(
        self, answer_text: str, context: Dict[str, Any]
    ) -> "tuple[Dict[str, Any], tuple[int, int, int, int]]":
        """Restructure reason_chat()'s free-form answer into sectioned fields.

        A second, schema-constrained call (not squeezed into the same call as
        the tool loop — this codebase's Pattern A convention never combines
        `tools` and a strict `output_config` schema in one call). Best-effort:
        on any failure this returns an empty dict, and the caller falls back
        to plain-text-only rendering — never breaks the chat response itself.
        """
        usage = (0, 0, 0, 0)
        if not answer_text.strip():
            return {}, usage
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{
                    "type": "text",
                    "text": resolve_prompt("k8fy/chat-structure", CHAT_STRUCTURE_PROMPT).text,
                    "cache_control": {"type": "ephemeral"},
                }],
                output_config={"format": {"type": "json_schema", "schema": CHAT_REASONING_SCHEMA}},
                messages=[{
                    "role": "user",
                    "content": (
                        f"Context: {json.dumps(context)}"
                        f"\n\nAnswer to restructure:\n{answer_text}"
                    ),
                }],
            )
            u = getattr(response, "usage", None)
            if u:
                usage = (
                    getattr(u, "input_tokens", 0) or 0,
                    getattr(u, "output_tokens", 0) or 0,
                    getattr(u, "cache_creation_input_tokens", 0) or 0,
                    getattr(u, "cache_read_input_tokens", 0) or 0,
                )
            text = next((b.text for b in response.content if b.type == "text"), "")
            parsed = ReasoningOutput.model_validate_json(text)
        except Exception as e:  # noqa: BLE001
            logger.warning("chat answer structuring failed, falling back to plain text: %s", e)
            return {}, usage

        details: Dict[str, Any] = {
            "severity": parsed.severity,
            "incident_summary": parsed.incident_summary,
            "timeline": parsed.timeline,
            "findings": parsed.findings,
            "likely_cause": parsed.likely_cause,
            "recommendations": parsed.recommendations,
            "status": parsed.status,
        }
        if parsed.recommended_actions:
            actions = []
            # Resolve once per response (ADR 0023's fleet registry), reused
            # across every action below — never trust the model to supply
            # cluster_id itself (CHAT_REASONING_SCHEMA doesn't even allow it),
            # same distrust already applied to namespace two lines down.
            # Degrades to None (skip resolution) when context lacks either
            # field; resolve_service_clusters itself degrades to [] on any
            # backend/network failure — both cases leave args untouched,
            # which is the correct today's-behavior fallback for a
            # single-cluster deployment with no registered fleet clusters.
            resolved_clusters: Optional[List[str]] = None
            if context.get("namespace") and context.get("service"):
                from k8fy.service_topology import resolve_service_clusters
                resolved_clusters = await resolve_service_clusters(
                    context["namespace"], context["service"], self.backend_url
                )
            for a in parsed.recommended_actions:
                args = {k: v for k, v in a.arguments.items() if v is not None}
                # Never trust the model's own namespace guess (seen defaulting
                # to "default" — a plausible hallucination, not a real value it
                # was told) — the conversation's context already carries the
                # authoritative namespace, so it always wins here regardless
                # of what the model put in arguments. RBAC (agent-live-diagnostics)
                # only grants access within that one namespace anyway.
                if context.get("namespace"):
                    args["namespace"] = context["namespace"]
                # Every live_* tool requires namespace; live_get_pod_logs/
                # live_describe_pod also require pod. Neither context nor the
                # model is guaranteed to supply these (seen: both omitted,
                # which used to silently reach live_list_pods(namespace="")
                # — the K8s API server treats an empty namespace segment as a
                # CLUSTER-scoped request, which the agent's namespace-scoped
                # RBAC Role always 403s on with a confusing error). A button
                # that's guaranteed to fail this way is worse than no button.
                if not args.get("namespace"):
                    logger.warning("dropping recommended action %r: no namespace available", a.tool)
                    continue
                if a.tool in ("live_get_pod_logs", "live_describe_pod") and not args.get("pod"):
                    logger.warning("dropping recommended action %r: no pod available", a.tool)
                    continue
                if resolved_clusters:
                    if args.get("pod"):
                        # A pod name is already cluster-specific — fan-out
                        # can't disambiguate which cluster it's in. Best-
                        # effort: relay to the first resolved cluster rather
                        # than ever falling back to the (possibly wrong)
                        # local one. Known limitation — see ADR 0028.
                        args["cluster_id"] = resolved_clusters[0]
                    elif len(resolved_clusters) == 1:
                        args["cluster_id"] = resolved_clusters[0]
                    else:
                        args["cluster_ids"] = resolved_clusters  # triggers fan-out, see tools.py's _dispatch_live_diagnostic
                actions.append({"label": a.label, "tool": a.tool, "arguments": args})
            details["recommended_actions"] = actions
        return details, usage

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _build_user_message(
        self, intent: str, data: Dict[str, Any], context: Dict[str, Any]
    ) -> str:
        """Build the user message for Claude based on intent and data."""
        return (
            f"Intent: {intent}\n"
            f"Context: {json.dumps(context, indent=2)}\n\n"
            f"Data already fetched for this query:\n{json.dumps(data, indent=2, default=str)}\n\n"
            "Analyze this data and answer the operator's question. If you need more "
            "detail, call a tool to fetch it; otherwise answer directly."
        )

    def _to_agent_response(
        self, final_text: str, data: Dict[str, Any], tool_calls: List[ToolCall]
    ) -> AgentResponse:
        """Validate the model's structured JSON and map it to an AgentResponse."""
        try:
            parsed = ReasoningOutput.model_validate_json(final_text)
        except ValidationError as e:
            logger.warning("structured output validation failed: %s", e)
            # Fall back to returning the raw text rather than dropping the answer.
            return AgentResponse(
                answer=final_text or "No answer produced.",
                confidence=0.3,
                sources=_sources_from(data),
                tool_calls=tool_calls,
            )

        # answer takes precedence when both answer and headline are provided
        # (k8fy/diagnose format: answer is the ≤15-word operational statement,
        #  headline is the emoji summary line — both should reach the frontend).
        # For k8fy/health-check, answer is empty and headline becomes the answer.
        answer = parsed.answer or parsed.headline
        details: Dict[str, Any] = {
            "recommendations": parsed.recommendations,
            "findings": [
                f.model_dump() if hasattr(f, "model_dump") else f
                for f in parsed.findings
            ],
            "likely_cause": parsed.likely_cause,
            "severity": parsed.severity,
        }
        if parsed.recommended_actions:
            details["recommended_actions"] = [a.model_dump() for a in parsed.recommended_actions]
        if parsed.headline:
            details["headline"] = parsed.headline
        if parsed.summary:
            details["summary"] = parsed.summary
        if parsed.service_health is not None:
            details["service_health"] = parsed.service_health.model_dump()
        if parsed.incident_summary:
            details["incident_summary"] = parsed.incident_summary
        if parsed.timeline:
            details["timeline"] = parsed.timeline
        if parsed.proposed_action:
            action_params: Dict[str, Any] = {}
            if parsed.target_deployment:
                action_params["deployment"] = parsed.target_deployment
            if parsed.target_replicas is not None:
                action_params["replicas"] = int(parsed.target_replicas)
            details["degraded"] = parsed.degraded
            details["proposed_action"] = parsed.proposed_action
            details["action_params"] = action_params
            details["blast_radius"] = parsed.blast_radius
            details["evidence"] = parsed.evidence
            details["reasoning"] = parsed.reasoning

        return AgentResponse(
            answer=answer,
            status=parsed.status,
            confidence=_normalize_confidence(parsed.confidence),
            sources=_sources_from(data),
            tool_calls=tool_calls,
            details=details,
        )


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _user_error_message(e: Exception) -> str:
    """Return a user-facing error message that never leaks raw API responses."""
    s = str(e)
    if "rate_limit" in s or "429" in s:
        return "Rate limit reached — too many requests in flight. Please wait a moment and try again."
    if "credit balance" in s.lower() or "billing" in s.lower():
        return "AI service unavailable — the API account has insufficient credits. Please contact your administrator."
    if "timeout" in s.lower() or "timed out" in s.lower():
        return "Request timed out — the query took too long. Try a more specific question or retry."
    if "overloaded" in s.lower() or "529" in s:
        return "The AI service is temporarily overloaded. Please retry in a few seconds."
    return "Analysis failed — an unexpected error occurred. Please try again."


def _record_loop_usage(
    response,
    executor_model: str,
    advisor_model: Optional[str] = None,
) -> None:
    """Record per-iteration token usage, separating executor and advisor turns.

    With the advisor tool active, advisor tokens appear only in usage.iterations
    (type: "advisor_message") and are billed at the advisor model's rates.
    Top-level usage totals reflect executor tokens only.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    iterations = getattr(usage, "iterations", None)
    if iterations:
        for iteration in iterations:
            itype = getattr(iteration, "type", "message")
            model = advisor_model if (advisor_model and itype == "advisor_message") else executor_model
            metrics.record_usage(model, iteration)
    else:
        metrics.record_usage(executor_model, usage)


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Return an indicative USD cost for one Claude call (informational, not billing).

    Accounts for prompt-caching: cache_creation_tokens are billed at the
    5-minute write rate (1.25× base input); cache_read_tokens at 0.1× base input.

    Falls back to prefix matching so versioned IDs like
    'claude-opus-4-8-20251001' resolve correctly even if not in the table.
    """
    rates = _COST_PER_M.get(model)
    if rates is None:
        for key, r in _COST_PER_M.items():
            if model.startswith(key):
                rates = r
                break
    if rates is None:
        rates = _COST_PER_M["claude-opus-4-8"]  # safe conservative default
    return (
        input_tokens          * rates["input"]
        + output_tokens       * rates["output"]
        + cache_creation_tokens * rates["cache_write"]
        + cache_read_tokens     * rates["cache_read"]
    ) / 1_000_000


def _sources_from(data: Dict[str, Any]) -> List[str]:
    """Derive answer provenance from the pod IDs present in the fetched data."""
    return sorted(data.keys())


def _normalize_confidence(value: float) -> float:
    """Clamp confidence to 0.0–1.0, tolerating a model that answers on a 0–100 scale."""
    if value > 1.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


# Create a singleton instance
_agent: Optional[K8fyAgent] = None


def get_k8fy_agent() -> K8fyAgent:
    """Get the K8fy agent instance."""
    global _agent
    if _agent is None:
        _agent = K8fyAgent()
    return _agent


_chat_agent: Optional[K8fyAgent] = None


def get_chat_agent() -> K8fyAgent:
    """Return the agent used for multi-turn chat (chat system prompt, all tools)."""
    global _chat_agent
    if _chat_agent is None:
        _chat_agent = K8fyAgent(
            prompt_name="k8fy/chat", prompt_fallback=CHAT_SYSTEM_PROMPT
        )
    return _chat_agent
