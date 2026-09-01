from pydantic import BaseModel
from typing import Optional, Dict, Any, List


class ToolCall(BaseModel):
    """A tool call made by the agent."""

    name: str
    arguments: Dict[str, Any]


class AgentResponse(BaseModel):
    """Response from the K8fy agent."""

    answer: str
    status: Optional[str] = None  # "healthy" | "degraded" | "unhealthy"
    confidence: float = 0.0
    sources: List[str] = []
    reasoning: Optional[str] = None  # Internal reasoning steps (for debugging)
    tool_calls: List[ToolCall] = []
    details: Dict[str, Any] = {}

    # Token usage + indicative cost for this single agent call (Tier-2 only).
    # Populated by _reason_pattern_a(); zero for Tier-1 fast-path answers.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0  # tokens written to prompt cache
    cache_read_input_tokens: int = 0      # tokens served from prompt cache
    estimated_cost_usd: float = 0.0

    # Which tier answered. "tier1" = deterministic, no model call (and so no
    # tokens, no cost, no prompt provenance); "tier2" = an LLM synthesis call.
    # None means the caller should assume its own default — reason_chat is
    # tier2 unless it says otherwise. Recorded so a free deterministic answer
    # is not logged as a paid one, which would distort both the cost figures
    # and any eval that scores routing.
    tier: Optional[str] = None

    # Which prompt produced this answer (spec 004 provenance, ROADMAP P19 gap C).
    # prompt_version is None when the local fallback string was used — there is
    # no Langfuse version to attribute the answer to in that case.
    prompt_name: Optional[str] = None
    prompt_version: Optional[int] = None


class QueryRequest(BaseModel):
    """Request to the agent for reasoning."""

    intent: str
    data: Dict[str, Any]
    context: Dict[str, Any] = {}
    trace_id: Optional[str] = None  # propagated from the backend for cross-service correlation (spec 004)


class FindingDetail(BaseModel):
    """Structured finding from the k8fy/health-check prompt (new format)."""
    resource: str
    status: str  # HEALTHY | DEGRADED | UNHEALTHY
    reason: str


class ServiceHealthDetail(BaseModel):
    """Per-service health summary from the k8fy/health-check prompt (new format)."""
    service: str
    ready_replicas: int = 0
    total_replicas: int = 0
    ready_percent: float = 0.0
    endpoints: int = 0


class RecommendedAction(BaseModel):
    """A recommended next step the UI can render as a runnable action.

    `tool` must be one of the live-diagnostics tool names (validated
    server-side against an explicit allow-list — see app.py's
    /live-tool-call endpoint); this is never a free-text command string.
    """

    label: str
    tool: str
    arguments: Dict[str, Any] = {}


class ReasoningOutput(BaseModel):
    """Structured output the model is constrained to emit (via output_config.format).

    Kept separate from AgentResponse: this is exactly what Claude returns, while
    AgentResponse is the service's wire shape (adds sources, tool_calls, etc.,
    which the agent fills in from provenance rather than the model).

    Supports two prompt formats:
    - Old (k8fy/system, k8fy/diagnose, etc.): answer field is populated.
    - New (k8fy/health-check): headline + summary are populated; answer defaults "".
    """

    # Old-format answer field; optional so the new health-check schema (which omits
    # it) still validates. In _to_agent_response, headline takes precedence.
    answer: str = ""
    status: str = "unknown"  # healthy | degraded | unhealthy | unknown | not_applicable
    confidence: float = 0.0  # 0.0–1.0
    recommendations: List[str] = []

    # New k8fy/health-check fields (empty/None for other prompts).
    headline: str = ""        # e.g. "🟢 checkout-api healthy (5/5 pods ready)"
    summary: str = ""         # ≤40-word prose summary (health-check)
    service_health: Optional[ServiceHealthDetail] = None

    # New k8fy/diagnose fields (empty/None for other prompts).
    incident_summary: str = ""  # ≤50-word outage description
    timeline: List[str] = []    # chronological evidence trail

    # findings: str for old format, FindingDetail for new health-check format.
    findings: List[Any] = []
    likely_cause: Optional[str] = None
    severity: str = "info"  # info | warning | critical

    # Structured, runnable recommendations (chat's "Recommended actions" —
    # distinct from the plain-text `recommendations` above).
    recommended_actions: List[RecommendedAction] = []

    # New remediation-proposal fields (ADR 0020 / spec 011 Use Cases 1+2).
    # Empty/None for every other prompt. This call NEVER executes anything —
    # it only produces the fields a human reviews before approving.
    degraded: bool = True        # whether this situation actually warrants a proposal (see DeploymentGuardianSkill)
    proposed_action: str = ""   # restart_deployment | scale_deployment | rollback_deployment | rotate_cert | human_escalation
    target_deployment: str = ""  # deployment name the action applies to; empty for human_escalation
    target_replicas: Optional[float] = None  # desired replica count — only for scale_deployment
    blast_radius: str = ""       # one sentence: what could go wrong if this is approved
    evidence: List[str] = []     # bullets supporting the decision
    reasoning: str = ""          # why this action, not an alternative
