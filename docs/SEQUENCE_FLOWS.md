# Sequence Flows

End-to-end call sequences for every query path through the system.
Diagrams are in [Mermaid](https://mermaid.js.org) — rendered by GitHub, VS Code
(Mermaid Preview extension), and most documentation tools.

**Legend**
- **Solid arrow** (`->>`) — explicit code call / HTTP request
- **Dashed arrow** (`-->>`) — return / response
- **Dashed box** (`rect`) — server-side or parallel boundary
- `[Tier-1]` — deterministic, no LLM
- `[Tier-2]` — agentic, involves Claude
- `[Pattern A]` — parallel pre-fetch + single Claude call
- `[Pattern B]` — Claude-driven agentic tool loop

---

## 1. End-to-end overview — all paths

Shows the Tier-1 fast-path, the Tier-2 skill dispatch, and which strategy each
skill uses. Expand the per-skill diagrams below for internal detail.

```mermaid
sequenceDiagram
    actor Operator
    participant FE   as Frontend
    participant Go   as Go Backend
    participant Py   as Agent Service (Python)

    Operator->>FE: ask question
    FE->>Go: POST /api/query {question, context}

    Go->>Go: inferIntent() → intent label

    Note over Go: [Tier-1] Deterministic fast-path
    Go->>Go: tryDeterministic(intent, data)

    alt Tier-1 answer available (simple health / cert rule)
        Note over Go: 0 LLM calls · <1 ms · confidence = 1.0
        Go-->>FE: AgentResponse
    else Tier-2 required
        Go->>Go: fetchPodData() — parallel pod queries
        Go->>Py: POST /reason {intent, data, context}

        Note over Py: SkillRouter.dispatch(intent)

        alt health_check → HealthSkill
            Note over Py: [Pattern A] parallel pre-fetch → 1 Opus call
        else cert_check → CertAuditSkill
            Note over Py: [Pattern A] 1 cert fetch → 1 Opus call
        else diagnose → DiagnoseSkill
            Note over Py: [Pattern A] parallel multi-signal pre-fetch<br/>(+ fleet-cluster fan-out, ADR 0023/0024) → 1 Opus call
        else general_query / metrics_query → K8fyAgent
            Note over Py: [Pattern B] Opus agentic loop · up to 5 tool iterations
        end

        Py-->>Go: AgentResponse {answer, status, confidence, sources, details}
        Go-->>FE: formatted response
    end

    FE-->>Operator: answer · status badge · sources · trace_id
```

---

## 2. Pattern A — HealthSkill (`health_check`)

Pre-fetches service health and degraded-pod events in parallel, then makes
exactly one Claude call. No agentic tool loop.

```mermaid
sequenceDiagram
    participant Router  as SkillRouter
    participant Skill   as HealthSkill
    participant BE      as Backend API<br/>/api/agent/fetch
    participant Anthropic as Anthropic API<br/>claude-opus-4-8

    Router->>Skill: dispatch("health_check", data, context)

    Note over Skill: [Pattern A] derive what to fetch from data + context

    par Pre-fetch in parallel (asyncio.gather)
        Skill->>BE: get_service_health(service_name, namespace)
        BE-->>Skill: endpoints · ready ratio · pod statuses
    and
        Skill->>BE: get_pod_events(pod_id, namespace)<br/>⚠ only for pods with restarts > 0 or ready=False
        BE-->>Skill: warning / crash events
    end

    Note over Skill: merge pre-fetched data into user message

    Skill->>Anthropic: messages.create<br/>model=opus-4-8 · no tools · merged data
    Note over Anthropic: adaptive thinking · effort=high<br/>structured output (REASONING_SCHEMA)
    Anthropic-->>Skill: {answer, status, confidence, …}

    Skill-->>Router: AgentResponse
```

**Cost profile:** N parallel backend fetches (milliseconds, no billing) + **1 Opus call**.
Tool iterations recorded in Prometheus: **0**.

---

## 3. Pattern A — CertAuditSkill (`cert_check`)

The simplest Pattern A case. The cert list is the only data source and its
parameters are fully known from context — always one fetch, always one call.

```mermaid
sequenceDiagram
    participant Router  as SkillRouter
    participant Skill   as CertAuditSkill
    participant BE      as Backend API<br/>/api/agent/fetch
    participant Anthropic as Anthropic API<br/>claude-opus-4-8

    Router->>Skill: dispatch("cert_check", data, context)

    Skill->>BE: get_certificates(namespace)
    BE-->>Skill: cert list · expiry dates

    Note over Skill: merge certs into user message

    Skill->>Anthropic: messages.create<br/>model=opus-4-8 · no tools · data + certs
    Note over Anthropic: adaptive thinking · effort=high<br/>structured output (REASONING_SCHEMA)
    Anthropic-->>Skill: {answer, status, confidence, …}

    Skill-->>Router: AgentResponse
```

**Cost profile:** 1 backend fetch + **1 Opus call**.
Tool iterations recorded in Prometheus: **0**.

---

## 4. Pattern A — DiagnoseSkill (`diagnose`), including fleet-cluster fan-out

**Superseded 2026-06-11 ([ADR 0026](../context-mesh/decisions/0026-pattern-a-skills-standardisation.md)):**
this used to be an agentic Sonnet-executor/Opus-advisor loop (same shape as
Diagram 5 below). `DiagnoseSkill` is now Pattern A like `HealthSkill`/
`CertAuditSkill`: every predictable signal is pre-fetched in parallel, then
exactly **one** Opus call reasons over all of it. As of ROADMAP P16/ADR 0024
(2026-08-03), the pre-fetch also resolves which of the tenant's fleet
clusters run the service being diagnosed and fans out a cluster-scoped
signal per match — [correlation.md](../context-mesh/policies/correlation.md)'s
existing rule (fan out, let Tier-2 synthesize/surface disagreement), not a
new mechanism.

Three distinct actors, deliberately kept as three separate lanes below (an
earlier version of this diagram conflated the Hub's `/api/agent/fetch` and
`/api/resolve-cluster`/`/api/live-fetch` handlers into two different
participants — they're the same one Go process; every "Hub" arrow below
lands in it):

```mermaid
sequenceDiagram
    participant Router as SkillRouter
    participant Skill  as DiagnoseSkill<br/>(Python agent)
    participant Hub    as Hub<br/>(the one Go backend process)
    participant Disc   as Discovery<br/>(agentify-discovery, INSIDE the fleet cluster)
    participant Anthropic as Anthropic API<br/>claude-opus-4-8

    Router->>Skill: dispatch("diagnose", data, context)

    Note over Skill: [Pattern A] parallel pre-fetch (asyncio.gather)

    par Core signals (unscoped — reads the Hub's own Postgres store)
        Skill->>Hub: POST /api/agent/fetch<br/>get_service_health / get_pod_events /<br/>get_metrics_history / get_change_history /<br/>get_logs / get_service_dependencies
        Hub-->>Skill: ingested-store results (no Discovery involved)
    and Fleet resolution (ADR 0023 — Hub-only, reads cluster_services)
        Skill->>Hub: GET /api/resolve-cluster?namespace=..&service=..
        Hub-->>Skill: {cluster_ids: [...]}  (empty for single-cluster deployments — no-op)
    end

    opt cluster_ids non-empty
        loop once per resolved cluster_id
            par Ingested, cluster-scoped (still Hub-only — Discovery not contacted)
                Skill->>Hub: POST /api/agent/fetch<br/>get_service_health {..., cluster_id}
                Note over Hub: builds a cluster-scoped pod ID (ADR 0024)<br/>k8fy.live-state.{cluster_id}.{namespace}<br/>— reads its own Postgres, nothing more
                Hub-->>Skill: cluster-scoped ingested result
            and Live, relayed to Discovery
                Skill->>Hub: POST /api/live-fetch {cluster_id, tool: live_list_pods}
                Hub->>Disc: relay over Discovery's persistent connection (ADR 0022 Decision #7)
                Note over Disc: executes the actual K8s read,<br/>in its own cluster, right now
                Disc-->>Hub: live K8s snapshot
                Hub-->>Skill: live_list_pods result
            end
        end
    end

    Note over Skill: merge every pre-fetched result (core + per-cluster) into one user message

    Skill->>Anthropic: messages.create<br/>model=opus-4-8 · no tools · merged data
    Note over Anthropic: adaptive thinking · effort=high<br/>structured output (DIAGNOSE_REASONING_SCHEMA)
    Anthropic-->>Skill: {findings, likely_cause, severity, …}

    Skill-->>Router: AgentResponse
```

**Cost profile:** N parallel backend/Hub fetches (milliseconds to low
seconds for a live relay hop, no LLM billing) + **1 Opus call**. Fleet
fan-out only adds cost for tenants with registered clusters — zero extra
calls otherwise. Tool iterations recorded in Prometheus: **0** (Pattern A
never loops).

---

## 5. Pattern B — K8fyAgent fallback (`general_query`, `metrics_query`)

Single-model agentic loop. No advisor tool. Claude decides which tools to call
based on the data and question.

```mermaid
sequenceDiagram
    participant Router  as SkillRouter
    participant Agent   as K8fyAgent (fallback)
    participant BE      as Backend API<br/>/api/agent/fetch
    participant Anthropic as Anthropic API<br/>claude-opus-4-8

    Router->>Agent: dispatch("general_query", data, context)

    Note over Agent: [Pattern B] single-model agentic loop

    loop Agentic tool loop (up to max_iterations = 5)
        Agent->>Anthropic: messages.create<br/>model=opus-4-8 · all registered tools · accumulated messages
        Note over Anthropic: adaptive thinking · effort=high<br/>structured output (REASONING_SCHEMA)

        alt stop_reason == tool_use
            Anthropic-->>Agent: tool_use blocks
            loop For each tool_use block
                Agent->>BE: /api/agent/fetch {tool, args}
                BE-->>Agent: tool result
            end
            Note over Agent: append assistant turn + tool_results
        else stop_reason == end_turn
            Anthropic-->>Agent: final structured JSON
            Note over Agent: loop exits
        end
    end

    Agent-->>Router: AgentResponse
```

**Cost profile:** N Opus calls (one per loop iteration). Tool iterations recorded in Prometheus: **actual count**.

---

## 6. Discovery ↔ Hub persistent connection (ROADMAP P18 use case #9, ADR 0022 Decision #7)

No LLM anywhere in this diagram — Discovery is deterministic, full stop.
This is the mechanism Diagram 4's "Fleet resolution"/live-relay boxes rely
on: Discovery connects to the Hub once and stays connected; the Hub relays
requests over that connection rather than ever dialing into a cluster
(which usually isn't reachable from the Hub — NAT, private VPC, firewall).
**Everything in this diagram happens either inside the cluster (Discovery)
or inside the one central process (Hub)** — there's no third location.
**This is not the only path a live diagnostic tool call can take** — see
Diagram 7 for the shorter one the Chat UI's "Run" buttons actually use, and
the one field (`cluster_id`) that decides between them.

```mermaid
sequenceDiagram
    participant Disc as Discovery<br/>(agentify-discovery, runs INSIDE the fleet cluster)
    participant Hub  as Hub<br/>(the one Go backend process — CollectorHub lives here)
    participant Agent as Python agent<br/>(DiagnoseSkill/HealthSkill/CertAuditSkill)

    Note over Disc,Hub: Connection setup — Discovery always dials out, Hub never dials in (once, reconnects with backoff on drop)
    Disc->>Hub: GET /api/collector/connect<br/>Authorization: Bearer {COLLECTOR_TOKEN}
    Hub->>Hub: resolveTenantContext(token) → (tenant_id, cluster_id)
    Hub-->>Disc: 101 Switching Protocols (WebSocket upgrade)
    Hub->>Hub: CollectorHub.Register(cluster_id, conn)

    loop Every SCAN_INTERVAL_SECONDS — Discovery-initiated, independent of the connection above
        Disc->>Hub: POST /api/cluster-inventory (namespaces + services)
        Disc->>Hub: POST /api/service-dependencies (mined edges)
        Disc->>Hub: POST /api/cluster-ingress (Ingress/Gateway+HTTPRoute/Route entry points, P18 #3)
        Disc->>Hub: POST /api/cluster-health (pod-readiness + K8s version snapshot, P18 #5)
    end

    Note over Agent,Disc: On-demand relay (as many times as needed, over the one open connection) — Agent only ever talks to the Hub
    Agent->>Hub: POST /api/live-fetch — cluster_id, tool=live_list_pods, args
    Hub->>Hub: liveFetchAllowedTools[tool]? then CollectorHub.RequestLive(cluster_id, ...)
    Hub->>Disc: relay request — id, type=request, tool=live_list_pods, args<br/>(over the already-open connection)
    Disc->>Disc: live_tools.dispatch(tool, args)<br/>— reads its OWN cluster's K8s API directly, own RBAC only
    Disc-->>Hub: relay response — id, type=response, result
    Hub-->>Agent: 200 — result

    Note over Hub: If no connection is registered for cluster_id,<br/>or Discovery doesn't answer within 15s,<br/>Hub returns 502/504 immediately — never blocks indefinitely
```

**Why this shape:** periodic push (inventory/dependencies) stays plain HTTP
POST — it already worked and didn't need the persistent channel. Only
on-demand request/response traffic uses the WebSocket. See the ADR 0022
amendment (2026-08-03) for why these weren't unified onto one connection.

**Code references**

| Hop | File | What it does |
|-----|------|---------------|
| Connection setup | [`handlers.go:2096`](../src/backend/internal/api/handlers.go#L2096) `HandleCollectorConnect` | Upgrades to WebSocket, registers the connection under `cluster_id` |
| Connection setup | [`live_relay.py:71`](../src/adapters/discovery/live_relay.py#L71) `run_forever` | Dials out, reconnects with capped backoff on drop |
| Agent → Hub | [`handlers.go:2155`](../src/backend/internal/api/handlers.go#L2155) `HandleLiveFetch` | Requires `cluster_id`; checks `liveFetchAllowedTools` |
| Hub → Discovery | [`collector_hub.go:152`](../src/backend/internal/api/collector_hub.go#L152) `CollectorHub.RequestLive` | Writes a WS frame over the already-open connection, awaits the matching response by `id` |
| Discovery execution | [`live_tools.py:64`](../src/adapters/discovery/live_tools.py#L64) `dispatch()` | K8s API call against Discovery's own cluster, own RBAC |

---

## 7. Chat "Run" button — direct live-tool-call (no collector relay)

No LLM anywhere in this diagram either, and — unlike Diagram 6 — **no
Discovery collector, no WebSocket, no `cluster_id`.** This is the path a
recommended action's "Run" button takes: three plain HTTP hops, ending in
the agent pod calling the Kubernetes API of the *cluster it itself runs
in*.

> [!TIP]
> **What actually runs when you click "Run":** Frontend → Hub (thin proxy)
> → Agent's own in-cluster K8s call. The Discovery collector and its
> persistent WebSocket (Diagram 6) are never touched — that path only
> activates when a query names a `cluster_id`, which recommended actions
> built by the chat-structuring prompt never populate.

```mermaid
sequenceDiagram
    participant FE  as Frontend<br/>(DiagnosisReport's "Run" button)
    participant Hub as Hub<br/>(the one Go backend process)
    participant Agent as Python agent<br/>(same in-cluster pod as the Hub talks to)
    participant K8s as Kubernetes API<br/>(the agent pod's OWN cluster)

    FE->>Hub: POST /api/live-query — tool=live_list_pods, arguments
    Hub->>Hub: HandleLiveToolCall: liveDiagnosticTools[tool]? (2nd, Hub-local allow-list)
    Hub->>Agent: POST /live-tool-call — tool, arguments (plain HTTP proxy, unchanged)
    Agent->>Agent: process_tool_call → _dispatch_live_diagnostic<br/>arguments has no cluster_id → stay local
    Agent->>K8s: GET /api/v1/namespaces/{ns}/pods<br/>(agent pod's own service-account token)
    K8s-->>Agent: pod list / logs / events
    Agent-->>Hub: 200 — result
    Hub-->>FE: 200 — result

    Note over Hub: liveDiagnosticTools deliberately excludes live_get_certificates —<br/>that tool has no local implementation and always requires cluster_id (see Diagram 6)
```

**Why this shape:** the Chat UI's recommended actions are meant for
quick, no-LLM confirmation of what a diagnosis already found — adding
fleet-cluster resolution here would cost a `resolve_service_clusters` round
trip for the common case (single-cluster deployments, or a query about the
same cluster the agent already runs in) where it can only ever be a no-op.
`DiagnoseSkill`'s own prefetch (Diagram 4) is the one caller that resolves
`cluster_id` and can hand it to `_dispatch_live_diagnostic` — a hand-built
`arguments` dict with `cluster_id` set would also take Diagram 6's relay
path through this exact same function, but no code path from the Chat "Run"
button constructs one today.

**Code references**

| Hop | File | What it does |
|-----|------|---------------|
| Frontend → Hub | [`api.ts:169`](../src/frontend/src/api.ts#L169) `runLiveTool()` | POSTs to `/api/live-query` |
| Hub proxy | [`handlers.go:1466`](../src/backend/internal/api/handlers.go#L1466) `HandleLiveToolCall` | Allow-list check (`liveDiagnosticTools`), then forwards as-is |
| Hub → Agent | [`agent_client.go:108`](../src/backend/internal/api/agent_client.go#L108) `AgentClient.LiveToolCall` | Plain HTTP POST — no `cluster_id` added |
| Agent dispatch | [`tools.py:578`](../src/agent/k8fy/tools.py#L578) `_dispatch_live_diagnostic` | Branches on whether `cluster_id` is in `arguments` |
| Recommended-action arguments | [`prompts.py:395`](../src/agent/k8fy/prompts.py#L395) `CHAT_STRUCTURE_PROMPT` | Only ever fills `namespace`/`pod` — never `cluster_id` |
| Local execution | [`live_diagnostics.py`](../src/agent/k8fy/live_diagnostics.py) | Direct in-cluster K8s API call, agent pod's own service account |

### Diagram 6 vs. Diagram 7 — which one actually runs

| | Diagram 6 — fleet relay | Diagram 7 — direct |
|---|---|---|
| **Trigger** | `cluster_id` present in `arguments` | `cluster_id` absent |
| **Caller** | `DiagnoseSkill`'s fleet fan-out (ADR 0023/0024) | Chat UI's "Run" button on a recommended action |
| **Hops** | Agent → Hub → Discovery → K8s API (a *different* cluster) | Frontend → Hub → Agent → K8s API (the *same* cluster the agent runs in) |
| **Connection** | Persistent WebSocket, opened once (`/api/collector/connect`) | Plain HTTP, one request per call |
| **Allow-lists** | `liveFetchAllowedTools` (Hub) + `live_tools.LIVE_TOOLS` (Discovery) | `liveDiagnosticTools` (Hub) + `LIVE_DIAGNOSTIC_TOOLS` (Agent) |
| **`live_get_certificates`** | Supported — the only path it has | Not supported (no local implementation) |

---

## Summary comparison

| Path | Skill | LLM calls | Tool loop | Fleet fan-out (ADR 0023/0024) | Models |
|------|-------|-----------|-----------|--------------------------------|--------|
| Tier-1 | — | **0** | no | no | — |
| Pattern A | HealthSkill | **1** | no | yes | Opus 4.8 |
| Pattern A | CertAuditSkill | **1** | no | yes | Opus 4.8 |
| Pattern A | DiagnoseSkill | **1** | no | yes | Opus 4.8 |
| Pattern B | K8fyAgent (fallback) | **1–N** (Opus) | yes | no | Opus 4.8 |

"Fleet fan-out" means the skill calls `resolve_service_clusters` and adds a
cluster-scoped prefetch task per matching cluster — a no-op (zero extra
calls) for any deployment with no registered fleet clusters.
