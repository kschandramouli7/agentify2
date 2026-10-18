# Agent Integration Guide

## Overview

The agent integration connects the backend's query orchestrator to Claude AI for intelligent reasoning about operational data. When a user asks a question, the system:

1. Parses the user's natural language question into an intent
2. Routes the query to relevant pods in the context-mesh
3. Fetches raw data from those pods
4. Sends the data to the Python agent service
5. The Skill Router dispatches the intent to the narrowest skill that can handle it
6. Claude reasons about the data using a skill-specific prompt and tool subset
7. Returns a structured, human-readable answer

## Architecture

### Components

**Backend Query Handler** (`src/backend/internal/api/handlers.go`)
- Receives HTTP POST to `/api/query`
- Parses question and context
- Orchestrates the query flow

**Intent Inference** (`inferIntent()`)
- Converts natural language question → query intent
- Current heuristics: "health" → `health_check`, "certificate" → `cert_check`, "diagnose/why/cause" → `diagnose`, etc.
- TODO: Replace with Claude-based NLP in future

**Query Executor** (`src/backend/internal/orchestrator/query_executor.go`)
- Routes queries to pods based on intent (and, since ADR 0024, an optional `clusterID` for fleet-scoped reads — see "Fleet clusters" below)
- Fetches data from selected pods
- Correlates results from multiple pods

**Agent Client** (`src/backend/internal/api/agent_client.go`)
- HTTP client that calls the Python agent service
- Sends question, intent, raw pod data, and context
- Receives structured response from Claude

**Python Agent Service** (`src/agent/`)
- FastAPI service running on `:8001`
- `/reason` endpoint: receives data and intent, routes to Skill Router
- Returns structured `AgentResponse` (answer, status, confidence, sources, details)

