# 0036 – Edge provenance capture (ROADMAP P27 phase 4)

## Status

Accepted   ·   (date: 2026-10-08)

## Context

A mined edge today can't say which miner(s) found it — Discovery's live
scanner, the Glue miner, and the opportunistic agent-skill miner all write
to the same `service_dependencies` row, currently indistinguishable at read
time — or whether it matched a qualified FQDN
(`service.namespace.svc.cluster.local`) or a bare name (`//service` /
`service:port`), a materially weaker form of evidence (a bare match could
coincidentally collide with an unrelated local identifier; a qualified one
essentially can't). ROADMAP P27 phase 4 names both as "provenance," the
third of its four sub-items to ship, after path/operation class (ADR 0034)
and caller cardinality (ADR 0035); bucketed evidence remains unbuilt.

## Decision

Two genuinely different kinds of fact get two different storage shapes —
this was a real fork, put to the user before implementation, and both
halves were confirmed explicitly rather than assumed:

**Which miner(s) confirmed an edge is a SET relationship** — live, Glue,
and the agent skill can all independently confirm the same edge, and that
agreement is itself a useful confidence signal a single "last writer wins"
column would discard. **Extends `service_dependency_callers`** (ADR 0035)
rather than a new table: a `source TEXT NOT NULL DEFAULT ''` column,
widening its primary key. Each caller-pod sighting already comes from
exactly one producer at push time, so this is additive to a table that
already exists, already has an RLS policy, and already has a retention
janitor — `COUNT(DISTINCT pod_name)` (caller cardinality) is completely
unaffected by widening the key, and the sources list comes from the same
table for free via `jsonb_agg(DISTINCT source)`.

**Qualified-vs-bare match strength is a per-observation classification,
the same shape outcome already is** — not a set, not a different edge.
**Two new counters on `service_dependencies` itself**:
`qualified_match_count`, `bare_match_count`, incremented via the same
`CASE WHEN ... THEN 1 ELSE 0 END` pattern the three existing outcome
counters already use. No row-splitting (unlike port/path, which define a
materially different edge) — this is evidence quality about the *same*
edge. Within one push cycle, when both forms are seen for the same
`(service, port, path)` key, **qualified wins** — a deliberately different
rule from outcome's "last line in the log wins," because match strength is
a property of the mining method, not a time-ordered state the way a health
outcome is.

### What was already true in the code before designing, not assumed

- `extract_service_calls`'s three match loops already structurally know
  their own match kind: `_HOSTNAME_RE` (qualified) vs `_URL_HOST_RE`/
  `_HOST_PORT_RE` (both bare forms) — this is a label on an existing
  branch, not new detection logic.
- All three producers already have a natural "which miner am I" identity
  at their own module scope — `discovery/main.py` (`"live"`),
  `dependency_miner.py` (`"glue"`), `service_topology.py`'s
  `mine_service_dependencies` (`"agent_skill"`) — passed through the same
  push call already carrying `caller_pod` (ADR 0035).
- `service_dependency_callers`'s existing upsert (inside
  `UpsertServiceDependency`'s own transaction) already takes the shape
  this extends: `source` joins its column list, its `ON CONFLICT` target,
  and the join `ListServiceDependencies` already does for
  `caller_pod_count`.

### Per-cycle dedup — match_kind joins the "strongest wins" rule

All three producers' per-cycle dedup dict (keyed on `(service, port,
path)`) tracks `(outcome, match_kind)` instead of just `outcome`. Outcome
keeps its existing "last non-null wins" semantics; `match_kind` is
**sticky once qualified is seen** — a later bare observation for the same
key in the same window never downgrades it. This surfaced a real bug while
testing: the Glue miner's cross-pod `pushed` dedup set (ADR 0035) was keyed
only on `(from, to, port, path)`, not pod — confirmed already correct for
its own purpose (evidence_count accumulates Hub-side), but the first
drafts of the new sticky-merge tests were themselves wrong for a related
reason: two log lines chosen to prove the merge rule produced two
*different* dedup keys, so the rule was never actually exercised. Fixed by
engineering colliding-key fixtures (same service/port/path, one qualified
form, one bare form) in both the agent and discovery test suites.

### Schema

```sql
ALTER TABLE service_dependencies
    ADD COLUMN IF NOT EXISTS qualified_match_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS bare_match_count INT NOT NULL DEFAULT 0;

ALTER TABLE service_dependency_callers
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT '';
-- Primary key widens to include source — same find-and-replace idempotent
-- migration pattern service_dependencies' own constraint migrations use
-- (ADR 0022/0031/0034): recognize both the old and new constraint name so
-- a restart after this migration never regresses it.
```

`source` follows the same "empty means not reported, never a phantom
value" rule `caller_pod` itself established in ADR 0035 — an empty source
is skipped by the same `if callerPod != ""` guard, never inserted as
`source = ''`.

### Read path

`ListServiceDependencies`'s existing `service_dependency_callers` join
gains `jsonb_agg(DISTINCT source) FILTER (WHERE source != '') AS sources`,
read into a Go `[]string` — following the `ServiceProfile.Ports`/
`portsJSON` precedent (`jsonb_agg` + `json.Unmarshal`), not
`pq.Array`/native Postgres arrays, consistent with every other
array-shaped column in this codebase. Chose **names** over a bare count:
the entire point of provenance is "which miners," not "how many" — a count
alone would throw away the one fact this item exists to capture.

### API surface

`serviceDependencyUpsertRequest` gains `MatchKind` (`json:"match_kind"`)
and `Source` (`json:"source"`), same explicit-never-omitted convention as
every prior optional field on this request. `UpsertServiceDependency`'s
interface and implementation both gain `matchKind, source string` trailing
parameters.

### UI

Passive only, folded into two existing cells rather than two new columns —
unlike port/path/caller-cardinality, which each introduced a genuinely new
fact and earned their own column, match strength and source are both
*metadata about evidence the table already shows*. Match strength
(`qualified_match_count`/`bare_match_count`) joins the Evidence cell's
existing tooltip; sources join the Callers cell's existing tooltip (`e.g.
"Confirmed by: live, glue."`). Both are silent when empty — an edge from
before this phase, or one with no caller-pod evidence at all — same "say
nothing until there's something to say" convention the path-coverage line
above them already follows.

## Consequences

- **Positive:** the mined graph can now distinguish "three independent
  miners agree" from "one producer's single observation," and "backed by a
  full qualified hostname" from "backed by a bare name that could be
  coincidental" — both previously invisible.
- **Positive:** `service_dependency_callers` absorbs a second kind of
  provenance fact without a second table, retention janitor, or RLS
  policy — ADR 0035's generalization work (the `retention.Janitor`
  name/metric parameterization) pays off again here by needing no further
  changes.
- **Negative / cost accepted:** `service_dependency_callers`'s primary key
  now carries six columns plus `pod_name` plus `source` — wider than ideal,
  but still a single composite key, and widening it (rather than a new
  table) was the explicit tradeoff accepted to keep caller cardinality's
  own `COUNT(DISTINCT pod_name)` untouched.
- **Negative / cost accepted:** `qualified_match_count`/`bare_match_count`
  are per-edge aggregates, not per-pod or per-source — a qualified match
  from one producer and a bare match from another on the same edge both
  land in the same two counters, indistinguishable from each other without
  cross-referencing `sources`. Acceptable because match strength is a
  property of the *extraction*, not of any one producer.
- **Revisit if:** P27 phase 4's last sub-item (bucketed evidence) wants
  match strength or source bucketed over time rather than as a cumulative
  total — the counters and the sources list are both all-time, with no
  windowing, same limitation `evidence_count` itself already has.
