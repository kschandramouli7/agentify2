# Spec 011 — Agentic AI Use Cases

> Proposed agentic workflows that build directly on agentify's existing
> two-tier architecture, skill pattern, and semantic memory. Each use case
> is self-contained and can be shipped independently.

> **⚠️ Correction (2026-10-18), found during a documentation audit:** Use
> Cases 1 and 2 below have since shipped (`IncidentResponderSkill`,
> `DeploymentGuardianSkill`, `action_executor.py`/`remediation_executor.py`
> — see `src/agent/k8fy/skills/`), but **not** with the design this spec
> describes. [ADR 0020](../decisions/0020-phase-3-remediation-with-approval-gate.md)
> explicitly **rejected** Use Case 2's "confidence > 0.9 → auto-rollback,
> no human review" flow (see its "Rejected alternatives") and shipped a
> mandatory human-approval gate (`checkRemediationAuth`) for every
> remediation action at every confidence level instead — no auto-execute
> path exists at all. The flows below are kept as the original proposal for
> historical context; **ADR 0020 is the current source of truth for what
> actually runs.** Use Cases 3–5 remain unimplemented (no matching code
> found for `CapacityIntelligenceSkill`/`KnowledgeBuilderSkill`/
> `PRReviewSkill`), consistent with their "proposed" status below.

---

## Use Case 1 — Autonomous Incident Responder

**What it does:** Closes the loop from anomaly detection to remediation to postmortem — without human toil at each step.

**Builds on:** P4c investigation loop (spec 009), DiagnoseSkill (spec 005), VaultCertSkill, semantic memory (P8), postmortem generation.

### Flow

```
Anomaly sweep detects degradation
        │
        ▼ (existing: spec 009)
  Diagnose root cause — DiagnoseSkill
        │
        ├── HEALTHY  → log, continue
        │
        └── DEGRADED / UNHEALTHY
                │
                ▼
         IncidentResponderAgent (NEW)
                │
         ┌──────┴──────────────────────────────────────┐
         │  Tool sequence (Pattern A, deterministic):  │
         │  1. get_similar_incidents   ← semantic mem  │
         │  2. get_change_history      ← last deploy?  │
         │  3. get_logs                ← crash reason  │
         └──────┬──────────────────────────────────────┘
                │
                ▼ One Claude call
         Decision: { action, confidence, reason }
                │
         ┌──────┴─────────────────────────────────┐
         │  action = "rollback"                   │→  kubectl rollout undo
         │  action = "restart_pods"               │→  kubectl rollout restart
         │  action = "scale_up"                   │→  kubectl scale --replicas
         │  action = "rotate_cert"                │→  VaultCertSkill._renew()
         │  action = "human_escalation"           │→  Slack alert + stop
         └────────────────────────────────────────┘
                │
                ▼
         Verify remediation (re-run health check)
                │
                ▼
         Generate postmortem → store in Postgres + notify Slack
```

### Why it's agentic
Multi-step autonomous execution with conditional branching. The agent decides WHICH tool to call (rollback vs restart vs scale) based on evidence — not a hardcoded rule. Human escalation remains the default for unknown failure modes (respects ADR 0003 read-only boundary with an explicit opt-in write path).

### New components
- `IncidentResponderSkill` (Pattern A) — pre-fetches similar incidents + logs, one Claude call for the decision
- `action_executor.py` — K8s write tools: `rollout_undo`, `restart_deployment`, `scale_deployment`
- `POST /api/incidents/respond` — backend endpoint, gated behind `AUTONOMOUS_REMEDIATION_ENABLED=true`

---

## Use Case 2 — Deployment Guardian

**What it does:** Every deploy triggers an automated pre/post health comparison. If health degrades post-deploy, the agent rolls back automatically and writes a deploy report.

**Builds on:** Change history (spec 007), health check (spec 002), temporal spine (spec 006), investigation loop (spec 009).

### Flow

```
K8s deploy event ingested → k8fy.events
        │
        ▼
DeploymentGuardianAgent triggered (webhook or poll)
        │
        ├── PRE-DEPLOY snapshot: {healthy_pods, restart_rates, p95_latency}
        │
        ▼ (deploy proceeds normally)
        │
        ├── POST-DEPLOY snapshot (30s after rollout completes)
        │
        ▼ One Claude call: compare pre vs post
        │
        ├── HEALTHY delta (< threshold) → "Deploy verified ✓" notification
        │
        └── DEGRADATION detected
                │
                ├── confidence > 0.9  → auto-rollback + report   # NOT what shipped — see the
                └── confidence < 0.9  → human alert with recommendation  # ⚠️ banner above; ADR 0020
                                                                          # requires human approval always
```

### Why it's agentic
The comparison is not a simple threshold check — Claude reasons about *whether* a change is deploy-caused vs pre-existing, and whether the delta is within normal variance for that service's history.

### New components
- `DeploymentGuardianSkill` — triggered by `change_history` event type
- Pre/post snapshot stored in `deployment_checks` table
- `deploy_report` document generated and stored as incident embedding

---

## Use Case 3 — Capacity Intelligence Agent

**What it does:** Analyzes restart trends, OOM kill events, and CPU/memory usage to predict when a service will breach capacity. Proposes resource changes and optionally applies them.

**Builds on:** Temporal spine (spec 006), restart trend skill, get_metrics_history tool.

### Flow