**Skill Router** (`src/agent/k8fy/skills/router.py`)
- Dispatches each intent to the narrowest skill sub-agent (spec 010)
- O(1) dispatch table — intent is pre-classified by Go; no classification cost here
- Falls back to the full `K8fyAgent` for unrecognised intents
- Registers 13 dispatch entries / 12 distinct skill classes today
  (`health_check`, `cert_check`, `diagnose`, `change_history`,
  `metrics_history`, `vault_cert`/`renew_cert` [share `VaultCertSkill`],
  `incident_respond`, `execute_remediation`, `deploy_guardian_check`,
  `dependencies` [ROADMAP P18 #2], `cold_services` [ADR 0038],
  `label_scoped_dependencies` [ADR 0043]) — `router.py` is the source of
  truth for the current list.

### Skills (core K8s observability path)

| Skill | Intent | Tool subset (core) | Strategy |
|-------|--------|-------------|----------|
| `HealthSkill` | `health_check` | `get_service_health`, `query_pod`, `get_pod_events` (+ fleet-scoped variants, see below) | **Pattern A** — parallel pre-fetch + 1 Claude call |
| `CertAuditSkill` | `cert_check` | `get_certificates` (+ `live_get_certificates` per fleet cluster) | **Pattern A** — pre-fetch certs + 1 Claude call |
| `DiagnoseSkill` | `diagnose` | 6+ tools (all except `get_certificates`) | **Pattern A** — parallel multi-signal pre-fetch + 1 Claude call |
| `K8fyAgent` (fallback) | `general_query`, `metrics_query`, anything else | All tools | **Pattern B** — agentic tool-calling loop, single model |

**All five original skill classes standardised on Pattern A** (`HealthSkill`,
`CertAuditSkill`, `DiagnoseSkill`, `ChangeHistorySkill`, `RestartTrendSkill`)
— deterministic parallel pre-fetch of every predictable signal, followed by
exactly one Claude call. No agentic tool-calling loop, no advisor/executor
pairing. See [ADR 0026](../context-mesh/decisions/0026-pattern-a-skills-standardisation.md)
(2026-06-11) — this superseded an earlier Opus-advisor/Sonnet-executor
design for `DiagnoseSkill` that no longer exists in the code. Only the
`K8fyAgent` fallback (unrecognised intents) still runs a real agentic loop
(Pattern B).

### Fleet clusters & live drill-down (ROADMAP P16/P18, ADR 0022–0024)

A tenant can own more than one Kubernetes cluster (a "fleet"). Two
different processes split the work — neither is a second copy of the
other, and it matters which one a given operation actually runs on:

- **Discovery (`agentify-discovery`)** — one Deployment **inside each
  fleet cluster**. Deterministic, non-agentic, never calls Claude. Every
  operation it performs is a read against *its own* cluster's K8s API,
  followed by either a push or a relay response to the Hub:
  - **Push (periodic, plain HTTP):** lists namespaces/services/workloads
    (`/api/cluster-inventory`), mines a service-dependency graph from pod
    logs (`/api/service-dependencies`), maps Ingress/Gateway+HTTPRoute/
    OpenShift Route entry points (`/api/cluster-ingress`, ROADMAP P18 #3),
    and reports a pod-readiness/K8s-version snapshot
    (`/api/cluster-health`, ROADMAP P18 #5) — four independent pushes, one
    scan failure never blocks another.
  - **Relay (on-demand, over its one persistent connection):** when the Hub
    forwards a `live_*` request, Discovery dispatches it locally
    (`live_tools.py` — reads pods/logs/events/Secrets directly from its own
    cluster) and sends the result back over the same connection.
  - **Watch + push (continuous, `POST /api/ingest`):** since
    [ADR 0027](../context-mesh/decisions/0027-merge-k8fy-adapter-into-discovery.md)
    merged the original k8fy-adapter's ingestion role into Discovery,
    it also runs long-lived K8s watch streams (`watch.py`) over pods/
    services/Deployments — emitting fine-grained `k8fy.live-state`/
    `k8fy.events` change events as they happen — plus two more scan-cycle
    steps (`_scan_metrics`, `_scan_certificates`) that sample container
    restart counts and TLS certificate expiry (`k8fy.metrics`/
    `k8fy.certificates`). All of it authenticates with the same
    `COLLECTOR_TOKEN` as the pushes above — there is no longer a second,
    separate credential for this data path.
  - Discovery **initiates** the one connection it holds (`GET
    /api/collector/connect`) and never accepts an inbound one — there is no
    standing credential that would let the Hub (or anything else) reach
    into a cluster on its own.

- **Hub** — the one central Go backend. It never talks to a cluster
  directly:
  - Authenticates each push/connection via `resolveTenantContext` and a
    presented `CollectorToken`.
  - Persists what Discovery pushes (`Integration.Namespaces`,
    `cluster_services`, `service_dependencies` — see
    [HUB_DATA_PATH.md](HUB_DATA_PATH.md#storage-backends)).
  - Answers `GET /api/resolve-cluster` (which cluster runs a service) from
    that persisted data — **not** a live call to any cluster.
  - Holds the connection registry (`CollectorHub`) and relays
    `POST /api/live-fetch` requests to whichever Discovery instance is
    registered for the requested `cluster_id`, then returns its answer to
    the agent.

**How a skill uses this:** the agent calls `resolve_service_clusters(namespace,
service, backend_url)` (`src/agent/k8fy/service_topology.py`), which is a
**Hub** call (`GET /api/resolve-cluster`) — Discovery is never contacted
directly by the agent. 0 matches (the common case for a single-cluster
deployment) is a no-op; 1 or more matches means the skill adds one prefetch
task **per resolved cluster**, per [correlation.md](../context-mesh/policies/correlation.md)'s
existing fan-out rule (diagnostic intent fans out across every matching
signal; Tier-2 synthesizes and surfaces disagreement rather than picking a
winner). `DiagnoseSkill` and `HealthSkill` both do this today; `CertAuditSkill`
does the same for `live_get_certificates`.

**Two ways a tool call can be "live" — same agent-side call, different
execution location:**
- `live_list_pods` / `live_get_pod_logs` / `live_get_events` /
  `live_describe_pod` / `live_get_certificates` — the agent calls the Hub
  (`POST /api/live-fetch`) with `cluster_id`; the Hub relays it and
  **Discovery executes the actual K8s read** in that cluster. Omit
  `cluster_id` and the read happens locally instead — this agent pod's own
  in-cluster ServiceAccount, no Hub/Discovery hop at all (unchanged,
  original behavior). `live_get_certificates` has **no** local
  implementation — it always requires `cluster_id`, i.e. it always runs on
  Discovery, never on the agent pod itself.
- `get_service_health` / `get_pod_events` / `get_metrics_history` /
  `get_change_history` / `get_certificates` — the *ingested*-store tools.
  These **never reach Discovery at all**, live or otherwise — passing
  `cluster_id` (ADR 0024) only changes which pod ID **the Hub** reads from
  its own Postgres store (`models.PodID`, e.g.
  `"k8fy.live-state.cluster-42.payments"` instead of
  `"k8fy.live-state.payments"`), so two clusters' identically-named
  namespaces never collide in the same rows. Omitted `cluster_id` behaves
  exactly as before this ADR.

### Data Flow

```
User Question
    ↓
[Query Handler] → Parse question & context
    ↓
[Infer Intent] → Determine query type (health_check, cert_check, diagnose, …)
    ↓
[Route to Pods] → Find relevant pods in registry based on intent
    ↓
[Fetch Data] → Query the Postgres-backed store (ADR 0010) for the selected pod(s)
    ↓
[Agent Client] → Send {question, intent, pod_data, context} to agent service
    ↓
[Skill Router] → Dispatch intent → HealthSkill | CertAuditSkill | DiagnoseSkill | … | K8fyAgent
    ↓
[Claude API] → Reason about data using skill-specific prompt + tool subset
              (Pattern A: 1 call; K8fyAgent fallback: agentic loop, N calls)
    ↓
[Format Response] → Return {answer, status, confidence, sources, details}
    ↓
User Answer
```

## Configuration

**Backend** (via environment variables):
```bash
AGENT_SERVICE_URL=http://localhost:8001   # Where agent service is running
```

**Agent Service** (via `.env` or environment):
```bash
ANTHROPIC_API_KEY=sk-...                  # Claude API key (also accepted as CLAUDE_API_KEY)
CLAUDE_MODEL=claude-opus-4-8              # Default model
CLAUDE_MAX_TOKENS=4096                    # Room for adaptive thinking + structured answer
CLAUDE_EFFORT=high                        # low | medium | high | max
AGENT_MAX_TOOL_ITERATIONS=5              # Cap on the tool-calling loop per request (K8fyAgent fallback only)
```

## Usage Example

### Request

```bash
curl -X POST http://localhost:8080/api/query \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Why is the payment service crashing in production?",
    "context": {
      "namespace": "prod"
    }
  }'
```

### Response

```json
{
  "answer": "payment-svc is in CrashLoopBackOff. Logs show 'OOMKilled' on the previous container instance. Restarts climbed from 0 to 17 between 14:08 and 14:31, roughly 3 minutes after revision 7 rolled out — likely trigger. Recommend increasing the memory limit and rolling back revision 7 to confirm.",
  "status": "unhealthy",
  "confidence": 0.91,
  "sources": [
    "k8fy.live-state",
    "k8fy.events",
    "k8fy.metrics"
  ],
  "details": {
    "findings": [
      "payment-svc: CrashLoopBackOff, 17 restarts",
      "Logs: OOMKilled on previous container",
      "Metrics: restart ramp 14:08–14:31",
      "Change event: revision 7 deployed 14:05"
    ],
    "likely_cause": "Memory limit too low; OOMKill triggered after revision 7 rollout",
    "severity": "critical",
    "recommendations": [
      "Increase memory limit for payment-svc",
      "Roll back revision 7 to confirm trigger",
      "Review memory allocation in new revision"
    ]
  }
}
```

## Intent Types

The system currently recognises these intents:

| Intent | Trigger words (heuristic) | Skill dispatched | Tools available |
|--------|--------------------------|------------------|-----------------|
| `health_check` | "health", "healthy", "status", "up" | `HealthSkill` | 3 core + fleet-scoped variants |
| `cert_check` | "certificate", "cert", "expir", "tls" | `CertAuditSkill` | 1 core + `live_get_certificates` |
| `diagnose` | "diagnose", "why", "cause", "failing", "crash" | `DiagnoseSkill` | 6+ tools |
| `metrics_query` | "metric", "cpu", "memory", "usage" | `K8fyAgent` (fallback) | All tools |
| `general_query` | (default) | `K8fyAgent` (fallback) | All tools |

`vault_cert`/`renew_cert`, `incident_respond`, `execute_remediation`,
`deploy_guardian_check`, `dependencies`, `cold_services`, and
`label_scoped_dependencies` route to their own skills too — see `router.py`
and `inferIntent()` for the current full list.

## Tools

The agent has access to tools that fetch data via `POST /api/agent/fetch`
(ingested store) or, for `live_*` tools, `POST /api/live-fetch` (relayed to a
fleet cluster's collector). `src/agent/k8fy/tools.py` is the source of truth
for the full current list.

| Tool | Description | Key parameters |
|------|-------------|----------------|
| `get_service_health` | Endpoints, ready ratio, pod statuses for a service | `service_name`, `namespace`, `cluster_id` (optional, ADR 0024) |
| `query_pod` | Phase, ready status, restart count for a specific pod | `pod_id`, `namespace` |
| `get_pod_events` | Recent warning/crash events for a pod | `pod_id`, `namespace`, `limit` |
| `get_certificates` | Certificate list, expiry dates, renewal needs (ingested snapshot) | `namespace` (optional) |
| `get_logs` | **Preferred** bounded redacted log tail — tries the Glue/Athena log platform first when configured (ADR 0021), else the live cluster; `previous=true` for the crashed container | `namespace`, `pod`, `previous`, `tail_lines` |
| `get_pod_logs` | Same, but always reads the adapter's cached store specifically — use `get_logs` unless you need this cached snapshot in particular | `pod_id`, `namespace`, `previous`, `tail_lines` |
| `get_metrics_history` | Restart-count time-series over a window | `pod_id`, `namespace`, `since`, `until`, `order` |
| `get_change_history` | Deployment/rollout events over a time window | `deployment`, `namespace`, `since`, `until` |
| `get_service_dependencies` | Mined service-call graph for a namespace (`service_topology.py`) | `namespace` |
| `get_similar_incidents` | Semantic search over past diagnoses (P8, pgvector) | `namespace`, `service`, `description`, `limit` |
| `get_vault_cert_status` / `rotate_vault_cert` | HashiCorp Vault PKI cert lifecycle | `pki_role`, `common_name`, … |
| `live_list_pods` / `live_get_pod_logs` / `live_get_events` / `live_describe_pod` | LIVE K8s API reads — this agent's own cluster, or a fleet cluster via `cluster_id` | `namespace`, `pod`, `cluster_id` (optional) |
| `live_get_certificates` | LIVE TLS cert expiry from a fleet cluster's `kubernetes.io/tls` Secrets — **`cluster_id` required**, no local implementation | `namespace`, `cluster_id` |

Each skill sub-agent is given only the tools relevant to its domain (see Skills table above).

## Fallback Behavior

If the agent service is unavailable:
- Backend returns raw pod data formatted as plain text
- Response has `status: "partial"` and `confidence: 0.5`
- User gets data but without Claude's reasoning

If a fleet cluster's **Discovery** instance is disconnected from **the
Hub**, `live_*` tool calls degrade to a clear error (`"cluster not
connected"`, HTTP 502 from the Hub's `POST /api/live-fetch`) rather than
blocking — the skill's other prefetch tasks still complete and Claude
reasons over whatever landed.

## Testing

### Manual Test

Start services:
```bash
# Terminal 1: Start backend
cd src/backend && go run cmd/agentify/main.go

# Terminal 2: Start agent service
cd src/agent && python -m uvicorn app:app --port 8001

# Terminal 3: Send a health query
curl -X POST http://localhost:8080/api/query \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Is payment service healthy?",
    "context": {"namespace": "prod"}
  }' | jq .

# Terminal 3: Send a diagnose query
curl -X POST http://localhost:8080/api/query \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Why is the payment service crashing?",
    "context": {"namespace": "prod"}
  }' | jq .
```

### Integration Test

```bash
make test-up            # Start test services
make test-integration   # Run tests (includes query testing)
```

## Future Improvements

1. **Dynamic Intent**: Let Claude infer intent from question instead of heuristics
2. **Namespace/service → cluster auto-routing**: today `cluster_id` must
   still be resolved via `resolve_service_clusters` per-tool-call — P16 is
   otherwise fully closed (all three sub-problems done, including
   `Integration.Token` → Secrets Manager, [ADR 0025](../context-mesh/decisions/0025-integration-token-secrets-manager.md))
3. **Remaining P18 fleet-collector use cases** — #6 (local anomaly
   pre-filtering), #7 (cross-cluster blast-radius checks for P13), #8
   (config/RBAC/NetworkPolicy posture). Use cases #1–#5 and #9 are shipped —
   see [ROADMAP](../context-mesh/ROADMAP.md) P18
4. **Scheduled Queries**: Support "alert me if X becomes unhealthy"

## Troubleshooting

**Agent service not responding:**
- Check agent service is running: `curl http://localhost:8001/health`
- Check backend logs for agent client errors
- Verify `ANTHROPIC_API_KEY` is set in agent service environment

**Query returns "Not implemented":**
- Agent service likely errored; check its logs
- Backend will fall back to returning raw data

**A `live_*` tool call returns "cluster not connected":**
- This error comes from **the Hub** (`CollectorHub.RequestLive` finding no
  registered connection for that `cluster_id`) — but the fix is almost
  always on **Discovery**'s side: check that specific cluster's
  `agentify-discovery` pod logs and confirm its `COLLECTOR_TOKEN` matches
  the `Integration` row's `collector_token` (see
  [ADR 0022](../context-mesh/decisions/0022-multi-tenant-fleet-hub.md)) —
  a mismatch means Discovery's connection attempt was rejected before it
  ever registered.
- `live_get_certificates` specifically also needs **Discovery**'s
  ClusterRole to have the `secrets: list, get` grant added in ADR 0024 —
  this is a Discovery-side RBAC issue, not something the Hub can work
  around.

**Two clusters' data looks merged/wrong for the same namespace name:**
- Confirm the caller actually resolved and passed `cluster_id` — omitting it
  routes to the legacy unscoped pod ID, which predates fleet support and is
  shared across every cluster reporting that namespace without a
  `cluster_id` (this is the ADR 0024 "byte-for-byte unchanged" default, not
  a bug, but it does mean an un-scoped query can't distinguish clusters)
