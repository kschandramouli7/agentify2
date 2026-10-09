# 0038 – Cold services view (ROADMAP P31 phase 2)

## Status

Accepted   ·   (date: 2026-10-09)

## Context

agentify's mined evidence (`evidence_count`, `last_seen`) is useful for a
decommissioning decision only if it can answer "has anything happened
recently," and no existing view asks that question — `evidence_count` is
an all-time cumulative counter, and nothing surfaces "scanned recently,
nothing seen" as a distinct, actionable signal. This is the second piece
of the CPaaS migration decommissioning work (after ADR 0037's
cross-cluster call capture): a "which services have gone quiet" view,
cheap because it needs no new schema — the columns it reads
(`service_dependencies.last_seen`, `scan_coverage.last_scan`) already
exist.

## Decision

**"Cold" = scanned recently AND not seen recently (or never seen at
all).** Both halves matter independently: without the "scanned recently"
half, a service that simply fell out of the sampling window (P27 phase
1's whole reason for existing — `scan_coverage` disambiguates "called
rarely" from "never sampled") would be indistinguishable from one that's
genuinely gone quiet. Without the "never seen at all" half, a service that
was *always* dead — the strongest possible cold signal — would be silently
dropped by any query that only looks at existing evidence rows.

### Query shape: start from `scan_coverage`, not from activity

`ListColdServices` starts its `scanned` CTE from `scan_coverage` and
`LEFT JOIN`s activity onto it, deliberately the opposite direction from
what might seem natural. A service that was scanned but has **never** had
any edge evidence has no row in an activity-first query at all — starting
there would silently drop exactly the case this feature exists to catch.
Starting from `scanned` means a NULL `last_seen` after the join reads as
"never," not "missing."

No `target_kind` filter anywhere in the query: a service kept warm only by
a `cross_cluster` edge from elsewhere in the fleet (ADR 0037) must
correctly read as not-cold, or this feature would have a blind spot
exactly where the migration work matters most — the Go doc comment on
`ListColdServices` says this plainly, since it is an easy invariant for a
later "optimization" to accidentally break.

### The shared "cold" predicate, factored once

`ListColdServices` (this ADR) and the planned `ListCrossClusterPairs`
(ADR 0039) both need the same notion of "cold" — factored into one Go
string constant, `coldServiceWhereClause`, using explicit `fmt.Sprintf`
argument indices (`%[1]d`/`%[2]d`) rather than positional `%d`, so each
caller states unambiguously which SQL parameter number binds to which
half regardless of that query's own parameter ordering. This was a real
bug caught during implementation: naive positional `%d` numbering bound
`staleDays` to the wrong half of the clause on the first attempt — explicit
indices make that class of mistake visible at the call site instead of
silent.

### Tier-1 deterministic intent, not a model call

Mirrors `DependencyGraphSkill`'s exact posture (ADR 0029's "a plain
extraction task; no Claude call belongs anywhere in this pipeline,"
applied to reading the graph back): "which services are cold" has one
correct answer already sitting in Postgres. New keyword routing
(`coldServicesKeywordRE` in Go, `_COLD_SERVICES_KEYWORD_RE` in Python,
kept in sync the same way the existing dependency-question regexes
already are) is checked **before** the general dependencies branch in both
`inferIntent` and `_chat_route` — "what's safe to decommission" is a more
specific question about the same graph, and diagnostic phrasing
("why does X look dead") still wins over both, same precedence the
dependencies branch already respects.

### Thresholds are tunable, not hardcoded

`GET /api/cold-services?stale_days=&scanned_within_days=` defaults to 14
and 2 respectively but accepts overrides — these are a judgment call
about what "quiet" means for a given fleet's traffic patterns, not a
universal constant.

### UI: passive, composes with the existing path filter

A "Show cold only" checkbox in the Dependencies panel, fetched only when
toggled on (not on every namespace load), filtering the same
`pathFilteredData` the path-filter feature already produces — an edge
shows when it touches a cold service on *either* side, mirroring
`ListColdServices`'s own both-directions aggregation. An honest empty
state ("no services currently look cold") distinct from the generic
"no dependency evidence" one, same "say why, don't just show blank"
convention path filtering already established.

## Consequences

- **Positive:** a decommissioning candidate list now exists, assembled
  entirely from data this product already collects — no new mining, no
  new producer changes.
- **Positive:** the shared `coldServiceWhereClause` constant means ADR
  0039's pairing feature inherits a tested, bug-caught-during-development
  definition of "cold" rather than re-deriving its own.
- **Negative / cost accepted:** "cold" is binary per the two thresholds
  chosen at query time — no bucketed/graduated staleness (that's P27
  phase 4's still-unstarted "bucketed evidence" sub-item, a separate,
  broader concern).
- **Negative / cost accepted:** this is a candidate list, not a
  decommission order — it reflects mined log evidence only, bounded by
  the same sampling caps (`MAX_PODS_PER_NAMESPACE`, log tail length) every
  other view in this product already inherits. The skill's own prose
  states this explicitly on every answer, cold or not.
- **Revisit if:** operators want a per-service override of the default
  thresholds (e.g., a batch job that's expected to go quiet for weeks
  between runs) — today every namespace shares one threshold pair per
  request, with no per-service annotation to suppress a false positive.
