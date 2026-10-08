# 0035 – Caller cardinality capture (ROADMAP P27 phase 4)

## Status

Accepted   ·   (date: 2026-10-08)

## Context

A mined edge says `from_service` calls `to_service` — it carries no notion of
**how many distinct pods** of `from_service` actually made that call. ROADMAP
P27 phase 4 names this plainly: "1 of 5 replicas" versus "all 5" is a large
semantic difference (leader-only work, sharded traffic, a canary mid-rollout)
the schema currently aggregates away entirely. This is the second of phase
4's four sub-items to ship, after path/operation class (ADR 0034); the other
two (provenance, bucketed evidence) remain unbuilt.

## Decision

**A new child table, `service_dependency_callers`, one row per (edge,
pod_name) ever seen, with `COUNT(DISTINCT pod_name)` computed at read time**
— not a counter column overwritten per cycle. This was a genuine, explicit
fork, put to the user before any code was written:

- The alternative — an overwritten `caller_pod_count` column on
  `service_dependencies` itself — needs no new table and no retention job,
  but would require restructuring producers 1 (Discovery's live miner) and 2
  (the Glue miner) from their current push-per-pod-per-edge loop into
  push-once-per-edge-after-aggregating-every-sampled-pod: a real change to
  carefully-commented, three-ADRs-deep loop structure (ADR 0029/0031/0034's
  dedup keys all live there), and it would only ever reflect the latest
  cycle, with no "distinct over the last N days" history.
- The child table is **purely additive** to those loops instead: each
  producer already has pod identity in scope at its existing per-pod push
  call site (confirmed by reading the code before designing, not assumed —
  see below), so this threads one more string through, the same shape
  port/outcome/path were each added in prior phases. The cost is a new
  table needing its own retention janitor, which turned out to be a small,
  already-proven pattern in this codebase (`src/backend/internal/retention/`,
  built for `events` under ADR 0015) — reused here, not reinvented.

A simple counter column could never have correctly answered "is this a pod
we've already counted" without storing the set itself — this is a SET
relationship ("which pods have called this"), not a per-observation scalar
fact the way port/outcome/path are, which is the structural reason a
counter was never really on the table as a *correct* option, independent of
the loop-restructuring cost argument above.

### What was confirmed true in the code before designing, not assumed

- **Discovery's live miner**: `pod["name"]` is in scope at its existing
  `push_dependency(...)` call, inside the same per-pod loop
  (`for pod, from_service in attributed[:cfg.max_pods_per_namespace]:`).
  `MAX_PODS_PER_NAMESPACE` defaults to 5, capping this producer's per-cycle
  contribution.
- **The Glue miner**: rows are grouped by `pod_name` first; the existing
  `_push_edge(...)` call sits inside `for pod_name, lines in
  by_pod.items():` — `pod_name` was already in scope there too.
- **The opportunistic skill-prefetch miner** (`mine_service_dependencies`):
  its one real call site (`diagnose.py`'s `_prefetch`, capped at
  `_MAX_TOPOLOGY_PODS = 2`) is itself inside `for pod_id in
  topology_pod_ids:`, and each call already receives that one pod's own
  single-pod `log_text` — but `pod_id` was dropped on the floor;
  `mine_service_dependencies`'s signature had no pod parameter at all. A
  signature gap, not a missing-data gap — fixed by adding `pod_id: str =
  ""` to the function itself.
- **The read-time-join precedent this mirrors exactly** — `ListServiceDependencies`
  already `LEFT JOIN`s `cluster_services` for ADR 0032's
  `expected_failure_reason` rather than storing it denormalized.

### A real bug found while wiring this up, not anticipated in the original plan

The Glue miner's cross-pod `pushed` dedup set was keyed on `(from, to, port,
path)` only — **not pod**. Its purpose predates caller cardinality: "push
each discovered edge at most once per cycle, since `evidence_count`
accumulates Hub-side anyway." That was correct for its own purpose, but it
would have silently suppressed every pod after the first to confirm the same
edge in one cycle — the exact signal caller cardinality exists to capture.
Fixed by widening the key to `(from, to, port, path, pod_name)`, so each
distinct contributing pod gets its own push. Caught by writing a test that
exercises two different pods confirming the same edge in one cycle
(`test_mine_namespace_pushes_once_per_distinct_contributing_pod`), not by
inspection — the original three-tuple-plus-port-plus-path key read as
correct until checked against this specific scenario.

### Schema and upsert

```sql
CREATE TABLE service_dependency_callers (
    tenant_id, cluster_id, namespace, from_service, to_service, port, path,
    pod_name, first_seen, last_seen,
    PRIMARY KEY (tenant_id, cluster_id, namespace, from_service, to_service, port, path, pod_name)
);
```
No separate `id` column — the composite key already uniquely identifies a
row. Same `ENABLE`/`FORCE ROW LEVEL SECURITY` + `tenant_isolation` policy
`service_dependencies` already uses. `UpsertServiceDependency` gained a
`callerPod` parameter; when non-empty, it upserts into this table in the
**same transaction** as the edge row, so the two can never land
half-written relative to each other. **When empty, the caller-pod upsert is
skipped entirely — never inserted as `pod_name = ''`.** An empty pod name
is not a real caller identity; inserting one would silently inflate every
affected edge's distinct-caller count by one, for every producer that
hasn't yet been updated to report it (an older collector, or
`mine_service_dependencies` before this phase).

### Retention

Pod names churn on every redeploy, so without bounding, this table would
grow forever — unlike `service_dependencies` itself, naturally bounded by
distinct `(from, to, port, path)` tuples. Reused
`src/backend/internal/retention/`'s existing `Janitor`/`Purger` pattern
(ADR 0015), but **generalized it first**: the original hardcoded
`telemetry.EventsPurgedTotal` inside `Janitor.purgeOnce`, so a second
`Janitor` instance for a second table would have silently misattributed its
deletions to the events metric. `retention.New` now takes a `name` (for log
lines) and a `prometheus.Counter` (for its own metric) per instance. `*Client`
can't implement `PurgeOlderThan` twice under two names on one receiver, so
the second table's adapter (`serviceDependencyCallersPurger`) is a small
type defined in `main.go` itself — keeping `main.go`'s existing posture of
duck-typing against the relational backend via an anonymous interface,
rather than importing the `postgres` package directly, which the original
events-janitor wiring never did either.

Own dedicated config (`SERVICE_DEPENDENCY_CALLERS_RETENTION_DAYS`/
`_INTERVAL_MINUTES`, defaults 30/60 — same defaults as events, but tunable
independently), matching the existing convention that every retention
concern in this codebase gets its own knobs.

### UI

Passive only, same posture path's own rollout used: a **Callers** column on
the edge table, showing the count or `—` when 0 (never a blank cell — same
rule the Health column already states). Framed as "N of M replicas" when
the *from*-service's own known replica count is available from its service
profile — the literal motivating example this roadmap item names — but the
bare count stands alone correctly when that context isn't available. No
diagram or filter changes needed.

## Consequences

- **Positive:** the mined graph can now distinguish "every replica makes
  this call" from "one replica, maybe the leader, maybe a canary" — exactly
  the semantic gap this item named.
- **Positive:** fixing the Glue miner's `pushed` dedup key is a genuine
  correctness improvement independent of this feature — `evidence_count`
  was previously undercounting whenever multiple pods independently
  confirmed the same edge in one cycle, since only the first pod's
  observation was ever pushed.
- **Positive:** generalizing `retention.Janitor` to accept its own name and
  metric makes the pattern actually reusable for a third table later,
  rather than needing another near-duplicate type.
- **Negative / cost accepted:** a new table, its own RLS policy, and its own
  retention janitor — more schema surface than port/path/outcome each
  needed, because this is a genuinely different kind of fact (a set
  relationship, not a per-observation scalar).
- **Negative / cost accepted:** `MAX_PODS_PER_NAMESPACE` (default 5) and
  `_MAX_TOPOLOGY_PODS` (2) both cap the realistic per-cycle cardinality
  contribution well below a service's true replica count for any namespace
  with more replicas than that — the column answers "how many *sampled*
  pods confirmed this," not "how many replicas actually call this," same
  sampling caveat the rest of this mining pipeline already carries.
- **Revisit if:** a namespace's real replica counts routinely exceed the
  sampling caps badly enough that the "N of M" framing becomes misleading
  rather than merely a lower bound (the fix would be raising the sampling
  caps, a cost/volume tradeoff already flagged elsewhere in P27, not a
  change to this decision); or if P27 phase 4's remaining two sub-items
  (provenance, bucketed evidence) surface a reason to fold caller identity
  into a differently-shaped table than this one.
