# 006 – Temporal ingestion & history query (the spine that makes diagnosis causal)

> Persists timestamped signals (restart-count samples first) and lets a query read
> them over a time window — so [spec 005](005-root-cause-correlation.md) can say
> *when* a problem started, not just that it exists. Realizes [ADR 0013](../decisions/0013-temporal-data-in-postgres-events-table.md).

## Goal

A diagnostic question can see a signal's **trend over time**, e.g. "restarts on
`payment-7c9-bbb` climbed 0→17 between 14:08 and 14:31" — instead of only the
current count. This is the prerequisite that turns P4a's current-state correlation
into temporal reasoning.

## Depends on

- [ADR 0013](../decisions/0013-temporal-data-in-postgres-events-table.md) — temporal
  data lands in the Postgres `events` table for v1 (no TSDB yet).
- [ADR 0010](../decisions/0010-postgres-single-store.md), [storage-strategy](../policies/storage-strategy.md)
  (trait classification), [correlation](../policies/correlation.md) (diagnose fan-out).

## Scope (v1) — and what is deliberately out

**In:**
- A new event namespace **`k8fy.metrics`**: append-only **restart-count samples**,
  one row per scrape, retained as history (not latest-wins).
- The events store `Query` honors a **time window** (`since`/`until`), an **entity**
  filter, an `event_type` filter, `order`, and `limit`.
- An agent tool **`get_metrics_history`** that fetches a windowed series, plus
  diagnose prompting that uses the *trend* (when it started) to inform the cause.

**Out (later increments, stay honest):**
- **CPU / memory** samples — need a metrics-server scrape; v1 ships **restarts only**
  (free from pod status). Noted as a known limitation.
- **Lifecycle event history** to `k8fy.events` — the profile exists, but emitting
  every `pod_modified` watch event is noisy (kubelet churns status constantly);
  deferred until we add transition-diffing or sampling.
- **Deploy/change correlation** ("crashed because of the 3pm deploy") — needs a
  change-event source we don't ingest. Still out (spec 005's bound narrows, not lifts).
- **Retention / downsampling / rate pre-computation** — the events table grows
  unbounded for now (ADR 0013 "revisit if"). Any rate/delta is computed
  deterministically, never by the LLM.

## Behavior

- **Given** restart samples for `payment-7c9-bbb` at t0=0, …, t30=17 **when** the
  metrics pod is queried with `since=t0` **then** it returns the samples in
  chronological order so the climb is visible.
- **Given** a `diagnose` query for `payment` **then** the multi-signal fan-out
  already includes the `k8fy.metrics` pod, so the agent sees recent restart samples
  in its initial data; it may call `get_metrics_history` to widen the window.
- **Given** no metric samples exist **then** diagnosis falls back to current-state
  (unchanged) and says so — it does not invent a trend.

## Interfaces

```
adapter: restart sample → event_namespace "k8fy.metrics"
  traits { shape: "numeric/metric", access_pattern: "time-range-scan",
           temporality: "append-only", mutability: "immutable" }
  → ingester routes to the events table (relational backend)

events store Query params (all optional):
  since, until : RFC3339 time bounds
  entity       : payload pod_id/service to filter to
  type         : event_type filter
  order        : "asc" | "desc" (default desc — preserves cert_check behavior)
  limit        : default 100, capped at 1000

routing:
  intent "metrics_query" | "metrics_history" → k8fy.metrics leaf pod
agent tool:
  get_metrics_history(pod_id|service, namespace, since?, until?, limit?)
```

## Open questions

- [ ] Shard `k8fy.metrics` by namespace (like live-state) once volume grows? (v1: single pod.)
- [ ] Where does the deterministic rate/delta helper live — backend query or a small
      agent-side pure function? (v1: agent reasons over raw cumulative samples.)
- [ ] Retention job for the events table (ADR 0013 defers it).

## Acceptance criteria

> Checked 2026-10-18. `get_metrics_history` is wired through
> `handlers.go`, `postgres.go`, `query_executor.go`, and `pod_profiles.go`,
> with coverage in `postgres_test.go`/`query_executor_test.go`, and is used
> by both `DiagnoseSkill` and `RestartTrendSkill`. Shipped and tested;
> simply never checked off.

- [x] Restart samples persist as **distinct rows** (append-only), not latest-wins.
- [x] The events `Query` returns only rows within `since`/`until` when given, in the
      requested order, and still defaults to recent-first (cert_check unaffected).
- [x] A `diagnose` fan-out includes `k8fy.metrics`; the agent can also call
      `get_metrics_history`.
- [x] Given a rising restart series, the agent's diagnosis references **when** the
      climb began; given no series, it does not fabricate a trend.
- [x] Temporal data is redacted at egress (ADR 0007) — unchanged.
