# 0020 – Phase-3 remediation (restart / scale / rollback), gated by mandatory human approval

## Status

Accepted   ·   (date: 2026-07-19)

## Context

[ADR 0003](0003-read-only-to-actions-boundary.md) phases K8fy's read→write
transition: Phase 1 read-only, Phase 2 automatic **certificate renewal only**
(the current state — `VaultCertSkill._renew()`, triggered by a UI click),
Phase 3 "other actions? Restarting pods (high risk), auto-scaling... not in
scope yet."

[Spec 011](../specs/011-agentic-use-cases.md) Use Case 1 (Incident Responder)
and Use Case 2 (Deployment Guardian) both require Phase-3 actions — restart,
scale, rollback — to close the loop from anomaly detection to remediation.
Building them as spec 011 originally described would silently expand the
read-write boundary past what ADR 0003 authorized: Use Case 2 in particular
proposed **auto-rollback with no human review when confidence > 0.9**, which
is a materially different risk posture than the cert-renewal precedent (a
single, narrowly-scoped, easily-reversible action with its own guardrails).

Two things make Phase 3 riskier than Phase 2:
- **Blast radius.** A bad cert renewal breaks TLS for one service. A bad
  restart/scale/rollback can cascade (e.g. rolling back to an image that no
  longer matches a migrated schema).
- **Confidence is not accuracy.** An LLM decision with confidence 0.95 can
  still be wrong; the existing skills have no track record yet at this class
  of action to justify unattended execution.

## Decision

Phase 3 is authorized, but **every** Phase-3 action requires an explicit
human approval step before execution — with no confidence-based bypass, at
any confidence level. Concretely:

1. **Propose, never auto-execute.** `IncidentResponderSkill` and
   `DeploymentGuardianSkill` only ever produce a **proposal**: a structured
   decision (`proposed_action`, `action_params`, `confidence`, `reasoning`,
   `blast_radius`, `evidence`) persisted with status `pending`. Producing a
   proposal makes zero infrastructure calls.
2. **Execution is a second, separate, explicitly-authorized call.** A human
   approves via the Admin Console (`POST /admin/remediation/{id}/approve`) —
   or, since the same endpoint is bearer-token-authenticated and can be
   called by any authorized external service, a future Slack/PagerDuty
   integration could authorize it too. There is no code path from "diagnosis"
   to "action" that skips this call, regardless of confidence score.
3. **Proposals expire.** A TTL (`REMEDIATION_PROPOSAL_TTL_MINUTES`, default
   30) bounds how stale the evidence behind an approval can be — an operator
   approving a proposal against conditions that have since changed is its own
   risk. Expired proposals must be regenerated, not approved.
4. **Approval is idempotent and single-shot.** Approve/reject transition
   `status` with a `WHERE status = 'pending'` guard so a duplicate click or
   webhook retry cannot double-execute.
5. **Write tools are invisible to every Claude-facing tool list.** The
   restart/scale/rollback tools (`action_executor.py`) are never registered
   as tools any Claude call can choose to invoke — not in the proposing
   skills, not in the general chat agent (P12). They are only reachable via
   a deterministic, non-LLM dispatch (`execute_remediation` intent) triggered
   by the approval endpoint. This removes prompt-injection or misinterpreted
   free-form chat as a path to an unattended write.
6. **Verify, don't chain.** After execution, a health check re-runs to
   confirm remediation worked. If it didn't, that result is recorded and
   surfaced — the system does not automatically attempt a second remediation.
7. **Rejected alternative:** confidence-gated auto-execution (spec 011's
   original "confidence > 0.9 → auto-rollback" for Use Case 2). Rejected
   because confidence is a model self-assessment, not a correctness
   guarantee, and Phase 3's blast radius is high enough that the cost of one
   human-in-the-loop click is small relative to the cost of an autonomous
   miss.
