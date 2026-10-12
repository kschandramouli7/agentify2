# 0040 – Bucketed evidence capture (ROADMAP P27 phase 4)

## Status

Accepted   ·   (date: 2026-10-12)

## Context

`service_dependencies.evidence_count` (and its three outcome counters) is a
running total since `first_seen`. This item's own ROADMAP entry names the
consequence plainly: a single counter cannot separate steady traffic from a
02:00 daily burst, and the "`payment-worker`'s sighting rate is declining"
finding that originally motivated all of P27 was only visible by comparing
two totals by hand, across two separate manual queries days apart — not
something the schema could answer directly.

This is the fourth and last of phase 4's four sub-items to ship, after
provenance (ADR 0036), caller cardinality (ADR 0035), and path/operation
class (ADR 0034) — all three already shipped.

## Decision

**Hourly buckets, captured passively in the same transaction as the edge
upsert, joined into the existing `ListServiceDependencies` read as two
pre-summed 24h windows.**

- **Granularity: hourly.** The motivating example (a 02:00 burst vs. a
  steady trickle across the same day) needs sub-day resolution — daily
  buckets would make the two indistinguishable, defeating the point.
- **`bucket_start` is computed server-side** as `date_trunc('hour', NOW())`
  at upsert time, never client-supplied. This avoids clock-skew between the
  three producers (live miner, Glue miner, agent skill) the same way
  `last_seen`'s own `NOW()` already does for the parent row — and means
  **zero changes were needed in any producer**: `UpsertServiceDependency`
  already runs once per observation with every fact this needs already in
  scope, so the bucket insert is purely additive to the existing
  transaction, the same posture caller cardinality and provenance both used.
- **A new child table, `service_dependency_buckets`**, not a change to
  `service_dependencies` itself — same reasoning path/outcome/caller
  cardinality each already established: this is a parallel per-interval
  fact about the edge, not a replacement for the all-time running total,
  which the health banner's `MIN_CLASSIFIED_FOR_HEALTH` threshold and
  caller-cardinality join both still depend on unchanged.
- **The UI payoff is a trend word, not a chart.** `ListServiceDependencies`
  gains a second new LEFT JOIN subquery — two conditional `SUM`s over
  `service_dependency_buckets`, `last_24h_count` (`[now-24h, now)`) and
  `prior_24h_count` (`[now-48h, now-24h)`) — joined into the edge the
  frontend already fetches, no new endpoint. `DependencyFlow.tsx`'s new
  `evidenceTrend()` compares the two (±20% treated as "steady", matching
  the explicit-threshold style `edgeHealth()`'s own `badFraction >= 0.5`
  already uses) and adds one line to the existing edge tooltip:
  "Rising/Steady/Declining — N calls in the last 24h vs M in the prior
  24h." No new chart, no new column, no diagram changes — same "passive
  UI only" posture every other P27 phase 4 sub-item shipped with.
- **Retention is a hard requirement, not an afterthought** (explicitly
  asked for alongside this feature): this table grows by a fixed ~24
  rows/day per edge regardless of fleet size or pod churn — a different,
  more predictable growth profile than `service_dependency_callers` (bounded
  by pod churn) or `service_dependencies` itself (bounded by distinct
  edges), but still unbounded over time without a janitor. Reused
  `src/backend/internal/retention/`'s existing generalized `Janitor`
  (ADR 0015, generalized for a second table by ADR 0035) for a third time —
  own dedicated config (`SERVICE_DEPENDENCY_BUCKETS_RETENTION_DAYS`/
  `_INTERVAL_MINUTES`, defaults 30/60, same defaults as the other two
  tables but independently tunable) and its own adapter
  (`serviceDependencyBucketsPurger` in `main.go`), since `*Client` can't
  implement `PurgeOlderThan` three times under three names on one receiver.

### What was confirmed before designing, not assumed

- `UpsertServiceDependency` already runs inside one transaction per
  observation (`postgres.go`) — the bucket insert rides that same
  transaction, so the running total and its hourly breakdown can never
  drift relative to each other.
- The existing `cpc` (caller-pod-count) subquery in `ListServiceDependencies`
  already establishes the "aggregate a child table once per edge key in a
  LEFT JOIN subquery, not per row" pattern this reuses verbatim for the two
  new `SUM`s.
- RLS on `service_dependency_buckets` needs the same explicit `GRANT` the
  restricted test role already needed for `cluster_health_snapshots`
  (caught as a real bug during the platform-labeling work) — added
  proactively this time, confirmed by `TestServiceDependencyTenantIsolation`
  passing against the new table on the first attempt.

## Consequences

- **Positive:** the exact gap this item named is closed — an edge's recent
  trend is now a server-computed fact, not something a reader has to infer
  by comparing two manually-pulled totals days apart.
- **Positive:** zero producer changes (Python) were needed — the entire
  feature lives in `postgres.go` (schema, upsert, purge) and `main.go`
  (janitor wiring), plus the two-line frontend tooltip addition. Same
  "passive, additive, no producer touches needed" shape as provenance and
  caller cardinality.
- **Negative / cost accepted:** a new table with its own RLS policy and its
  own retention janitor — more schema surface than a single counter would
  have been, because distinguishing "which hour" from a running total is a
  genuinely different kind of fact, the same tradeoff caller cardinality's
  ADR already accepted for "which pod."
- **Negative / cost accepted:** the ±20% "steady" band is a fixed heuristic,
  not a statistically-derived threshold — tuned for "don't flip-flop between
  rising/declining on trivial noise," not validated against real traffic
  distributions yet.
- **Revisit if:** the ±20% band proves too noisy or too insensitive in
  practice once more real data accumulates; or a future need (e.g. P25's
  change correlation) wants the raw bucket series itself rather than two
  pre-summed windows, which would justify adding a dedicated
  `ListServiceDependencyBuckets` read path and endpoint — deliberately not
  built now, since nothing in this pass needed more than the two sums.
