# 004 – Query provenance & audit trail

> "Why did the AI say that, and what did it see?" — extend the `sources` we already
> return into a correlated, auditable trace per query. Builds on
> [ADR 0006](../decisions/0006-two-tier-query-path.md) (tiers),
> [ADR 0007](../decisions/0007-egress-data-governance.md) (redaction), and
> [ADR 0011](../decisions/0011-self-observability-metrics.md) (metrics).

## Goal

Every query carries a `trace_id` returned to the caller and propagated to the
agent, plus one complete structured `query.trace` record that ties the answer to
its inputs — so an operator (or auditor) can reconstruct how any answer was
produced.

## Depends on

- Policies: [correlation](../policies/correlation.md), [data-governance](../policies/data-governance.md).
- Decisions: ADR 0006 (tier), ADR 0007 (provenance shows the **redacted** view).

## Context / constraints

- **trace_id is the spine.** Generated per `/api/query`, returned in the response,
  and sent to the agent so both services' logs correlate (closes the correlation-ID
  gap deferred in ADR 0011).
- **Provenance shows what the model saw — the redacted view** (ADR 0007), via the
  pod `sources` and the agent's tool calls. The audit trail is **not** a second
  copy of raw, pre-redaction data.
- **v1 emits the trace as a structured log** (`query.trace`), queryable via the log
  stack (Loki/CloudWatch). App-level retrieval-by-id is deferred (see Non-goals).
- **Non-goals (v1):**
  - A Postgres-persisted trace table + `GET /admin/traces/{id}` retrieval API — it
    largely duplicates log search and adds a retention burden; deferred.
  - Capturing the **exact prompt text** the agent built (agent-side; the inputs
    (`sources`) + tool calls are captured, which is the substance).
  - Retention/pruning policy for any persisted traces.

## Behavior

- **Given** any query **then** the `QueryResponse` includes `trace_id`, and a single
  `query.trace` log is emitted with: trace_id, question, intent, namespace, tier
  (tier1|tier2|no_data), status, confidence, `sources` (pod ids), tool calls
  (tier2), latency_ms.
- **Given** a Tier-2 query **then** the same `trace_id` is sent to the agent, which
  logs it — so the agent's reasoning logs join the backend trace by id.
- **Given** a Tier-1 (deterministic) answer **then** the trace records tier1 with no
  tool calls and no model input (nothing egressed).

## Interfaces

```
QueryResponse { ..., trace_id }
AgentRequest  { intent, data, context, trace_id }   # trace_id propagated to the agent
AgentResponse { answer, confidence, sources, tool_calls }  # tool_calls now captured for the trace
log: "query.trace" { trace_id, question, intent, namespace, tier, status, confidence, sources, tool_calls, latency_ms }
```

## Open questions

- [ ] Persist traces to Postgres for app-level retrieval-by-id, or rely on the log
      stack? (v1: logs. Revisit if enterprises need an in-product audit view.)
- [ ] Capture the exact agent prompt (agent-side) for full reproducibility?
- [ ] Retention policy once/if traces are persisted.

## Acceptance criteria

> Checked 2026-10-18. `handlers.go:1004-1005` logs `"query.trace"` with
> `trace_id` on every query; `trace_id` threads through `types.go`,
> `agent.py`, and the dedicated `trace_search.py` feature built on top of
> it, with coverage in `test_chat_trace.py`/`test_trace_search.py`. Shipped
> and tested; these were simply never checked off.

- [x] Every `/api/query` response carries a non-empty `trace_id`.
- [x] A `query.trace` log per query includes tier, sources, status, confidence, latency.
- [x] The agent receives and logs the same `trace_id` (cross-service correlation).
- [x] Tier-2 traces list the tool calls the agent made.
