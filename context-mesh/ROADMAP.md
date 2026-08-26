# agentify — Roadmap & Backlog

> Prioritized record of decisions/work **not yet acted on**, so they aren't lost.
> Seeded from an external design review (2026-06-01) plus our own assessment.
> The one item being acted on *now* — the two-tier query path — is recorded as an
> accepted decision in [ADR 0006](decisions/0006-two-tier-query-path.md), not here.

## Important correction to the review's premise

The review assessed the **docs only** ("no repo attached") and concluded there was
no working vertical slice. As of 2026-06-01 there **is** one: `ingest → store →
query → agent → answer` was validated live (K8s-shaped event → pod formation →
Redis → routed query → Opus 4.8 → correct health verdict). So the review's #1
("stop everything, build one slice") is already done; the items below are about
**hardening and cutting**, not first-time build.

## Priority ladder

| # | Item | Status | Lands in |
|---|------|--------|----------|
| **P1** | Two-tier query path (deterministic fast-path + agentic) | **✅ Done (validated 2026-06-01: health query 13.3s→1ms, 0 LLM calls)** | [ADR 0006](decisions/0006-two-tier-query-path.md) |
| **P2a** | Egress / redaction / data-governance gate | **✅ v1 done (2026-06-01: allowlist redaction live; in-region client = follow-up)** | [ADR 0007](decisions/0007-egress-data-governance.md) + [policy](policies/data-governance.md) |
| **P2b** | Collapse storage to a single **Postgres** store (Redis removed; pgvector deferred) | **✅ v1 done (2026-06-02: current_state+events tables; validated on real PG via embedded-postgres)** | [ADR 0010](decisions/0010-postgres-single-store.md) + [storage-strategy](policies/storage-strategy.md) |
| **P2c** | Multi-provider / per-tenant model routing (in-region: Bedrock/Vertex/Foundry) | Proposed — **deferred until a client requires it** | [ADR 0008](decisions/0008-multi-provider-model-routing.md); now per-**deployment** (P3a resolved) |
| **P3a** | Multi-tenancy / isolation model | **Superseded 2026-08-02 — ADR 0009's single-tenant-per-deployment call reversed by [ADR 0022](decisions/0022-multi-tenant-fleet-hub.md): row-level `tenant_id` + Postgres RLS** | [ADR 0009](decisions/0009-tenancy-single-tenant-per-deployment.md) (superseded), [ADR 0022](decisions/0022-multi-tenant-fleet-hub.md) |
| **P3b** | Audit / answer provenance (trace_id + structured `query.trace`) | **✅ v1 done (2026-06-03: trace_id returned + propagated; retrieval API deferred)** | [spec 004](specs/004-query-provenance.md) |
| **P3c** | Self-observability (instrument the pipeline) | **✅ v1 done (2026-06-03: Prometheus /metrics — tier split, LLM, ingest, HTTP)** | [ADR 0011](decisions/0011-self-observability-metrics.md) |
| **P4a** | Agentic root-cause correlation (deepen Tier 2) | **✅ v1 done (2026-06-04: `diagnose` intent + multi-signal fan-out → Tier-2 correlation; findings/likely_cause/severity; live-validated)** | [spec 005](specs/005-root-cause-correlation.md), [correlation](policies/correlation.md) |
| **P4b** | Temporal spine (restart time-series → causal diagnosis); classical ML deferred | **✅ spine done (2026-06-05: k8fy.metrics append-only samples, windowed query, get_metrics_history tool); ML still Proposed** | [spec 006](specs/006-temporal-ingestion-and-history.md), [ADR 0013](decisions/0013-temporal-data-in-postgres-events-table.md) |
| **P4b-ops** | Operational context for causal diagnosis: deploy/change events + on-demand pod logs | **✅ v1 done (2026-06-05: `k8fy.events` deploy events + `get_change_history`; ephemeral redacted log tail via `get_pod_logs`; live-validated deploy↔onset correlation). Hardened 2026-06-06: events-table retention janitor ([ADR 0015](decisions/0015-events-table-retention.md)) + bearer-token auth on the adapter `/logs` surface.** | [spec 007](specs/007-change-events.md), [spec 008](specs/008-on-demand-pod-logs.md), [ADR 0014](decisions/0014-on-demand-ephemeral-log-fetch.md) |
| **P4c** | Investigation-on-anomaly loop (human-in-loop, **no** auto-remediation) | **✅ v1 done (2026-06-06: opt-in periodic deterministic sweep → diagnose → Slack-compatible webhook; namespace incident dedup + cooldown + per-sweep cap; redacted egress; read-only)** | [spec 009](specs/009-investigation-on-anomaly.md), [ADR 0016](decisions/0016-proactive-investigation-loop.md); respects [ADR 0003](decisions/0003-read-only-to-actions-boundary.md) |
| **P5** | Pattern A standardisation across all skill classes (deterministic pre-fetch + single Claude call per intent) | **✅ Done (2026-06-11: all 5 skills on Pattern A; DiagnoseSkill advisor/executor removed; [ADR 0026](decisions/0026-pattern-a-skills-standardisation.md))** | [spec 010](specs/010-skill-router.md), [ADR 0026](decisions/0026-pattern-a-skills-standardisation.md) |
| **P5+** | Supporting tooling: AI gateway (semantic cache/budgets), eval harness + tool-call budgets, agent tracing | Later | ops/spec |
| **P6** | HashiCorp Vault integration — cert management + autonomous rotation | **✅ Scaffold done (2026-06-17)** — open items: Vault HA, Terraform provider, dynamic secrets |
| **P7** | **Eval harness as CI gate** — Langfuse dataset + CI eval step | **✅ Done (2026-06-25)** — `scripts/seed_eval_dataset.py` + `scripts/run_evals.py` + 02-deploy.yml gate; `intent`+`tier` added to QueryResponse | [ADR 0019](decisions/0019-eval-harness-as-ci-gate.md) |
| **P8** | RAG + pgvector + semantic memory (third memory layer) | After P7 | [ADR 0018](decisions/0018-three-layer-memory-architecture.md) |
| **P9** | PR review agent — second domain use case proving two-tier generalises | **Not started. Architecture decision (2026-07-20): build as its own deployable agent, not a `SkillRouter` entry in `src/agent`** — see below | — |
| **P10** | Context management at scale — budget-aware truncation, summarisation | Alongside P9 | — |
| **P11** | Multi-provider routing: Bedrock stub | After P9 | [ADR 0008](decisions/0008-multi-provider-model-routing.md) |
| **P12** | Multi-turn conversational chat — dedicated Chat nav page | After P11 | Architecture decided 2026-06-17 |
| **P13** | Agentic use cases expansion | **Use Cases 1+2 done (2026-07-20)** — see below; 3/4/5 not started | [spec 011](specs/011-agentic-use-cases.md), [ADR 0020](decisions/0020-phase-3-remediation-with-approval-gate.md) |
| **P14** | Split out two standalone agents: remediation executor (security isolation) + PR review agent (second domain) | **Next up (agreed 2026-07-20)** — see below | — |
| **P15** | Pull-based log-platform connector (Splunk first, Elasticsearch/OpenSearch second) — replaces direct-cluster log fetch with a query-time read against wherever logs already land | Test harness (Fargate+Firehose+S3/Athena) built 2026-07-21/22 — connector code itself not started | [spec 008](specs/008-on-demand-pod-logs.md), [ADR 0014](decisions/0014-on-demand-ephemeral-log-fetch.md) (extends, does not revisit), [ADR 0021](decisions/0021-log-platform-test-infra.md) |
| **P16** | Multi-cluster connector — wire the existing `Integration` model into runtime routing (currently admin-only bookkeeping) | Proposed (2026-07-21), revised 2026-08-02 for tenant-scoping (`Integration` gains `tenant_id`) — see below | `internal/models/integration.go`, `internal/api/handlers.go` (`HandleResolveCluster`) |
| **P17** | Multi-cluster access for the live-diagnostics tools | **Superseded 2026-08-02 by [ADR 0022](decisions/0022-multi-tenant-fleet-hub.md)** — the central-agent-pulls-via-STS design replaced by [P18](#p18--deterministic-per-cluster-fleet-collector--multi-tenant-hub-ingest-proposed-2026-08-02-revised-2026-08-02-replaces-p17)'s deterministic per-cluster collector; see below | `decisions/0022-multi-tenant-fleet-hub.md` |
| **P18** | Deterministic per-cluster fleet collector + multi-tenant Hub ingest (replaces P17) | Proposed (2026-08-02) — **use cases #1 (namespace/service/deployment inventory), #2 (service-dependency mining), and #9 (on-demand live drill-down) shipped 2026-08-03; #3 (ingress/entry-point mapping) and #5 (fleet-wide health/version snapshots) shipped 2026-08-04, #4 (cross-cluster dependency edges) confirmed 2026-08-04, all as `agentify-discovery`**; use case #2 extended 2026-08-18 with a Glue/Athena-based miner (ADR 0029); use cases #6-#8 not started — see below | `decisions/0022-multi-tenant-fleet-hub.md`, `decisions/0029-glue-based-dependency-mining.md`, `src/adapters/discovery/`, `src/agent/k8fy/service_topology.py`, `src/agent/k8fy/dependency_miner.py`, `src/backend/internal/api/collector_hub.go` |

---

## P2a — Egress / redaction / data-governance gate

**Review (confidence: Certain):** the K8fy adapter holds a ClusterRole that reads
Secrets, and the flow ships raw pod data (secret names, namespaces, cert metadata,
possibly payloads) to an external API with no redaction — a procurement-killing
finding for enterprise security review. Make egress destinations configurable
(some customers require Bedrock/in-region or a self-hosted model).

**Our assessment:** Agree, and it's the top *enterprise-gating* item. We confirmed
the flow sends raw fetched pod data to Anthropic with zero redaction. Two-tier
(P1) helps a little — deterministic answers never leave the boundary — but Tier 2
still egresses. **Build a redaction/classification gate before anything reaches a
model, and make the model endpoint pluggable** (first-party / Bedrock / Vertex /
Foundry — Claude is **not** self-hostable; see [P2c](#p2c--multi-provider-model-routing)).
Not blocking the working slice; blocking any enterprise pilot.

**Status (v1 done, 2026-06-01):** allowlist redaction implemented at the backend→
agent boundary (`internal/governance`, applied in `/api/query` + `/api/agent/fetch`),
validated live (secrets in payload dropped before egress), unit-tested. Endpoint is
overridable via `ANTHROPIC_BASE_URL`. **Still open:** first-class in-region clients
(Bedrock/Vertex/Foundry — see [P2c](#p2c--multi-provider-model-routing)); redacting
the operator's free-text question; per-tenant classification (needs P3a). See
[ADR 0007](decisions/0007-egress-data-governance.md) and [policy](policies/data-governance.md).

## P2b — Storage consolidation (Postgres + pgvector spine)

**Review (Likely/Certain):** real data shapes are three (append-only/time-series,
current-state snapshots, free text), not six. Run Postgres + pgvector as the spine
(relational + JSONB + vector in one transactional system; ceiling ~50M vectors,
far above us), drop Weaviate/Redis from the MVP, add S3+Parquet+DuckDB for cheap
history, and only adopt ClickHouse/Timescale or a dedicated vector DB (Qdrant/
Milvus) when volume/filtered-ANN-at-scale justifies it.

**Our assessment:** Agree on the implementation; **keep the pod-mesh concept** (the
product's brain — ADRs [0001](decisions/0001-adopt-context-mesh-architecture.md),
[0002](decisions/0002-pods-are-recursive.md)). The review slightly conflates
"6 stores" with the mesh idea — they're separable. Collapsing the engines makes
`store_type` mostly `relational` and shrinks the trait→store matrix to a routing
detail, which also removes most of the justification for an auto refinement-loop
(a feature deletion = a win). We already felt this pain: the polyglot stack
wouldn't run locally. Sequencing note: P3a is resolved
([ADR 0009](decisions/0009-tenancy-single-tenant-per-deployment.md)) —
**single-tenant per deployment**, so the Postgres schema is single-tenant (no
`tenant_id`/RLS).

**Status (v1 done, 2026-06-02, [ADR 0010](decisions/0010-postgres-single-store.md)):**
collapsed to **one Postgres** — `current_state` table (kv / latest-wins, replaced
Redis) + `events` table (relational). Redis removed from runtime, config, deps, and
package tree. **pgvector deferred** (no semantic-search feature — YAGNI); Weaviate
left inert as the documented future vector option. Validated on a real Postgres via
`embedded-postgres` (no Docker). **Still open:** pgvector when similarity search
lands; S3+Parquet+DuckDB cold history; ClickHouse/Timescale at time-series volume.

## P2c — Multi-provider model routing

**Why:** at multi-client scale, clients differ on data residency, compliance,
cloud, and billing. "Self-hosted Claude" is not possible; the in-boundary options
are provider-operated: **Bedrock/Vertex/Foundry** (data in the customer's cloud
account/region) or **Claude Platform on AWS** (full features + AWS billing, but
inference is Anthropic-operated — data may leave AWS).

**Our assessment:** verified (2026-06-02) that **agentify uses only the portable
Messages-API surface** (tool use, structured outputs, caching, adaptive thinking) —
all supported on Bedrock; it uses no Managed Agents/server-side tools. So Bedrock is
viable with **zero functional loss**. Recommended shape: a per-tenant client factory
`{provider, region, model_id, credentials}`; keep agentify on the portable surface
to preserve portability; prefer **BYO-cloud** billing (client's own Bedrock/Vertex
account) for data-sensitive clients.

**Status:** **deferred — do not build until a paying client requires it.** Gated on
P3a (provider/region/creds are tenant attributes). Full analysis in
[ADR 0008](decisions/0008-multi-provider-model-routing.md).

## P3a — Multi-tenancy / isolation model

**Review (Certain):** no tenant concept anywhere; "namespace" is a K8s namespace,
not a tenant boundary. Retrofitting tenancy after the schema is set is one of the
most expensive migrations there is — pick row-level vs schema vs DB-per-tenant now.

**Our assessment / Resolution (2026-06-02, [ADR 0009](decisions/0009-tenancy-single-tenant-per-deployment.md)):**
The review's framing assumed a shared SaaS. It isn't: agentify is **single-tenant
per deployment** (confirmed), so the deployment *is* the isolation boundary and
**no in-app multi-tenancy is built** — no `tenant_id` rows, no RLS, no per-tenant
schemas. A constant `DEPLOYMENT_ID` seam is reserved for fleet observability + a
future migration path. Row-level isolation is the menu to revisit **only if** the
GTM shifts to shared multi-tenant SaaS — and it must be done before P2b's schema
hardens. Cost accepted: fleet ops (N deployments).

## P3b — Audit / answer provenance

**Review:** enterprises will ask "why did the AI say that, and what did it see?"
We return `sources` already; extend to a queryable trace of every fetch + prompt.

**Our assessment:** Agree, incremental. Natural extension of the `sources` field
both tiers already emit (ADR 0006).

**Status (v1 done, 2026-06-03, [spec 004](specs/004-query-provenance.md)):** every
`/api/query` returns a `trace_id` and emits one structured `query.trace` log
(question, intent, tier, sources, status, confidence, tool calls, latency). The
trace_id is propagated to the agent (which logs it) — one correlation spine across
both services (this also closed the correlation-ID gap deferred in P3c). Provenance
shows the **redacted** view (sources + tool calls), not raw data. **Deferred:** a
Postgres-persisted trace table + `GET /admin/traces/{id}` retrieval API (largely
duplicates log search; adds retention burden); agent-side **exact-prompt** capture;
trace retention policy.

## P3c — Self-observability

**Review (high prior):** an observability product with no internal observability
won't survive its own incidents. Instrument the pipeline before adding features.
Also: the pod registry is a single source of truth / likely SPOF — define caching,
failure behavior, and stale/missing-entry handling.

**Our assessment:** Agree. Pair metrics with the structured logging we already emit.

**Status (v1 done, 2026-06-03, [ADR 0011](decisions/0011-self-observability-metrics.md)):**
Prometheus `/metrics` (pull-based, bounded labels, excluded from its own middleware)
with domain metrics — `agentify_queries_total{intent,tier,status}` (the Tier-1/Tier-2
split), query + agent-call latency, `agentify_ingest_total`, and HTTP metrics — plus
free Go-runtime metrics. Validated live. **Agent-side token/cost done (2026-06-03):**
the Python agent exposes its own `/metrics` (`agent_model_tokens_total{model,type}`,
request/iteration counters, indicative `agent_estimated_cost_usd_total`). **Still
open:** request correlation IDs done (spec 004); distributed tracing; CloudWatch
remote-write; dashboards/alerts. **Pod-registry resilience — done (2026-06-03,
[ADR 0012](decisions/0012-pod-registry-cache.md)):** a read-through snapshot cache
over the DynamoDB registry — eliminates per-query Scans, serves stale on a registry
blip (instead of 500s), invalidates on pod formation, and exposes
`agentify_registry_cache_total{result}`.

## P4 — Capability expansion (after foundations)

- **P4a Root-cause correlation — ✅ v1 done (2026-06-04):** a `diagnose` intent
  ([spec 005](specs/005-root-cause-correlation.md)) recognized before health/cert
  fans out to all of a service's k8fy signals; the Tier-2 agent synthesizes one
  causal narrative (active incident → latent risk → likely cause → prioritized
  actions) with structured `findings`/`likely_cause`/`severity`. **Bound:** v1
  correlates current-state **health + certs** only — temporal root cause ("crashed
  because of the 3pm deploy") needs the event/metric pipeline (P4b) and is deferred.
  The agent honors this: when no events are available it says so rather than
  inventing a cause (live-validated).
- **P4b Temporal spine — ✅ v1 done (2026-06-05):** the prerequisite that makes
  diagnosis *causal*. Restart counts are now emitted as **append-only samples**
  (`k8fy.metrics`) instead of latest-wins, persisted in the Postgres events table
  ([ADR 0013](decisions/0013-temporal-data-in-postgres-events-table.md)); the events
  store gained windowed/entity/order/limit queries; the agent has a
  `get_metrics_history` tool and uses the restart **trend** (when it began) in
  diagnosis ([spec 006](specs/006-temporal-ingestion-and-history.md)).
  **Bounds (deferred):** restarts only (no CPU/mem — needs metrics-server); no
  lifecycle-event capture yet (watch-event noise); no deploy/change correlation; no
  retention job.
- **P4b-ML Classical ML (still Proposed):** time-series anomaly detection, log
  template extraction (Drain-style) to collapse log volume before it hits a model,
  embeddings for semantic event search (pairs with pgvector), trivial forecasting
  for cert/capacity. **Principle: deterministic tool for the deterministic job, LLM
  for synthesis only** — we already compute `days_until_expiry` deterministically;
  the LLM must never do that arithmetic. Now unblocked by the temporal spine, but
  not justified until there's sample volume.
- **P4c Investigation-on-anomaly loop:** anomaly fires → agent gathers context →
  posts a summary to Slack/PagerDuty. **Human-in-the-loop; no auto-remediation** —
  consistent with [ADR 0003](decisions/0003-read-only-to-actions-boundary.md).

## P5 — Pattern A skills standardisation ✅ Done (2026-06-11)

All five skill classes (`HealthSkill`, `CertAuditSkill`, `ChangeHistorySkill`,
`RestartTrendSkill`, `DiagnoseSkill`) now use Pattern A: deterministic parallel
pre-fetch of all predictable signals + exactly one Claude call per request. No tools
are declared to Claude; data is injected directly into the user message. The
advisor/executor strategy (`advisor_20260301` beta) in `DiagnoseSkill` is removed.
See [ADR 0026](decisions/0026-pattern-a-skills-standardisation.md) and
[spec 010](specs/010-skill-router.md) for the full implementation record.

## P5+ — Supporting tooling (when scaling)

### Tools vs Skills — context (historical)

**Tools** are atomic, stateless functions the LLM calls during reasoning to fetch
data. We already have seven: `get_service_health`, `get_pod_logs`,
`get_metrics_history`, `get_change_history`, `get_pod_events`, `get_certificates`,
`query_pod` (all in `src/agent/k8fy/tools.py`).

**Skills** are higher-level, pre-packaged diagnostic workflows that combine multiple
tools, have their own specialised system prompt, and return a structured result.
A skill knows *how* to solve a class of problem — not just how to fetch one piece of
data. The current general-purpose K8fy agent is a single "know-everything" context;
skills split it into expert specialists.

#### Pattern A — Hardcoded tool sequences (deterministic skill, lower cost)

Pre-assemble data with a fixed tool sequence, then make exactly ONE Claude call
with everything pre-loaded. Bypasses the ad-hoc tool loop entirely:

```python
# src/agent/k8fy/skills/diagnose_crash.py
async def diagnose_crash_loop(pod_id, namespace) -> DiagnosisResult:
    logs    = await process_tool_call("get_pod_logs",        {"pod_id": pod_id, "previous": True})
    events  = await process_tool_call("get_pod_events",      {"pod_id": pod_id})
    metrics = await process_tool_call("get_metrics_history", {"pod_id": pod_id})
    # ONE Claude call with all data pre-assembled → predictable cost
    return await reason_over(logs, events, metrics, CRASH_DIAGNOSIS_PROMPT)
```

Cost: exactly 1 Opus call + 3 tool fetches regardless of how complex the crash is.
The current ad-hoc loop takes 2–7 tool iterations for the same problem.
**Recommended first step — immediately implementable, 30–50% token cost reduction.**

#### Pattern B — Sub-agent with specialised prompt (agentic skill, higher quality)

A separate Claude instance with domain expertise baked into its system prompt.
A **skill router** (using the Tier-1 findings that already exist) dispatches to
the right specialist:

```
User: "why is payment-worker crashing?"
         ↓
   Tier-1 evaluator → finds CrashLoopBackOff
         ↓
   SkillRouter (new — reads Tier-1 findings):
     crash detected     → CrashLoopSkill    (prompt: K8s failure-mode expert)
     cert expiry        → CertAuditSkill    (prompt: PKI/TLS lifecycle expert)
     rollout regression → DeploymentSkill   (prompt: rollout strategy expert)
```

Each skill has a focused system prompt that makes Claude sharper for its domain,
reducing hallucination and irrelevant context. The router lives naturally at the
same point where Tier-1 currently hands off to Tier-2 (`handlers.go`
`tryDeterministic` → agent call).

**Why Pattern B fits agentify specifically:**
- The Tier-1 evaluator already classifies the problem type *before* Claude is called.
  That classification is exactly the input a skill router needs.
- Different failure modes need different expertise: crash-loop root cause ≠ PKI
  lifecycle ≠ deployment rollout analysis.
- The skill boundary enforces a tool-call budget per skill class (a crash skill
  always calls logs + events + metrics; never more).
- Aligns with the existing intent taxonomy (`health_check`, `cert_check`,
  `diagnose`, `change_history`) — one skill per intent class.

**Implementation order:** Pattern A first (lower risk, immediate savings), then
Pattern B (higher quality, requires skill router + per-domain prompts).

### Langfuse prompt management ✅ Done (2026-06-11)

All six K8fy skill prompts are now managed via Langfuse under the label
`"production"` (names: `k8fy/system`, `k8fy/health-check`, `k8fy/cert-audit`,
`k8fy/change-history`, `k8fy/restart-trend`, `k8fy/diagnose`). The agent fetches
live prompts at startup via `k8fy/prompt_manager.py` with a local fallback so
the service starts cleanly without credentials. Prompts can now be edited in the
Langfuse UI and picked up without a code deploy (60 s cache TTL).

Setup: set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` in
environment or `.env`, then run `python scripts/migrate_prompts_to_langfuse.py`
once to push local strings into Langfuse.

### Other P5 items

- **AI gateway** — semantic caching (cache hits ~5ms vs ~2s full round-trip),
  fallback model routing (Haiku for simple intents, Opus only for synthesis),
  per-namespace cost budgets.
- **Eval harness** — regression test suite for Tier-2 answer quality: fixed
  query/ground-truth pairs, run on CI after prompt or model changes. Prevents
  silent quality regressions. Tools: Langfuse or MLflow for tracing + eval.
  Prerequisite: skills (Pattern A/B) must be stable before evals are meaningful.
- **Agent tracing** — per-call tool-iteration counts, latency, and token cost
  surfaced in the UI alongside the answer (partial: Prometheus metrics exist for
  token counts; structured per-call trace still deferred).
- **Tool-call budgets** — hard cap on tool iterations per skill class. Pattern A
  naturally enforces this; Pattern B needs an explicit counter in the agent loop.

All explicitly later — they matter once the two-tier path, governance gate, and
skill layer land.

## P6 — HashiCorp Vault Integration (cert management + autonomous rotation)

**Context (2026-06-17):** Development/test scaffold implemented to mimic the client
setup where TLS certificates are issued and rotated by HashiCorp Vault PKI rather
than cert-manager or manual Kubernetes secrets. This demonstrates how agentify can
act as an autonomous cert management agent for Vault-backed workloads.

### What was implemented

| Component | Location | Purpose |
|-----------|----------|---------|
| Vault Helm values | `infra/kubernetes/vault/vault-values.yaml` | Standalone Vault on EKS (dev/test) |
| Vault setup script | `scripts/vault-setup.sh` | Init, unseal, K8s auth, PKI engine, policies, roles |
| Payment service manifest | `infra/kubernetes/payments-test/payment-service.yaml` | nginx HTTPS with Vault Agent Injector annotations — cert injected from Vault PKI at runtime |
| Cert rotator CronJob | `infra/kubernetes/vault/cert-rotator-cronjob.yaml` | Daily check: renews cert via Vault PKI if < 30 days remaining |
| VaultCertSkill | `src/agent/k8fy/skills/vault_cert.py` | Pattern A skill: pre-fetches Vault cert status + K8s cert status, one Claude call to assess and optionally call `rotate_vault_cert` |
| Vault tools | `src/agent/k8fy/tools.py` | `get_vault_cert_status` + `rotate_vault_cert` — call Vault HTTP API directly from the agent |
| `vault_cert` intent | `inferIntent()` in `handlers.go` | Queries containing "vault", "pki", "rotate cert", etc. route to VaultCertSkill |

### Architecture

```
HashiCorp Vault (helm, standalone)
├── PKI engine  → issues TLS certs for payment.payments.svc.cluster.local
├── KV v2       → stores renewed certs for audit
└── K8s auth    → payment-service + cert-rotator ServiceAccounts

payment-service pod
└── Vault Agent Injector sidecar
    ├── On start:  fetches cert from PKI → writes /vault/secrets/tls.crt
    └── On renew:  detects lease expiry → fetches new cert → SIGHUP nginx

cert-rotator CronJob (daily)
└── Checks cert expiry via Vault KV
    └── If < 30 days: vault write pki/issue/payment-service

agentify Investigate / K8s Observability
└── "Is the Vault cert for payment-service healthy?"
    → vault_cert intent → VaultCertSkill
    → get_vault_cert_status tool (queries Vault HTTP API)
    → Claude: assess + optionally call rotate_vault_cert
    → "Cert expires in 12 days — rotating now..."
    → rotate_vault_cert tool → new cert issued + stored in KV
```

### Open items (deferred)

- **Production Vault HA** — Raft storage, multi-node, TLS between Vault nodes
- **Vault Agent template updates** — auto-restart pods (not just SIGHUP) on cert change
- **Terraform Vault provider** — manage PKI roles and policies as code
- **Vault Enterprise** — namespace isolation per tenant (aligns with P3a)
- **Dynamic secrets** — extend VaultCertSkill to manage DB credentials, not just TLS
- **Audit log integration** — stream Vault audit logs into agentify event store for anomaly detection

---

## Post-Vault Gap Analysis (2026-06-17)

> Items P7–P12 were identified through a structured technical review against
> senior LLM-engineer evaluation criteria. They are ordered by impact on demonstrable
> architectural depth, not by implementation complexity. P7 (eval harness) must ship
> first — it gates credibility of everything else.

---

## P7 — Eval Harness as CI Gate ⚡ Immediate priority

**Hard truth:** The eval harness has been listed as P5+ since day one and is the
most damaging gap in the portfolio. The feedback explicitly praised evaluation pipeline
work. Agentify has no eval code — only a roadmap line. This must be fixed first.

**Prerequisite met:** All five skills are on Pattern A (ADR 0017). The infrastructure
is in place (Langfuse wired, `trace_id` returned, `query.trace` logged). The missing
piece is the test dataset and CI step.

**Acceptance criteria:**
- Langfuse dataset `k8fy-regression` with ≥ 10 (query, ground-truth) pairs covering
  all intent classes
- `scripts/run_evals.py` — POSTs each query to `/api/query`, scores against ground
  truth (intent, tier, status, required fields, latency), records score against
  `trace_id` in Langfuse
- CI step in `02-deploy.yml` that runs the eval post-rollout and blocks on score < 0.85
- Scores visible in Langfuse UI alongside production traces

**Architecture:** See [ADR 0019](decisions/0019-eval-harness-as-ci-gate.md).

---

## P8 — RAG + Semantic Memory (pgvector) ✅ Done (2026-08-18)

Shipped: `incident_embeddings` (pgvector `vector(512)`, ivfflat cosine index),
the agent's `/embed` route (Voyage AI, `available=false` fallback when no
key), `get_similar_incidents` wired into `DiagnoseSkill._prefetch()`. The
2026-08-18 date marks when this was verified end-to-end and given direct
test coverage (`TestIncidentEmbeddings` in `postgres_test.go`;
`test_embed_endpoint.py`, `test_similar_incidents.py`, and the
`similar_incidents` case in `test_diagnose_live_fallback.py` on the agent
side) — the feature itself shipped earlier, in the commit implementing
this section (`git log -S embedAndStoreIncident`).

**Hard truth:** RAG is explicitly listed as a required production LLM pattern in
evaluation criteria. Agentify deferred pgvector as YAGNI (ADR 0010). That decision
was correct at the time; it is no longer correct.

**What it does:** Embeds diagnostic outputs at trace-persist time. When a `diagnose`
query fires, the pre-fetch sequence retrieves the top-3 semantically similar past
incidents and injects their summaries into the Claude call context. The system learns
from its own history — a second incident with the same root cause gets a higher-
confidence diagnosis faster.

**Architecture:**
```
Trace persisted (Tier-2 answer stored)
  → async embed(headline + findings + likely_cause) via Haiku
  → INSERT INTO incident_embeddings (trace_id, embedding, summary, ...)

DiagnoseSkill._prefetch() [Pattern A, new signal]
  get_similar_incidents(service, namespace, description)
    → SELECT ... ORDER BY embedding <-> $query_vec LIMIT 3

Claude sees: "Similar past incidents: [date, likely_cause, what resolved it]"
```

**Implementation steps:**
1. Enable `pgvector` extension on RDS (`CREATE EXTENSION IF NOT EXISTS vector`)
2. Add `incident_embeddings` table (migration in `initSchema`)
3. Async embed goroutine in `logTrace` — calls a new `/embed` endpoint on the agent
4. New `get_similar_incidents` tool in `tools.py`
5. Add tool to `DiagnoseSkill._prefetch()` pre-fetch sequence
6. Add `DIAGNOSE_REASONING_SCHEMA` field for `similar_incidents` context

**See also:** [ADR 0018](decisions/0018-three-layer-memory-architecture.md) — formal
definition of the three-layer memory model this completes.

---

## P8b — Memory Architecture: Reframe and Document

**The reframe:** agentify already has two of the three memory layers. The third
(semantic) is what P8 adds. Once P8 ships, the full architecture is:

| Layer | What it is | Where in agentify |
|-------|-----------|-------------------|
| **Working memory** | In-request context; K8s signals pre-fetched by Pattern A | Pattern A skill pre-fetch; multi-turn session cache (P12) |
| **Episodic memory** | Time-ordered append-only event history | `events` table: `k8fy.metrics`, `k8fy.events`, `k8fy.certs` |
| **Semantic memory** | Vector retrieval over past incident knowledge | `incident_embeddings` + pgvector (P8) |

**Action:** After P8 ships, update the architecture documentation and any public-
facing descriptions to lead with this three-layer framing. It is a defensible and
demonstrable architectural pattern — not just a feature list.

---

## P9 — PR Review Agent (second domain use case)

**Hard truth:** The JD lists PR review as the primary use case. Agentify is K8s
observability only. Without at least one demonstrable artifact outside K8s, the
portfolio reads as narrow.

**Architectural conviction:** The same two-tier pattern generalises. PR review is
not a different architecture — it is the same architecture on a different domain:

```
Tier-1 (deterministic):
  - File count delta
  - Test coverage delta (from CI metadata)
  - Dependency changes (package-lock.json / go.sum diff)
  - Binary/generated file changes
  → Returns structured flags: [{severity, file, reason}]

Tier-2 (LLM, only if Tier-1 finds issues or query requests deep review):
  - CodeReviewSkill (Pattern A)
  - Pre-fetch: diff, related test files, historical PR patterns
  - One Claude call → structured findings [{file, line, severity, explanation, suggestion}]
```

**Why it matters beyond the interview:** This proves the architectural thesis of agentify
is generalisable infrastructure, not a single-purpose K8s tool. It is the foundation
for positioning agentify as a "developer intelligence platform" vs a K8s monitoring tool.

**MVP scope:**
- GitHub webhook receiver (or on-demand `POST /api/review { repo, pr_number }`)
- `inferIntent` extended to recognise `pr_review` intent
- `PRReviewSkill` following Pattern A (fetch diff + test delta → one Claude call)
- `PRReviewCard` component in the frontend (same structure as `DiagnosisCard`)

---

## P10 — Context Management at Scale

**Hard truth:** Pattern A is a cost optimisation (deterministic pre-fetch → one call).
It is not a context management strategy. At scale, the signals it injects (full logs,
full event history, full restart metrics) can easily fill a 50k-token context. The
system has no budget-aware truncation, no hierarchical summarisation, no selective
retrieval. This was identified as a specific gap in the technical review.

**What needs to be added:**

1. **Context budget per skill** — each Pattern A skill has a `MAX_CONTEXT_TOKENS`
   constant. If pre-fetched data exceeds the budget, truncate deterministically
   (most-recent events first; truncate logs to last N lines; drop metrics beyond
   a configurable window). Log the truncation so it is visible in traces.

2. **Hierarchical summarisation trigger** — for multi-turn chat sessions (P12):
   after 20 turns, automatically summarise the early history into a compact block
   that replaces it. This is the same pattern as `compact_20260112` in the Anthropic
   API — implement at the application level as a fallback.

3. **Budget-aware tool selection** — in the `_reason_single` agentic path (general
   queries), track remaining context budget after each tool call. Stop calling tools
   when `budget_remaining < MIN_SYNTHESIS_TOKENS`. This converts Pattern A's static
   pre-fetch into a dynamic version.

**Framing for technical interviews:** "Pattern A enforces a deterministic context
budget — we pre-fetch exactly the signals we need and nothing more. The agentic path
adds budget tracking to prevent runaway context growth. Summarisation is triggered at
session boundaries, not per-call."

---

## P11 — Multi-Provider Routing: Bedrock Stub

**Hard truth:** ADR 0008 deferred Bedrock/Vertex until a client requires it. That is
the correct production decision. For demonstrability, the implementation is missing.
Evaluators ask "have you actually implemented provider switching?" — "yes, the
architecture supports it" is a weaker answer than "yes, here's the Bedrock client."

**Minimum viable implementation:**
- Add `AnthropicBedrock` client path in `config/claude_client.py`
  (uses `anthropic.AnthropicBedrock(region_name=...)`)
- Wire via `CLAUDE_PROVIDER=bedrock` env var
- Test that one skill (HealthSkill) works end-to-end on Bedrock
- Document the model ID change: `claude-opus-4-8` → `anthropic.claude-opus-4-8`

This is a single file change + one integration test. The architecture is already
designed (ADR 0008). Execution is all that is missing.

**See also:** [ADR 0008](decisions/0008-multi-provider-model-routing.md) — full
provider routing design. The stub activates the `BEDROCK` branch of that ADR.

---

## P12 — Multi-Turn Conversational Chat (Dedicated Chat Page)

**Decision:** Implement as a **dedicated admin nav item** ("Chat"), not integrated
into the existing K8s Observability ServiceEvaluator flow. Rationale: clean separation
of interaction paradigms; supports open-ended questions not tied to a specific service;
simpler to build and test independently.

**Architecture decisions confirmed (2026-06-17):**

| Concern | Decision | Rationale |
|---------|----------|-----------|
| Transport | HTTP POST (send turn) + SSE (receive stream) | Simpler than WebSocket; works through ALB; browser auto-reconnects SSE |
| Session state | Postgres `chat_sessions` + Go `sync.Map` write-through cache | Survives pod restarts; any pod can serve a session |
| K8s context | Cache K8s signals in session (5-min TTL), explicit refresh on demand | Avoids re-fetching on every turn; "show me the latest data" triggers refresh |
| Tier routing | Multi-turn always Tier-2; Tier-1 data seeded as opening context | Tier-1 is single-shot by design; conversation is inherently Tier-2 |
| Context window | Full history + prompt caching; summarise at 20 turns | Cache makes marginal per-turn cost small; summarisation prevents runaway cost |
| Frontend | New `ChatPanel` component + `ChatNavItem` in admin sidebar | Dedicated page with message thread, streaming tokens, typing indicator |

**New endpoints:**
```
POST /api/chat/sessions          → create session { session_id }
POST /api/chat/{id}/messages     → send user turn { message_id }
GET  /api/chat/{id}/stream       → SSE: token stream for current turn
GET  /api/chat/{id}/history      → full conversation history
DELETE /api/chat/{id}            → close session
```

**New Postgres table:**
```sql
CREATE TABLE chat_sessions (
    id                 TEXT PRIMARY KEY,
    namespace          TEXT NOT NULL DEFAULT '',
    service            TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',  -- summarised old history
    messages           JSONB NOT NULL DEFAULT '[]',
    context_cache      JSONB NOT NULL DEFAULT '{}',
    context_fetched_at TIMESTAMP,
    created_at         TIMESTAMP DEFAULT NOW(),
    last_active        TIMESTAMP DEFAULT NOW(),
    expires_at         TIMESTAMP
);
```

**Implementation stages (do not skip stages):**

| Stage | What | Validation |
|-------|------|-----------|
| 1 | Session CRUD (Postgres table + Go endpoints) | `POST /sessions` returns 200, `GET /history` returns empty array |
| 2 | Non-streaming multi-turn (full response, no SSE) | Conversation works end-to-end; history grows correctly |
| 3 | Streaming (SSE from Python agent → Go → frontend) | Tokens appear progressively in ChatPanel |
| 4 | K8s context cache + Tier-1 seed on session start | Session opens with pod health pre-loaded |
| 5 | Summarisation at 20 turns | Long sessions compress without losing context |

**See also:** Architecture discussion in project conversation history (2026-06-17).

---

## P13 — Agentic use cases: Incident Responder + Deployment Guardian (2026-07-20)

**Status: done, with a deliberate governance change from spec 011's original design.**
Use Case 1 (Incident Responder) and Use Case 2 (Deployment Guardian) are built,
gated by a mandatory human-approval step for every write action — see
[ADR 0020](decisions/0020-phase-3-remediation-with-approval-gate.md), which
amends [ADR 0003](decisions/0003-read-only-to-actions-boundary.md) to authorize
Phase-3 actions (restart/scale/rollback) with that gate. Spec 011's original
"confidence > 0.9 → auto-rollback" idea for Use Case 2 was explicitly rejected —
every proposal requires an explicit approve/reject, regardless of confidence.

**What shipped:**
- `remediation_proposals` Postgres table + propose→approve/reject→execute
  lifecycle (idempotent decisions, TTL-bounded proposals).
- `IncidentResponderSkill` (wired into the existing spec 009 investigation
  loop) and `DeploymentGuardianSkill` (new in-process poller over `k8fy.events`
  deploy rows) — both Pattern A, both propose-only, never execute.
- `action_executor.py` — the only code path that writes to a K8s Deployment
  (restart/scale/rollback-via-change-history), reachable exclusively through a
  deterministic `execute_remediation` dispatch after approval; never exposed
  as a Claude-callable tool.
- Admin Console **Remediation** panel (Approve/Reject) backed by the same POST
  API (`/admin/remediation/{id}/approve|reject`, bearer-token guarded) so an
  external approver (Slack, PagerDuty) can call it later without a redesign.
- RBAC: a new namespace-scoped `agent-remediator` Role (mirrors the existing
  `agent-cert-renewer` pattern), not a namespace-wide grant.

**Deliberately deferred:** full ReplicaSet-revision rollback (MVP replays the
previous deploy event's recorded images instead); Use Cases 3 (Capacity
Intelligence), 4 (Knowledge Builder), 5 (PR Review) — untouched by this pass.

---

## P14 — Split out two standalone agents (agreed 2026-07-20)

**Context:** everything in `src/agent` today is one FastAPI process with nine
`SkillRouter` skills plus the chat agent — a router pattern (deterministic
Go-side `inferIntent()` dispatch, not agent-to-agent delegation; see the
"multi-agent vs one-agent-multi-skill" discussion, 2026-07-20). Reviewed
whether any current or planned skill has a strong enough reason to be pulled
out into its own deployable agent instead. Most don't — they share the same
data model, the same read-only boundary, and the same redaction/tracing/cost
plumbing, so splitting them would just reintroduce the polyglot-microservice
complexity [ADR 0010](decisions/0010-postgres-single-store.md) already paid
down once. Two do:

### P14a — Remediation executor as its own network-isolated agent

**Why:** `RemediationExecutorSkill` / `action_executor.py` (P13 / [ADR
0020](decisions/0020-phase-3-remediation-with-approval-gate.md)) is the only
write-capable code in the whole system. Today its isolation from the LLM
reasoning loop is enforced **by convention** — it's simply never registered
as a Claude-callable tool. That's a code-review guarantee, not an
infrastructure one. Splitting it into its own minimal service converts that
into a network-level guarantee: the general reasoning agent (which processes
untrusted-ish inputs — log lines, adapter data) would have **no network path**
to the K8s write RBAC at all, even if a future, more agentic skill widened the
model's tool surface. Least-privilege separation between "reasons over data"
and "holds write credentials" is a standard security boundary, not a
theoretical one.

**Shape:** a small stateless service exposing one dispatch endpoint
(`restart_deployment` / `scale_deployment` / `rollback_deployment`), called
only by the backend's `/admin/remediation/{id}/approve` path after a human
has approved a proposal — same trigger, same RBAC (`agent-remediator` Role),
just moved out of the general agent pod. No LLM in this service at all.

### P14b — PR review agent (Use Case 5 / P9) as its own agent from the start

**Why:** spec 011 already frames this as "the second-domain use case that
proves the two-tier pattern generalises" — it shares zero data model with K8s
pods/certs/events, needs entirely different tools (PR diff, GitHub API) and
credentials (GitHub tokens, not K8s/Vault), and is triggered by a different
event source (GitHub webhook) with no dependency on the K8fy pipeline
running. Folding it into `SkillRouter` as a tenth entry would be the wrong
call for a domain this disjoint — build it as its own deployable agent (or at
minimum a fully standalone module) from day one, reusing the two-tier
pattern (deterministic Tier-1 lint checks + Pattern-A Tier-2 skill) but not
the K8fy process.

**Sequencing:** independent of each other; P14a is the higher-priority of the
two (closes an actual security gap in shipped code), P14b can land whenever
P9 is picked up.

---

## P15 — Pull-based log-platform connector (proposed 2026-07-21)

**Context:** today's `get_logs`/`live_get_pod_logs` fetches a bounded tail
directly from the K8s API (as of [ADR 0027](decisions/0027-merge-k8fy-adapter-into-discovery.md),
relayed through agentify-discovery's persistent connection — the standalone
adapter and its `get_pod_logs` tool this section originally described are
retired), redacts it (denylist scrubber — freeform text can't use
the allowlist gate structured fields get), and discards it — never persisted
(spec 008, [ADR 0014](decisions/0014-on-demand-ephemeral-log-fetch.md)). The
ask: stop hitting cluster logs directly; integrate with wherever the org's
logs actually land (Splunk, an OpenSearch/Elasticsearch index, potentially fed
by Kinesis Firehose).

**Decision (2026-07-21):** pull, not push. The agent becomes a bounded,
time-windowed *reader* of the existing log platform at diagnosis time — same
fetch-redact-discard discipline as today, just a different backend behind the
same tool shape. This explicitly does **not** revisit ADR 0014 or ADR 0010
(Postgres-only): nothing new gets persisted, so the "logs are ephemeral, no
retention question" premise holds. **Rejected alternative:** continuous
stream consumption (agentify itself consuming a live Kafka/Kinesis topic and
indexing it) — bigger lift (needs a real log store, volume-aware retention,
ingest-time structured redaction instead of the denylist scrubber), and only
justified by a proactive/pattern-mining use case that doesn't exist yet. ADR
0014's own "revisit if" clause anticipates that path; treat it as a distinct
future decision if a concrete use case (e.g. extending the P4c investigation
loop) demands it — don't bundle it into this item.

**Connector lineup (reframed 2026-07-27, see ADR 0021's 2026-07-27
revision):** Athena/Glue (shipped 2026-07-25) is the first live `LogSource`
implementation. Splunk and Elasticsearch/OpenSearch are **additional**
connectors planned next to broaden integration options — for customers whose
own log platform is already Splunk or an ES/OpenSearch index — not a
replacement for Athena. Splunk is its own implementation (SPL via the REST
search-jobs API, Splunk token auth). Elasticsearch and OpenSearch share
close enough to the same `_search` Query DSL that one connector covers both —
the design below (query construction, schema) was written against that
shared API and still applies once it's built as the next connector.

**Shape:** a pluggable log-backend abstraction — direct K8s fetch (existing)
is one implementation, Athena/Glue (shipped 2026-07-25) is a second, OpenSearch/
Splunk are next. All sit behind the same `get_pod_logs`/`get_logs`-equivalent
tool contract (bounded tail/window, entity filter, redact-before-the-agent-
sees-it). No new Postgres tables, no new retention janitor.

**Design refined 2026-07-21 (brainstorm), scope confirmed as read-connector
only — the ingest pipeline (Fluent Bit/Firehose + OpenSearch domain + index
templates) is explicitly out of scope for this item; assumed to exist or be
stood up separately:**

> **Substrate note (2026-08-04):** the `AdapterClient`/`handlePodLogs`/
> `internal/api/adapter_client.go` this design's `K8sAdapterLogSource`
> bullet below was written against no longer exist —
> [ADR 0027](decisions/0027-merge-k8fy-adapter-into-discovery.md) retired
> them outright, replaced by `live_get_pod_logs` relayed through
> agentify-discovery's persistent connection. Not rewritten in place since
> this whole section is still an unbuilt proposal; whoever picks this up
> should design `K8sAdapterLogSource` (or whatever it's renamed to) as a
> thin wrapper over `live_get_pod_logs`/`get_logs`'s router
> (`log_router.py`) instead, not over the deleted `AdapterClient`.

- **Clarified Firehose's role:** it's a delivery mechanism (producer →
  buffer/batch → OpenSearch), not something queried at read time. Two
  standard ingest topologies exist upstream of this item — Fluent Bit → ES
  output plugin directly, or Fluent Bit/app → CloudWatch Logs → subscription
  filter → Firehose → OpenSearch (only worth the extra hop for fan-out to a
  second destination like S3). Neither is this item's concern; only the
  resulting OpenSearch index/schema is.
- **Interface:** a `LogSource` Go interface (`FetchLogs(ctx, LogQuery) (LogResult, error)`)
  behind the existing `handlePodLogs` call site. `K8sAdapterLogSource` wraps
  today's `AdapterClient.FetchLogs` unchanged; `OpenSearchLogSource` is the
  new implementation. Selected by config (`LOG_SOURCE=k8s_adapter|opensearch`).
  The agent-side `get_pod_logs` tool contract (`LogRequest`/`LogResponse` shape
  in `internal/api/adapter_client.go`) does not change.
- **Query construction:** OpenSearch Query DSL (`POST /<index>/_search`) — bool
  query on namespace + pod (prefix match for the deployment, same K8s-hash-
  suffix-stripping already used elsewhere) + container; mandatory bounded
  `range` filter on `@timestamp` (never an open-ended query); `sort` desc;
  `size` capped to match today's 100-default/200-cap tail convention.
  `previous=true` (crashed-container-instance logs) has no direct OpenSearch
  equivalent without a restart/instance-id field on documents — fallback is
  bounding the time window tightly around the restart timestamp already known
  from `k8fy.metrics`, which `DiagnoseSkill` already correlates against.
- **Schema (greenfield — proposed, not yet built):** modeled on Fluent Bit's
  standard Kubernetes-filter output (near-zero-transform match if Fluent Bit
  ever becomes the actual shipper) rather than a bespoke convention:
  ```
  @timestamp                    — range-filter field
  kubernetes.cluster_name       — P16 forward-compat (unused until multi-cluster)
  kubernetes.namespace_name
  kubernetes.pod_name
  kubernetes.container_name
  kubernetes.labels.app         — optional, deployment-level correlation
  log.level                     — parsed level if the shipper extracts one
  message                       — freetext line; the only field the denylist scrubber touches
  stream                        — stdout/stderr
  ```
  Index naming: daily-rotated (`logs-<yyyy.MM.dd>`, queried via a `logs-*`
  alias) with retention handled by an OpenSearch **Index State Management
  (ISM)** policy (automatic rollover/deletion) — replaces the Postgres
  age-based janitor pattern (ADR 0015 already flags that janitor as a
  stopgap not meant for real log volume; ISM is the native mechanism for
  exactly this).
- **Two-layer redaction — a real improvement over today, not just a lateral
  move:** because OpenSearch documents are structured (unlike a raw K8s log
  tail), the connector can allowlist at the *field* level (only ever return
  `{timestamp, level, message, pod, namespace, container}`, silently dropping
  anything else — arbitrary custom labels, attached metadata) **and** still
  denylist-scrub the freetext `message` field with the existing `RedactText`
  scrubber. Belt and suspenders, not possible with a raw pod-log tail. Worth
  documenting as an ADR update when this lands, not just folded in silently.
- **Auth: IAM via IRSA** (SigV4-signed requests, fine-grained access control
  scoped to search/get only — no write/delete) — same pattern already used
  for Secrets-Manager-backed credentials elsewhere, avoiding a new static
  credential to rotate. Rejected: basic auth (master user/password) — only
  the fallback if the OpenSearch domain isn't using IAM-based access control.
- **Reliability:** request timeout + graceful degradation on a slow/failed
  query (same `process_tool_call` convention already used everywhere in
  `tools.py` — a failed tool call returns "logs unavailable," never crashes
  the diagnose call).

**Test harness built 2026-07-21/22 ([ADR 0021](decisions/0021-log-platform-test-infra.md)):**
Fargate profile(s) → Kinesis Firehose → **S3 (Hive-partitioned) + Athena** —
not OpenSearch. Originally scaffolding to validate the `LogSource` interface
cheaply (Athena has zero idle cost, unlike a continuously-billed
search-engine instance). See ADR 0021 for the full infra design
(Fargate/cluster-onboarding registry, Glue partition projection, IRSA-based
query access reusing the existing backend/agent roles).

**Athena path shipped as the first live connector (2026-07-25, reframed
2026-07-27 — revises ADR 0021):** `src/agent/k8fy/log_router.py`'s
`get_logs()` tries this data source first for every namespace when
configured, falling back to the live cluster on empty/error — no
per-namespace registry or manual toggle, wired into both the chat tool loop
and `DiagnoseSkill`'s prefetch. It currently lives in the Python agent
rather than behind the Go `LogSource` interface this item specifies
(`internal/api/adapter_client.go`, `LOG_SOURCE=k8s_adapter|opensearch`) —
accepted architecture debt, see ADR 0021. **Splunk and Elasticsearch/
OpenSearch remain the next connectors to add** — for onboarding a customer's
own, already-populated log platform — as *additional* integration options
alongside Athena, not a replacement for it. The goal is a flexible,
multi-connector `get_logs()` that supports whichever platform(s) a
deployment actually has.

**Generalized to multiple namespaces/services per cluster (2026-07-27,
revises ADR 0021):** onboarding was originally hardcoded to one namespace
(`payments`) per cluster; the Firehose→S3→Glue destination was always meant
to be shared across services (one bucket, one Glue database/table), only the
namespace-onboarding step wasn't. `var.clusters`' `namespace: string` became
`namespaces: list(string)`; each namespace gets its own `aws_eks_fargate_profile`
selector via a flattened `for_each`, all still feeding the single shared
Athena/Glue resources. The Glue table was also renamed `payments_logs` →
`pod_logs` to stop implying a payments-specific table — rows are already
distinguished per-service via the `kubernetes.namespace_name`/`pod_name`
columns, not by table name. Onboarding an additional service is now "add a
namespace to the cluster's list," not new pipeline infrastructure. **Not yet
applied live** — Fargate profile selectors are immutable in EKS, so applying
this will force-replace the existing `payments` profile.

## P16 — Multi-cluster connector (proposed 2026-07-21)

> **Terminology note (added 2026-08-03):** this section predates the
> Discovery/Hub naming settled in ADR 0022/glossary.md, so it still says
> "collector"/"adapter" in places below (left as originally written, for
> history). Read **collector** as **Discovery** (`agentify-discovery`, runs
> once per cluster) and every endpoint/table named here
> (`cluster_services`, `GET /api/resolve-cluster`, `models.PodID`) as
> **Hub**-side — Discovery only ever pushes to the Hub or answers a
> Hub-relayed live request; it never resolves clusters or scopes pod IDs
> itself.

**Context:** the ask is to keep adding cluster connections (integration +
authn/authz) over time, not just one hardcoded adapter. This is **more built
than it looks**: `Integration` (`internal/models/integration.go`) already has
almost the right shape — `{ID, Name, AdapterURL, Namespaces, Status, Token}`,
one row per cluster/adapter, full CRUD, admin UI panel. What's missing is
wiring: the actual outbound adapter client (`h.adapterClient` in
`handlers.go`) is a single global built once at startup from one adapter
URL/token — it never consults `integrationStore`.

**Decision (2026-07-21):** tracked as its own item, separate from P15 (log
platforms). Making it real requires: (1) routing a query's namespace/service
to the right `Integration` row (namespace→cluster mapping — explicit on the
record or discovered via sync); (2) a per-Integration `AdapterClient`
(keyed/cached), not a startup-time singleton; (3) moving `Integration.Token`
off plaintext Postgres storage — already flagged in the code as a prototype
shortcut — onto a Secrets-Manager reference, fetched via IRSA the same way the
agent already fetches its Anthropic/Langfuse keys, since per-cluster/per-log-
platform credentials (Splunk API tokens, Kafka SASL, Kinesis IAM) are more
sensitive than the single dev-cluster bearer token this shortcut was written
for.

**Revised (2026-08-02):** the "separate, larger decision" flagged below has
now been made — [ADR 0022](decisions/0022-multi-tenant-fleet-hub.md)
supersedes ADR 0009 and adopts genuine multi-tenancy (`tenant_id` + Postgres
RLS). `Integration` now also gains `tenant_id`, nested *above* `Namespaces` —
a tenant (customer/org) can own a fleet of multiple clusters, each still
represented by its own `Integration` row. The namespace→cluster routing work
below is unchanged in shape, just additionally scoped by tenant.

**v1 done (2026-08-03, [ADR 0023](decisions/0023-service-cluster-resolver.md)):**
sub-problem (1) above — namespace/service→cluster routing — is solved via a
new `cluster_services` registry table (populated by `agentify-discovery`'s
existing inventory push, which already fetched service names and previously
discarded them) and `GET /api/resolve-cluster`, wired into `DiagnoseSkill`'s
Pattern-A prefetch (`src/agent/k8fy/skills/diagnose.py`): every fleet
cluster resolved for the service being diagnosed gets a live snapshot
prefetched via the ROADMAP P18 use case #9 relay, all in parallel —
`correlation.md`'s existing fan-out rule, not a new mechanism.

**v2 done (2026-08-03, [ADR 0024](decisions/0024-ingested-data-cluster-scoping.md)):**
sub-problem (2) is now also resolved — not by making `h.adapterClient` a
keyed per-Integration cache as originally framed, but by making pod IDs
themselves cluster-aware (`models.PodID`, `internal/models/shard.go`):
`current_state`/`events` reads and writes are isolated by which pod ID they
target, not a WHERE-clause retrofit (RLS was deliberately rejected for these
two tables — see ADR 0024's "Isolation mechanism" section). `HealthSkill`
and `CertAuditSkill` now also use the resolver (`HealthSkill` fans out to
cluster-scoped `get_service_health` + `live_list_pods`; `CertAuditSkill` to
a brand-new `live_get_certificates` tool — the first Secrets RBAC grant
`agentify-discovery` has ever had, narrow-scoped to `type=kubernetes.io/tls`
client-side, flagged in the ADR as RBAC-unenforced at the Kubernetes level).

**Resolver rollout complete (2026-08-03):** `ChangeHistorySkill`,
`RestartTrendSkill`, and `VaultCertSkill` also now call
`resolve_service_clusters` — every Pattern-A skill uses the resolver.
`ChangeHistorySkill`/`RestartTrendSkill` fan out a cluster-scoped
`get_change_history`/`get_metrics_history` per resolved cluster (no live
equivalent exists for either signal, so ingested-data-only, same as
`get_service_health`). `VaultCertSkill` only scopes its K8s-cert half
(`get_certificates`) — `get_vault_cert_status` is deliberately never given a
`cluster_id`: Vault is addressed by a single `VAULT_ADDR` this agent process
is configured with, and no per-cluster Vault routing concept exists
anywhere (no `live_get_vault_status` tool, no Hub-side Vault cluster_id
support) — flagged in `vault_cert.py` as a real gap if Vault-backed cert
monitoring ever needs to span a fleet, not silently assumed away.

**v3 done (2026-08-03, [ADR 0025](decisions/0025-integration-token-secrets-manager.md)):**
sub-problem (3) is now also resolved. Gated on `INTEGRATION_SECRETS_PREFIX`
(empty = every existing deployment's plaintext behavior, unchanged) — when
set, new/updated `Integration.Token` values are stored in AWS Secrets
Manager (`internal/secrets.Manager`) and only an ARN reference lives in
Postgres (`token_secret_arn`, mutually exclusive with the plaintext `token`
column). A one-time `cmd/migrate-integration-tokens` script moves
pre-existing plaintext tokens over. `Integration.CollectorToken` (the
*inbound* push credential, looked up by equality on every collector
request) is explicitly not touched — a Secrets-Manager reference can't serve
as that lookup key without a separate redesign. **All three P16
sub-problems are now closed** — this token still has no runtime consumer
(the per-Integration outbound `AdapterClient` remains unbuilt, see the
"Original framing" note below), so what's closed here is the storage-hygiene
gap, not a new capability.

**Original framing (2026-07-21), preserved for context — since superseded by
ADR 0022:** this was multi-**cluster**, not multi-**tenant** — ADR 0009
(single-tenant per deployment, no `tenant_id`/RLS) stayed as-is, and serving
multiple *customers* (not just multiple clusters for one operator) was
explicitly called out as a separate, larger decision revisiting ADR 0009 —
"don't let tenant-isolation machinery creep in under cover of this item."

---

## P17 — Multi-cluster access for the live-diagnostics tools (proposed 2026-07-25, superseded 2026-08-02)

**Superseded by [ADR 0022](decisions/0022-multi-tenant-fleet-hub.md).** The
recommendation below (one central agent, `sts:AssumeRole` into a per-cluster
IAM role) was never built ("not started") and is now replaced rather than
implemented — see [P18](#p18--deterministic-per-cluster-fleet-collector--multi-tenant-hub-ingest-proposed-2026-08-02-revised-2026-08-02-replaces-p17) for
the design that replaces it. Kept below, unmodified, for context on why the
centralized-pull shape was rejected.

**Context (2026-07-25):** the chat live-diagnostics console (`live_list_pods`,
`live_get_pod_logs`, `live_get_events`, `live_describe_pod` —
`src/agent/k8fy/live_diagnostics.py`) authenticates to the Kubernetes API via
the pod's own mounted ServiceAccount token (`src/agent/k8fy/k8s_client.py`),
calling `https://kubernetes.default.svc` directly. This is topologically
single-cluster by construction — it only works because the agent pod is
physically running inside the cluster it's querying, via RBAC
(`agent-live-diagnostics` Role, `infra/kubernetes/payments-test/serviceaccounts.yaml`)
scoped to read-only pods/logs/events in that one cluster. There is no
"in-cluster" shortcut once a second cluster is in play.

**Recommendation (2026-07-25, superseded):** don't centralize long-lived
credentials for every cluster in one agent. Given this stack is all-EKS/AWS
already (IRSA everywhere, OIDC trust for GitHub Actions), the fitting
extension is: one base IAM role for the agent, allowed to `sts:AssumeRole`
into a separate, per-cluster IAM role — one per onboarded cluster, added the
same way P15's log-platform work already onboards clusters (a `clusters`
map/registry, "add one entry," not a new Terraform module per cluster). Each
per-cluster IAM role maps via EKS access entries to the same narrowly-scoped
`agent-live-diagnostics`-style RBAC Role already built, in that cluster.
This gets AWS-native short-lived tokens (STS, ~1hr, auto-rotating) instead of
static SA tokens, and a central, auditable place (CloudTrail `AssumeRole`
events) to see and revoke per-cluster access — without inventing a bespoke
token broker.

**Why superseded, not just extended:** this design still requires the central
agent to hold *some* standing credential (a base IAM role able to assume into
every onboarded cluster's role) — a large blast radius if that base role or
the agent process is ever compromised, and it doesn't change once multiple
*tenants'* clusters are in the fleet (a compromised central agent could
`AssumeRole` into ANY tenant's cluster role). P18's deterministic per-cluster
collector needs no such standing credential at all: each cluster only trusts
its own local RBAC and one narrow, tenant-scoped push/callback credential —
compromising one collector exposes only that one cluster, never the fleet.

**Tradeoff (still true under P18):** this stack's per-cluster piece is
EKS-specific. A non-EKS or non-AWS cluster joining the fleet still needs its
own collector variant, not the one described here — this tradeoff doesn't go
away with the redesign, it just moves from "the central agent's IAM role"
to "the collector's own auth mechanism."

---

## P18 — Deterministic per-cluster fleet collector + multi-tenant Hub ingest (proposed 2026-08-02, revised 2026-08-02, replaces P17)

**Context:** [ADR 0022](decisions/0022-multi-tenant-fleet-hub.md) decided
*that* the fleet model inverts to push-based, multi-tenant reporting, and
that Discovery (the per-cluster collector this item builds) must genuinely
work across every K8s distribution, not just EKS. This item is the concrete
*what to build*.

**Shape — every bullet below is labeled by which side runs it:**
- **Discovery (per cluster, deterministic, not agentic, one long-running
  Deployment):** no Claude/LLM calls, one process per onboarded cluster —
  not split into a separate CronJob + API server, so there's one manifest,
  one RBAC ServiceAccount, one thing to upgrade and monitor per cluster. An
  internal ticker drives periodic mining+push; the same process holds the
  outbound persistent connection (below) for on-demand requests. A
  CronJob-only variant (periodic push, no live drill-down) is a valid
  simpler phase-1 if drill-down isn't needed yet.
  - **Log source: the standard K8s pod-logs API by default** (portable
    across every distribution), **not** Athena/Glue — Athena stays an
    *optional* per-cluster enhancement for fleet members that happen to
    have it, never the required path (ADR 0022 Decision #6).
  - Reuses `service_topology.py`'s existing extraction logic
    (`extract_service_mentions`) as-is against whichever log source this
    particular cluster actually has — the mining logic doesn't get
    rebuilt, just re-hosted with a portable default input.
  - Authenticates to its own cluster only via the existing
    `agent-live-diagnostics`-style RBAC Role (already built, read-only
    pods/logs/events) — no new in-cluster permissions needed, and no
    cloud-specific IAM assumption for local reads (ADR 0022 Decision #6's
    portability checklist).
- **Ingest API (Hub, tenant *and cluster* scoped):** extends the
  `POST`/`GET /api/service-dependencies` endpoints already built (P15
  connector phase, service-topology mining) to require and filter by both
  `tenant_id` and `cluster_id` (ADR 0022 Decision #1/#2/#3) — same
  handlers, now behind the shared tenant-resolution middleware instead of
  open namespace-only scoping. This — and everything else in this bullet —
  runs on the Hub; Discovery only ever calls it. Natural next signals to
  add to the same ingest shape: namespace/service/deployment inventory,
  ingress/entry-point mapping, health/cert/metrics summaries,
  RBAC/NetworkPolicy posture — see "Use cases unlocked" below.
- **Connectivity: outbound-only from Discovery, always** (ADR 0022
  Decision #7, corrected from an earlier inbound-callback draft). Discovery
  dials out to the Hub and holds the connection open; the Hub never dials
  into a cluster. Both periodic push and on-demand "fetch X now" requests
  multiplex over that one connection.
- **Credential model:** one bearer credential per (tenant, cluster) pair —
  `Integration.CollectorToken`, a field **separate** from the pre-existing
  `Integration.Token` (corrected from an earlier draft that said this would
  "extend `Integration.Token`'s existing shape": `Token` is *outbound*, the
  Hub calling out to an adapter; a collector credential is *inbound*,
  something calling into the Hub — conflating them would mean a leaked
  outbound token also grants inbound push access). The mechanism is
  cloud-agnostic (works identically on any distribution); how the Hub
  stores/rotates its own copy is a Hub-side detail (ADR 0022 Decision #5),
  not a Discovery-side requirement. Resolved server-side by
  `resolveTenantContext` (`src/backend/internal/api/handlers.go`) — Discovery
  itself carries no separate `cluster_id`/`tenant_id` config (see ADR 0022's
  2026-08-03 amendment).

**Use cases unlocked, roughly in build order:**
1. **Namespace/Service/Deployment inventory — shipped 2026-08-03**
   (`src/adapters/discovery/inventory.py`, `k8s_client.py`'s
   `list_deployments`/`list_statefulsets`/`list_daemonsets`): each scan cycle,
   **Discovery** (`agentify-discovery`) lists every namespace's `Service`s,
   `Deployment`s, `StatefulSet`s, and `DaemonSet`s via the portable core
   `apps/v1` API — entirely in-cluster reads — and pushes the resulting
   **active-namespace list** (any namespace with at least one of those
   objects) to a new tenant/cluster-scoped **Hub** endpoint,
   `POST /api/cluster-inventory`, which overwrites the matching
   `Integration.Namespaces` — auto-discovery instead of the manual checkbox
   entry `IntegrationsPanel.tsx` currently requires, closing the gap flagged
   when that form's namespace field was made editable earlier. Full replace
   per push (reflects live cluster truth — a decommissioned namespace
   disappears on the next cycle, not left stale). **Known gap, not solved
   here:** `IntegrationsPanel.tsx`'s manual namespace editor still does a
   full replace on save via `PUT /admin/integrations/{id}` (a Hub endpoint),
   so an admin saving that form after Discovery's auto-discovery has run
   would clobber the Discovery-pushed list — same class of deferred-UX gap
   as use case #2's onboarding follow-up, flagged as a manual follow-up, not
   blocking.
2. **Service-dependency mining — shipped 2026-08-03 as `agentify-discovery`**
   (`src/adapters/discovery/`): a standalone Deployment reusing
   `extract_service_mentions`'s logic off the portable pod-logs API,
   per-cluster — this mining happens entirely inside Discovery, in-cluster —
   pushing real tenant/cluster-scoped edges to the Hub via the
   `Integration.CollectorToken` credential — not just from inside the
   monolithic agent process anymore. Known-services are read directly from
   this cluster's own `Service` objects (a Discovery-side, in-cluster read),
   not the Hub's (untenanted) `GET /admin/tracked` — see the ADR 0022
   amendment. `from_service` is resolved via K8s Service-selector-to-pod-
   label matching (the same mechanism K8s itself uses for Service
   endpoints), not a pod-name heuristic. Deferred from this slice: cluster
   onboarding UX (token minting is via the existing Hub admin API only, no
   UI yet) and the `agentify-discovery-secret`/Secrets Manager wiring for
   the CI deploy pipeline — both flagged as manual follow-ups, not
   blocking.

   **Glue-based extension — shipped 2026-08-18, see
   [ADR 0029](decisions/0029-glue-based-dependency-mining.md):** a second,
   complementary miner (`src/agent/k8fy/dependency_miner.py`) runs
   centrally in the Agent process, reusing the same `extract_service_mentions`
   logic against the P15 Athena/Glue log source instead of live per-cluster
   reads — covers clusters/windows the live scan missed, at the cost of an
   hourly (not ~60s) cadence. Required two supporting changes: the Glue
   pipeline now tags every log row with its source cluster's `Integration.ID`
   (it previously had no cluster-identifying column at all), and
   `POST /api/service-dependencies` now accepts an explicit `cluster_id`
   from a trusted, unauthenticated internal caller (the miner has no
   per-cluster `CollectorToken` of its own to authenticate as).
3. **Ingress/entry-point mapping — shipped 2026-08-04** (Discovery:
   `k8s_client.py`'s `list_ingresses`/`list_gateways`/`list_httproutes`/
   `list_routes` + `discover_api_capabilities`'s new group-probing,
   `ingress.py`, `main.py`'s `_scan_ingress`; Hub:
   `cluster_ingress_endpoints` table, `Upsert`/`ListClusterIngress`,
   `POST`/`GET /api/cluster-ingress`): Discovery now scans `Ingress`
   (`networking.k8s.io/v1`, always) plus Gateway API's `Gateway`+`HTTPRoute`
   and OpenShift `Route` (each only when `discover_api_capabilities` found
   the corresponding API group — a missing CRD is never treated as an
   error) and pushes a flattened (namespace, kind, host, backend_service)
   entry-point map to the Hub, tenant/cluster-scoped and RLS-isolated the
   same way as `cluster_services`. **Store + a minimal read endpoint only**
   in this pass, by design: no new Claude-callable tool consumes
   `GET /api/cluster-ingress` yet — same "collect first, build the
   consumer as a separate step" shape use case #1's inventory push
   followed with P16's resolver. **Known simplification:** Ingress
   host/backend flattening is a cross product of a rule's hosts against its
   backends (not exact per-rule pairing, which `list_ingresses` doesn't
   preserve) — fine for an entry-point *map*, not a precise routing table.
4. **Cross-cluster dependency edges — confirmed 2026-08-04, no code
   change.** The claim that `service_dependencies` carrying `cluster_id`
   is sufficient on its own was verified end-to-end rather than left as an
   architectural assumption: a new Go test
   (`postgres_test.go`, "ROADMAP P18 use case #4") proves
   `ListServiceDependencies` surfaces two of one tenant's clusters' edges
   for the same namespace together, each correctly tagged with its own
   `cluster_id` (it's scoped by tenant+namespace only, never by cluster —
   nothing to change there); a new Python test
   (`test_service_topology.py`,
   `test_fetch_service_dependencies_passes_cross_cluster_edges_through_untouched`)
   proves `fetch_service_dependencies` forwards `cluster_id` verbatim
   rather than dropping it. So the `get_service_dependencies` chat tool and
   `DiagnoseSkill`'s prefetch (`tasks["service_dependencies"] =
   fetch_service_dependencies(...)`, injected directly into the Claude
   prompt) really do start surfacing cross-cluster edges automatically once
   more than one of a tenant's clusters has pushed data for a namespace —
   confirmed, not just claimed. `DiagnoseSkill`'s prompt guidance (already
   written to consider upstream/downstream services) extends naturally to
   "downstream service lives in a different cluster," not just a different
   namespace in the same one.
5. **Fleet-wide health/version snapshots — shipped 2026-08-04** (Discovery:
   `k8s_client.py`'s `list_pod_health`, `health_snapshot.py`, `main.py`'s
   `_scan_health`; Hub: `cluster_health_snapshots` table,
   `Upsert`/`ListClusterHealthSnapshots`,
   `POST`/`GET /api/cluster-health`): each scan cycle, Discovery sums pod
   readiness (the standard per-pod `Ready` condition) across every
   namespace and pairs it with the K8s server version
   `discover_api_capabilities` already fetches, pushing one snapshot that
   overwrites the cluster's single row in place — a plain
   `INSERT ... ON CONFLICT (cluster_id) DO UPDATE`, not the delete-then-
   insert-a-row-set shape `cluster_services`/`cluster_ingress_endpoints`
   use, since a snapshot only ever reflects the *current* state, not
   history. Extends P3c (self-observability) from one pipeline to the whole
   fleet; feeds a future fleet dashboard without the Hub needing live
   per-cluster access for routine polling. **Store + a minimal read
   endpoint only** in this pass, same boundary as use case #3: no agent
   tool or frontend fleet dashboard consumes `GET /api/cluster-health` yet
   (confirmed: no fleet/cluster-overview component exists in
   `src/frontend/src/components/` today). **Capacity (node count/
   allocatable CPU-mem) deliberately not included** — would need a new
   `nodes` RBAC grant Discovery has never had and a wholly new API call —
   flagged as a follow-up, not silently dropped.
6. **Local anomaly pre-filtering for P4c** — the investigation-on-anomaly
   loop's deterministic sweep logic can run *in* the collector (cheap, no
   LLM), pushing only genuine candidates to the Hub, which then runs the
   existing LLM-backed diagnose/webhook flow centrally. Distributes the
   polling load across the fleet instead of one central process polling
   every cluster's metrics on a timer. **Reframed 2026-08-04:** investigating
   how this would actually run "in the collector" surfaced that there were
   two per-cluster collectors with overlapping concerns (the original
   k8fy-adapter and Discovery) — resolved by
   [ADR 0027](../decisions/0027-merge-k8fy-adapter-into-discovery.md)'s full
   merge before this item started. There is now unambiguously one place
   "in the collector" means: Discovery's scan cycle
   (`src/adapters/discovery/main.py`), which already runs the metric/
   certificate sampling this item would build the anomaly sweep alongside.
   Still not started as its own item.
7. **Cross-cluster blast-radius checks for P13** — Deployment Guardian can
   ask "does anything in another of this tenant's clusters depend on this
   service" before approving a risky rollout, using the same cross-cluster
   edges from use case #4 — again, no new tool, just richer data reaching
   an existing one.
8. **Config/RBAC/NetworkPolicy posture** — a lightweight compliance/
   assessment signal (does this cluster have NetworkPolicies at all, what
   does its RBAC surface look like) — a bigger scope increase than the
   others (new signal category), tracked here but not assumed to ship with
   the first version of the collector.
9. **On-demand live drill-down — shipped 2026-08-03** (Hub side:
   `src/backend/internal/api/collector_hub.go`'s `CollectorHub` +
   `HandleCollectorConnect`/`HandleLiveFetch`; Discovery side:
   `src/adapters/discovery/live_relay.py` + `live_tools.py`): Discovery now
   also holds open a **second**, purpose-built persistent WebSocket to the
   Hub (`GET /api/collector/connect`, same `CollectorToken` handshake as the
   push endpoints) — periodic push (use cases #1/#2) stays plain HTTP POST,
   unchanged; see the ADR 0022 amendment for why these weren't unified onto
   one connection. The agent's four `live_*` tools (`src/agent/k8fy/
   tools.py`, running in the agent, which calls the Hub) gained an optional
   `cluster_id` argument: omitted, they behave exactly as before (direct
   in-cluster call — only meaningful for the single-cluster deployment the
   agent itself runs beside); set, the **Hub's** `POST /api/live-fetch`
   relays the request to that cluster's Discovery over its open connection,
   Discovery executes the actual K8s API call in-cluster, and the answer
   flows back through the Hub to the agent — "show me live state in
   cluster B right now" from chat, without a standing central credential
   for the fleet (P17's rejected shape).
   **Known gap, not solved here:** `cluster_id` must be supplied explicitly
   (via `GET /admin/integrations`) — this does not build "which cluster is
   service X in" auto-routing, which is [P16](#p16--multi-cluster-connector-proposed-2026-07-21),
   separately proposed and not started.

**Explicitly out of scope for this item:** ADR 0008/0007's coupled-decision
follow-ups (per-tenant model routing/BYOK, per-tenant redaction policy) —
tracked separately per ADR 0022 Decision #8, not solved here. The
k8fy-adapter/collector consolidation ADR 0022 Decision #9 flagged is
**resolved** — see
[ADR 0027](../decisions/0027-merge-k8fy-adapter-into-discovery.md).

**Not started** — this is a design item, not yet a plan or code.

---

## Frontend — ops console (not a reviewer P-item; foundational gap)

**Status (v1 scaffolded, 2026-06-04):** `src/frontend/` — Vite + React + TypeScript
+ react-query. **Ask** panel (POST `/api/query` → answer, status, confidence,
sources, trace_id) and a **Pods** table (GET `/admin/pods`), wired to the backend
via Vite's dev proxy. **Validated by running (2026-06-04):** Vite serves, the Pods
table polls `/admin/pods` (200), and an Ask query rendered the full `QueryResponse`
including the `trace_id` (provenance visible in the UI). Full `tsc` typecheck
(`npm run typecheck`) recommended as the last check. **Deferred:** shadcn/Tailwind
(CLAUDE.md stack) until iterated visually; admin integrations CRUD (backend handlers
are stubs); WebSocket chat (backend TODO).