8. **Phase 2 (cert renewal) is unchanged.** It remains a lower-risk,
   single-purpose, immediately-reversible action with its own existing
   guardrails; this ADR does not retrofit the approval-gate pattern onto it.

## Amendment (2026-09-12) — an unset auth token now fails closed outside dev

Point 2 above says approve/reject "is bearer-token-authenticated" but never
decided what an **unset** token should mean. The implementation
(`checkRemediationAuth`) picked "open" — the same posture as an unset
`COLLECTOR_TOKEN` — without that being a reviewed decision, and it stayed
that way through the deployed cluster running with `REMEDIATION_AUTH_TOKEN`
unset and `ENV=prod` (flagged as ROADMAP OPS-3, 2026-09-01).

**Why this needed a decision, not just a patch.** Unlike `COLLECTOR_TOKEN`
(scopes which cluster's evidence a push belongs to) or the original
`EVAL_AUTH_TOKEN` default this was modeled on, approve/reject **executes a
real Kubernetes write** — restart, scale, or rollback — the instant it's
called. An unset token here is a materially larger exposure than either of
those, and it sits one config change away from live at any time: nothing
prevents `AUTONOMOUS_REMEDIATION_ENABLED`/`DEPLOY_GUARDIAN_ENABLED` from being
flipped to `"true"` without also setting the token, since the three are
independent flags.

**Decision: fail closed outside dev**, mirroring `EVAL_AUTH_TOKEN`'s own
2026-09-01 amendment (ADR 0030) exactly — `checkRemediationAuth` now checks
`ENV`, and an unset token disables approve/reject entirely when `ENV` is not
`"dev"`/unset, rather than leaving them open.

**The tradeoff, stated plainly, since the alternative has a real cost too.**
Failing closed means a misconfigured deployment (both `ENABLED` flags on,
token forgotten) makes remediation **silently unusable** — every approve/
reject call 401s, with no proposals ever executable — rather than usable-but-
insecure. On a feature whose entire purpose is being available *during an
incident*, "silently broken" is its own failure mode. This was weighed and
accepted anyway: an operator discovers "the button doesn't work" during
setup/testing, before an incident, once, and fixes it by setting the token
(`infra/kubernetes/backend.yaml` now sources it from
`agentify-remediation-secret`, not a literal value) — whereas the insecure
default is discovered, if ever, by someone exploiting it.

**Point 7's rejected alternative is unaffected.** Confidence-gated
auto-execution remains rejected for the reasons already stated; this
amendment only changes what an *absent credential* does at the approval
endpoint, not who is allowed to skip approval.

## Consequences

- **Positive:** unlocks the two highest-value spec 011 use cases (closing the
  loop from detection to remediation) without expanding the autonomous-action
  surface ADR 0003 deliberately limited. The propose/execute split also means
  a bad proposal costs nothing but a rejected click — no infrastructure
  touched.
- **Negative / cost accepted:** remediation is not actually autonomous end to
  end — a human must be available to approve during an incident, which
  reintroduces the toil this was meant to reduce (mitigated by making the
  approval endpoint externally callable, so paging tools can put the decision
  one click away rather than requiring someone to open the console).
- **Negative / cost accepted:** rollback in this pass is change-history-based
  (replays the previous deploy event's recorded images), not a full K8s
  ReplicaSet-revision rollback — sufficient for image-bump deploys, not for
  every possible change shape.
- **Negative / cost accepted (2026-09-12 amendment):** an unset
  `REMEDIATION_AUTH_TOKEN` now disables approve/reject entirely outside dev,
  rather than leaving them open — a misconfigured deployment discovers this
  as "remediation doesn't work" (every decision 401s) instead of as a
  security gap. Judged the better failure mode for a write-capable endpoint.
- **Revisit if:** the team accumulates enough approved/executed history to
  trust a narrower, explicitly-scoped auto-execute path for a single very
  low-risk action (e.g. restart-only, single-replica, non-payments
  namespaces) — that would be a new ADR, not a silent loosening of this one.
