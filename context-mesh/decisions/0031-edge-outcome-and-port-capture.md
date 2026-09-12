# 0031 – Edge outcome and port capture (ROADMAP P27 phase 2)

## Status

Accepted   ·   (date: 2026-09-12)

## Context

`context-mesh/ROADMAP.md`'s P27 ("Edge Enrichment") names the core defect in
the mined service-dependency graph plainly: a row is four facts —
`from_service`, `to_service`, `evidence_count`, `first_seen`/`last_seen` — so
**a healthy call and a failed one produce identical rows**. Phase 2 is the
fix P27 itself calls "pure parsing wins, biggest payoff" and "now the gate on
P24" (Policy Synthesis, which cannot generate a NetworkPolicy without a
port — "a NetworkPolicy with no port can only say `allow all ports`").

Two fields, both already present in the log text every producer already
reads, both previously discarded:

- **Outcome** — success / failure / timeout, turning "does this dependency
  exist" into "is it working."
- **Port** — from the bare `host:port` log form only; a qualified FQDN
  mention (`payment-api.payments.svc.cluster.local`) carries no port in the
  matched text itself.

Three producers share this extraction path (`docs/SERVICE_DEPENDENCIES.md`
§3: the live per-cluster miner, the Glue/Athena miner, and the diagnose
skill's opportunistic miner), all calling the same `extract_service_mentions`
function (agent and Discovery each carry an identical copy; the Glue miner
imports the agent's). Any change here is cross-cutting by construction.

This is P27's second decision requiring its own record. The roadmap already
flags the first as a gap: "phases 1 and 3 shipped with no ADR — one is
owed." Phase 2 changes the table's `UNIQUE` key, which is a genuinely
hard-to-reverse decision on its own, so it gets an ADR rather than repeating
that omission.

## Decision

**`extract_service_mentions` is not modified in place.** It is pinned by
~26 tests across the agent and Discovery copies and imported verbatim by
`dependency_miner.py`. A new function, `extract_service_calls(log_text,
namespace, known_services) -> List[CallObservation]`, is added beside it in
both `src/agent/k8fy/service_topology.py` and
`src/adapters/discovery/service_topology.py` (identical in both, matching
the existing verbatim-duplication convention for this pure-function
section). `CallObservation` is a small frozen dataclass: `service`, `port:
Optional[int]`, `outcome: Optional[str]`.

`extract_service_mentions` is reimplemented as `{obs.service for obs in
extract_service_calls(...)}` — behavior-preserving, confirmed by running
its full existing test suite unchanged (all pass) rather than assumed: none
of the module's regexes can match across a newline, so scanning line-by-line
(required for per-line outcome context) finds exactly the same mentions the
whole-blob scan always has.

**Outcome inference (`_infer_outcome`) is a new, deliberately conservative
function**, checked in confidence order so a stronger signal never loses to
a weaker one on the same line:

1. An HTTP status code following a trigger word/symbol (`->`, `status`,
   `responded`, `response`, `returned`) — `2xx`/`3xx` → `success`, `4xx`/
   `5xx` → `failure`.
2. `timeout` / `timed out` → `timeout`.
3. `unreachable` / `refused` / `failed` / `failure` / `unavailable` →
   `failure`.
4. `\bok\b` → `success`.
5. No match → `None` (unknown) — returned far more often than a real APM
   would, deliberately. Outcome vocabulary is per-logger; a wrong guess
   corrupts the confidence model `evidence_count`/coverage already builds
   on, while an honest "unknown" just doesn't increment an outcome counter.

**Port is captured only from the bare `host:port` form.** No attempt is
made to associate a port with a qualified FQDN mention from elsewhere on
the line — visible partial capture, not a gap being silently papered over.

**Schema: `service_dependencies` gains `port INT NOT NULL DEFAULT 0` and
three outcome counters** (`outcome_success_count`, `outcome_failure_count`,
`outcome_timeout_count`, each `INT NOT NULL DEFAULT 0`). No separate
"unknown" counter — it is `evidence_count - (success+failure+timeout)`,
derivable rather than stored.

**`port = 0` is the sentinel for "not captured," never SQL `NULL`.** Real
ports are 1–65535. The table's `UNIQUE` constraint includes `port`
(`(tenant_id, cluster_id, namespace, from_service, to_service, port)`,
replacing the prior constraint that omitted it), and Postgres treats every
`NULL` as distinct from every other `NULL` in a unique index — a `NULL`
port would silently defeat evidence accumulation for every edge whose port
was never observed, precisely the bug the tenant/cluster migration already
worked around once for `cluster_id` (see that migration's own comment in
`postgres.go`). The constraint swap reuses that exact drop-and-recreate
`DO $$ ... $$` pattern, generalized to find whichever unique constraint
currently exists on the table rather than naming the old one explicitly, so
it runs correctly whether it follows that earlier migration or, on a fresh
database, right after `CREATE TABLE`.

**`UpsertServiceDependency` increments at most one outcome counter per
call**, via `CASE WHEN $n = '...' THEN 1 ELSE 0 END` on the `INSERT` and the
matching `ON CONFLICT DO UPDATE`'s accumulation — mirroring
`UpsertScanCoverage`'s existing `x = table.x + EXCLUDED.x` pattern (P27
phase 1) one column over, not a new idiom.

**Per-cycle push semantics: the "at most once per cycle" rule (ADR 0029,
already established for the Glue miner) extends its key to include port,
and reports the *last KNOWN* outcome observed in that cycle's window.**
Concretely, in each producer: group that cycle's `CallObservation`s by
`(to_service, port)`, and when the same key is seen again, overwrite the
stored outcome only if the new observation actually has one (`obs.outcome
is not None`) — a later line that merely re-mentions the target without a
classifiable outcome must not erase an earlier confident one. This is a
real, debatable judgment call, not an obviously-correct default: "most
recently observed state" was chosen over "first observed" because the
value of this data is answering "is this working *now*," and log tails are
processed in chronological order.

- **`dependency_miner.py`** (Glue miner): the existing namespace-wide
  `pushed` set becomes keyed on `(from_service, to_service, port)`; the
  per-pod last-outcome grouping happens before that dedup check, within one
  pod's own accumulated log text for the cycle.
- **`discovery/main.py`** (`_scan_namespace`, live miner): per-pod push
  granularity is **unchanged** — each sampled pod still pushes
  independently, exactly as before this phase. P27 phase 4's "caller
  cardinality" (how many distinct pods made a call) is a separate,
  not-yet-built concern; fixing it here would have been out of scope.
- **`service_topology.py`'s `mine_service_dependencies`** (diagnose-skill
  miner): the same last-known-outcome grouping, scoped to the one
  `log_text` blob a diagnose call already fetched.

