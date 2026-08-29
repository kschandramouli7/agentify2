"""DeploymentGuardianSkill — handles deploy_guardian_check intent (spec 011 Use Case 2).

Pattern A: compares a pre/post-deploy restart-count snapshot (assembled by the
Go DeploymentGuardian poller) and proposes a remediation ONLY when the deploy
looks like it caused a regression. Like IncidentResponderSkill, this call
makes zero infrastructure writes — see ADR 0020. The caller (DeploymentGuardian)
only persists a proposal when the response's `degraded` field is true.
"""

from typing import Any, Dict

from k8fy.agent import K8fyAgent, REMEDIATION_REASONING_SCHEMA
from k8fy.prompts import DEPLOYMENT_GUARDIAN_PROMPT
from models.response import AgentResponse


class DeploymentGuardianSkill(K8fyAgent):
    """Proposes a rollback when a deploy looks like it caused a regression. Never executes one (ADR 0020)."""

    def __init__(self) -> None:
        super().__init__(
            prompt_name="k8fy/deployment-guardian",
            prompt_fallback=DEPLOYMENT_GUARDIAN_PROMPT,
            tools=[],
            output_schema=REMEDIATION_REASONING_SCHEMA,
        )

    async def reason(
        self, intent: str, data: Dict[str, Any], context: Dict[str, Any] | None = None
    ) -> AgentResponse:
        if context is None:
            context = {}
        # data already contains pre_snapshot/post_snapshot — the Go poller
        # assembles both from the temporal spine (k8fy.metrics); no additional
        # prefetch is needed here.
        return await self._reason_pattern_a(intent, data, context, {})