```
Scheduled sweep (daily) across all services
        │
        ▼
CapacityIntelligenceAgent (Pattern A)
        │
        ├── get_metrics_history(service, 30d, "restarts")
        ├── get_pod_events(filter="OOMKilled")
        ├── get_metrics_history(service, 30d, "cpu_throttle")  ← future: metrics-server
        │
        ▼ One Claude call
        │
        Output: [{
          service,
          finding: "OOMKilled 3x in 7 days; memory limit 512Mi but avg usage 490Mi",
          recommendation: "Increase memory limit to 768Mi",
          urgency: "high" | "medium" | "low",
          kubectl_command: "kubectl set resources deploy/payment-worker -n payments --limits=memory=768Mi"
        }]
        │
        ▼
   Post to Slack channel #platform-capacity-alerts
   Store in recommendations table
   (Optional: auto-apply if urgency=critical AND auto_scale_enabled=true)
```

### Why it's agentic
Combines time-series trend analysis with domain knowledge about K8s resource management to produce prioritized, actionable recommendations — not just raw metric thresholds.

### New components
- `CapacityIntelligenceSkill` (Pattern A)
- `recommendations` table in Postgres: `{service, finding, kubectl_command, urgency, created_at, applied_at}`
- `GET /admin/recommendations` — new admin UI panel

---

## Use Case 4 — SRE Knowledge Builder

**What it does:** Learns from every incident. After each Tier-2 diagnosis, the agent updates a living runbook for that service — adding new failure patterns, confirmed root causes, and resolution steps. Engineers can ask "have we seen this before?" and get a structured answer.

**Builds on:** Semantic memory (P8), incident embeddings, postmortem generation, multi-turn chat (P12).

### Flow

```
Tier-2 diagnose completes → incident_embeddings row stored
        │
        ▼
KnowledgeBuilderAgent (async, after trace persist)
        │
        ├── get_similar_incidents(service, last_3)
        ├── Fetch existing runbook for this service from knowledge_base table
        │
        ▼ One Claude call: diff + update
        │
        ├── If new failure pattern → ADD to runbook
        ├── If known pattern + new resolution → UPDATE existing entry
        └── If already documented → no-op
        │
        ▼
   UPDATE knowledge_base (service, runbook_markdown, updated_at)
```

**Query interface** (via multi-turn chat P12):
```
Engineer: "Have we seen payment-worker crash like this before?"
           → get_similar_incidents + knowledge_base lookup
           → "Yes, 3 times in the last 60 days. Root cause was always
              DB connection pool exhaustion after deploy. Resolved by
              increasing pool_max_size from 10 to 25."
```

### Why it's agentic
The agent reads the existing runbook, understands what's already documented, and makes a judgment call about whether to add, update, or skip — rather than blindly appending every incident.

### New components
- `KnowledgeBuilderSkill` — fires async after every Tier-2 trace
- `knowledge_base` table: `{service, namespace, runbook_markdown, version, updated_at}`
- `GET /admin/knowledge/{namespace}/{service}` — view living runbook in Admin UI
- `get_runbook` tool — available to DiagnoseSkill for richer context

---

## Use Case 5 — PR Review Agent

**What it does:** Reviews Kubernetes manifest and Helm chart changes in pull requests. Flags misconfigurations, resource anti-patterns, and security issues before they reach production. Uses the same two-tier pattern as the K8s observability skills.

**Builds on:** Two-tier architecture (ADR 0006), the Pattern A skill framework (ADR 0017). This is the P9 second-domain use case that proves the architecture generalises.

### Flow

```
POST /api/review  { repo, pr_number, github_token }
        │
        ▼
Tier-1 (deterministic, ~50ms):
  - Missing resource limits/requests
  - Missing liveness/readiness probes
  - Privileged containers or hostNetwork=true
  - Image using :latest tag
  - Missing namespace
  → Returns [{severity, file, line, rule, fix}]
        │
  If issues found OR explicitly requested:
        ▼
Tier-2 — PRReviewSkill (Pattern A):
  Pre-fetch: diff text, existing manifest history (change_history),
             similar past review findings (semantic memory)
  One Claude call:
  → Nuanced review: "This change increases memory limit from 512Mi to
    2Gi on 10 replicas. Total namespace budget would be 20Gi. Verify
    the node group can accommodate this before merging."
        │
        ▼
PRReviewCard (frontend) or GitHub PR comment (via GitHub MCP)
```

### Why it's agentic
The Tier-2 agent reasons across the diff context, historical deployment patterns for that service, and similar past review findings — providing judgment that goes beyond linting rules.

### New components
- `PRReviewSkill` (Pattern A) — `get_pr_diff`, `get_change_history`, `get_similar_incidents` pre-fetch
- `GET /api/review` endpoint, `review_cert` intent in `inferIntent`
- `PRReviewCard` frontend component (same structure as `DiagnosisCard`)
- Optional: GitHub MCP integration to post findings as inline PR comments

---

## Implementation priority

| # | Use case | Effort | Impact | Dependency |
|---|----------|--------|--------|------------|
| 5 | PR review agent | Low–medium | High (P9 roadmap) | None — new domain |
| 4 | SRE knowledge builder | Low | High (semantic memory already built) | P8 complete ✅ |
| 2 | Deployment guardian | Medium | High (automates manual validation) | spec 007 ✅ |
| 1 | Incident responder | Medium–high | Very high (closes loop) | spec 009 ✅, needs write tools |
| 3 | Capacity intelligence | Medium | Medium | metrics-server needed for full signal |

**Start with 5 (PR review) and 4 (knowledge builder):**
- PR review proves the two-tier pattern generalises to a second domain
- Knowledge builder ships on the semantic memory layer already in production
- Both are low-risk (read-only, no cluster writes needed)
