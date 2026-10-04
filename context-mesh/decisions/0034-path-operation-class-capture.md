# 0034 – Path / operation-class capture (ROADMAP P27 phase 4)

## Status

Accepted   ·   (date: 2026-10-04)

## Context

`context-mesh/ROADMAP.md`'s P27 phase 4 names four uncaptured dimensions of a
mined edge — provenance, caller cardinality, bucketed evidence, and
path/operation class. This decision covers path only; the other three remain
unbuilt, per an explicit choice to scope this pass to the one phase-4
sub-item that unblocks other work rather than all four at once.

Path is also the named, hard prerequisite for **ROADMAP P29 half (1)** — "given
`POST /charge`, show the diagram filtered to just that call" — which the
roadmap states outright is otherwise unbuildable: "an edge is currently
`from`, `to`, `count`... no diagram can show a dimension that is not
captured." This decision ships capture and passive exposure (an API field, a
table column, a capture-rate line) only — the interactive path-filtered
diagram itself is deferred to a follow-up once real path data exists to
design the filter UI against.

Port (ADR 0031, phase 2) is the closest precedent and the template this
decision follows where it can: same three producers
(`src/adapters/discovery/service_topology.py`'s live miner,
`src/agent/k8fy/dependency_miner.py`'s Glue miner, `src/agent/k8fy/
service_topology.py`'s opportunistic skill-prefetch miner), same shared,
duplicated extraction core, same schema pattern (a sentinel-valued column
joining the row's `UNIQUE` key). It differs in one load-bearing way: port has
small, naturally bounded cardinality (a handful of ports per pair); a raw
path does not (`/orders/12345` vs `/orders/67890` vs every other order id
ever logged). Storing it the way port was stored, without first bounding it,
would fragment evidence across effectively-infinite rows — the literal
opposite of what `evidence_count` is for.

## Decision

**Path is normalized before it is stored, never stored raw.** A new
`_normalize_path(raw: str) -> str` (added identically to both
`service_topology.py` copies, same verbatim-duplication convention port's
extraction code already follows) templates a path segment-by-segment:
a segment becomes `:id` when it is all-digits, UUID-shaped
(`8-4-4-4-12` hex), or a long hex run (≥16 hex chars); every other segment is
kept literal. `/orders/12345` → `/orders/:id`. Deliberately simple and
conservative — no OpenAPI inference, no learned/ML approach — consistent
with every other extraction heuristic in this pipeline (word-boundary
keyword stems, Service-list validation over hostname-shape heuristics). An
empty or implausibly long raw capture (>200 chars — more likely log prose
than a real path) normalizes to `""`, the same "not captured" sentinel port
and outcome already use.

**Capture mechanism.** `CallObservation` gains a `path: str = ""` field.
`extract_service_calls`'s three match loops (qualified FQDN, bare `//host`,
bare `host:port`) each now use `finditer` instead of `findall` so a match's
end position is available, and peek immediately after it with a new
`_PATH_SUFFIX_RE` — an optional `:<port>` then an optional
`/<path-up-to-the-first-whitespace/quote/bracket/comma/?>` — matching the
literal shape a URL takes right after its host
(`scheme://host[:port][/path]`). Stopping at `?` means a query string is
simply never captured, rather than captured and then stripped.

**Bonus fix bundled in, not scope creep requiring a separate decision:** the
qualified-hostname branch never looked past its own match at all before this
— a line like `"calling http://payment-backend.payments.svc.cluster.local:
8080/charge"` (an existing pinned test fixture) captured neither port nor
path even though both were right there. Reusing the same peek-after-match
helper for this branch fixes that for port too, for free, with no separate
migration — the port column and its semantics already exist from ADR 0031.

**Schema: `service_dependencies` gains `path TEXT NOT NULL DEFAULT ''`, and
the `UNIQUE` constraint widens to include it** —
`(tenant_id, cluster_id, namespace, from_service, to_service, port, path)`,
replacing the port-only constraint ADR 0031 added. Same reasoning port's own
ADR gave for joining the key rather than aggregating: an edge that aggregates
every path into one row loses exactly the dimension the "API surface" and
"probe filter" diagrams (ROADMAP P27's own table of what each phase
unlocks) need — which endpoints does this edge actually cover.