All three push functions (`_push_edge`, `push_dependency`,
`upsert_service_dependency`) gain `port`/`outcome` parameters and send them
as `0`/`""` when unknown — sent explicitly, never omitted from the request
body, so the Hub-side handler never has to distinguish "an older collector
that doesn't know about this field" from "a producer that captured nothing
this time." Both already mean the same thing: unknown.

## Consequences

- **Positive:** the mined graph can answer "is this dependency working," not
  just "does it exist" — the roadmap's own stated reason this is the
  highest-leverage unbuilt piece of P27, and the direct unlock for P24
  (Policy Synthesis), which was otherwise permanently blocked on port.
- **Positive:** the refactor that makes this possible
  (`extract_service_calls` underneath `extract_service_mentions`) was
  verified behavior-preserving by running the existing pinned test suite
  unchanged, not merely reasoned about — all ~26 tests across both copies
  pass with zero edits.
- **Negative / cost accepted:** outcome inference is a heuristic over
  per-logger, uncontrolled vocabulary. It will under-classify (return
  `None`) far more often than it misclassifies, which is the intended
  trade — but it is not, and cannot be, a general log-parsing solution.
- **Negative / cost accepted:** a dependency edge now potentially spans
  multiple rows (one per distinct captured port, plus one `port=0` bucket
  for unknown-port sightings), which the existing Dependencies panel does
  not yet aggregate for display — the panel's UI surfacing of these new
  fields is explicitly deferred to a later pass, not part of this decision.
- **Negative / cost accepted:** "last known outcome wins per cycle" is a
  judgment call, not a derived fact. A dependency that fails once and
  recovers within the same scan window reports as succeeding; the reverse
  is also true. Bucketed, time-ordered evidence (P27 phase 4, "deferred")
  would resolve this properly; this phase does not attempt it.
- **Revisit if:** outcome vocabulary coverage turns out too low in practice
  to be useful (the fix would be a per-integration/per-logger normalizer
  configuration, a materially bigger feature than this phase); or if P24's
  actual policy-generation needs surface a requirement this phase didn't
  anticipate (e.g. protocol, not just port — already scoped separately as
  P27 phase 7).