**The migration touches all three of `service_dependencies`'s constraint
migrations, not just adds a fourth.** `postgres.go`'s existing comments
document a real bug found live on 2026-09-12: the ADR 0022 tenant/cluster
migration and the ADR 0031 port migration each originally recognized only
their *own* target constraint name as "already migrated," so a restart after
the later one had run made the earlier block see an unrecognized name, drop
it, and try to recreate its own narrower constraint — which failed outright
once real data had multiple rows differing only by the later field, and took
the whole Postgres connection down with it (`buildBackendFactory` treats a
schema-init failure as total unavailability, not just this table). That fix
was "each must recognize the *other's* name too." Adding a third stage means
*every* earlier block must now recognize the newest name as well, or the
exact same bug reappears one stage later. Both the ADR 0022 block and the
ADR 0031 port block had their exclusion lists widened to include
`service_dependencies_tenant_cluster_ns_svc_port_path_key`, and a new test
(`postgres_test.go`, mirroring the existing ADR 0031 regression test
exactly) proves `initSchema` stays idempotent with path-differentiated data
already present.

**Per-cycle dedup keys widen to include path, in all three producers.**
ADR 0031 established "last known outcome per `(to_service, port)` wins
within one cycle's window, pushed at most once." Two distinct paths on the
same `(service, port)` are now two distinct rows, not one — leaving the
dedup key as `(service, port)` would silently collapse one path's evidence
into whichever observation happened to be seen last. `service_topology.py`'s
`mine_service_dependencies`, `dependency_miner.py`'s `_mine_namespace`
(including its cross-pod `pushed` set), and `discovery/main.py`'s
`_scan_namespace` all widen their dedup keys to
`(service, port, path)` and thread `path` through to their respective push
functions (`upsert_service_dependency`, `_push_edge`, `push_dependency`),
sent explicitly as `""` when unknown — never omitted — same convention as
port and outcome.

**UI: passive exposure only, no new interactive filter.** `ServiceDependency`
(Go and TypeScript) gains `path`/`Path`; the Dependencies panel's edge table
gains a Path column next to Port; a quiet, non-alarming line — "Path known
for N% of edges" — surfaces the capture rate, honoring P27's own explicit
rule that every added field ships with its own visible capture-rate signal
or a mostly-empty one gets silently trusted as complete. No change to
`DependencyFlow`'s diagram rendering (it already draws one arrow per edge
row with no dedup step, confirmed by reading `buildGraph`, so port-splitting
and now path-splitting both render correctly with zero additional code) and
no change to `_dependency_answer`'s chat prose (more rows per pair doesn't
break its existing counts).

## Consequences

- **Positive:** unblocks ROADMAP P29 half (1) — the path-filtered diagram
  view — at the schema/extraction layer; only the UI filter itself remains.
- **Positive:** unlocks two of P27's own named diagram ideas ("API surface,"
  "probe filter") once someone builds them, since the data they need now
  exists.
- **Positive:** the qualified-FQDN port/path bonus fix closes a real,
  previously-silent gap in phase 2's own coverage, at no extra migration
  cost.
- **Negative / cost accepted:** normalization is a heuristic, same class of
  limitation ADR 0031 accepted for outcome inference. A path whose
  identifier segment doesn't match any of the three patterns (a slug, a
  short code, an opaque token that isn't hex-shaped) is stored literal and
  will fragment evidence the way an unnormalized path would — visible
  partial coverage via the capture-rate line, not a silently wrong claim of
  completeness.
- **Negative / cost accepted:** a dependency edge can now span even more
  rows than port splitting alone produced (one per distinct `(port, path)`
  pair observed) — accepted for the identical reason port's own ADR
  accepted the same tradeoff: aggregating away the dimension defeats the
  reason it was captured.
- **Revisit if:** real production data shows the normalizer under-templates
  badly enough that row count per pair grows unreasonably (the fix would be
  a richer normalizer — e.g. slug detection — not a schema change); or if
  P29's actual filter UI surfaces a requirement (e.g. matching `/charge` to
  its already-normalized form) this decision didn't anticipate.
