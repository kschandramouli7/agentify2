# 012 – Label-scoped dependency view (ROADMAP P32)

## Goal

Let a user key in a pod-label value (an APM number today; any
operator-tracked label key tomorrow) in chat and see the dependency flow
for every service carrying it — including upstream/downstream services
that don't carry that same tag.

## Depends on

- Policies: [correlation](../policies/correlation.md) (the union-over-
  either-endpoint fan-out shape, adapted from pod-level fan-out to a
  service-set lookup).
- Other specs: none directly; builds on the storage/mining machinery
  behind [ADR 0029](../decisions/0029-dependency-mining.md)'s
  `_service_for_labels` and the fleet's existing `service_dependencies`.
- ADR: [0043](../decisions/0043-generic-label-based-service-scoping.md)
  (the schema/API/Tier-1 decisions this spec implements).

## Context / constraints

- Must work with: Discovery's live scan, the Glue dependency miner
  (`dependency_miner.py`), and the Splunk dependency miner
  (`splunk_dependency_miner.py`) — all three already resolve
  `from_service` via pod labels and are the only places this data exists.
- Must work with: the existing `DependencyFlow` frontend component — no
  new visualization, a new *scoped* data path into the same one.
- **Non-goals:**
  - Ad-hoc Splunk-index-driven dependency mining (a different mechanism,
    deferred to its own ADR — see ADR 0043's Context section).
  - Per-tenant `TRACKED_LABEL_KEYS` configuration (v1 is deployment-wide).
  - Fuzzy/partial label-value matching (exact match only).
  - Pod-level label history (service granularity only; see ADR 0043
    Consequences on canary/mixed-value rollouts).

## Behavior

- **Given** `TRACKED_LABEL_KEYS` includes `apm-id`, and pods in `payments`
  carry `apm-id: APM123456` **when** a user types "show me the dependency
  flow for APM123456" **then** the chat layer resolves this via the
  alias-table + regex fast path (no LLM call for extraction) to
  `label_key=apm-id, label_value=APM123456`, and the response renders a
  `DependencyFlow` scoped to every service carrying that tag plus every
  edge touching them.
- **Given** the same tagged service set **when** one tagged service calls
  a downstream service with no `apm-id` label at all (or a different one)
  **then** that downstream service and the edge to it still appear in the
  rendered graph, visually distinguished as out-of-scope (not hidden, not
  merged indistinguishably with in-scope nodes).
- **Given** a user types `team=checkout` explicitly **when** `team` is in
  `TRACKED_LABEL_KEYS` **then** the same fast path resolves it with zero
  LLM calls, identical code path to the APM case — nothing in the tool or
  endpoint is APM-specific.
- **Given** a user asks conversationally, e.g. "what's connected to the
  checkout team's services" **when** no explicit syntax or alias match
  fires **then** the chat agent's existing tool-use turn extracts
  `label_key=team, label_value=checkout` as part of the same call it
  already makes to select a tool — not an additional LLM call.
- **Given** a user names a label key not in `TRACKED_LABEL_KEYS` **when**
  the lookup runs **then** the tool/endpoint returns a clear "not a
  tracked label; tracked keys are: ..." result (via the existing
  tool-result round-trip, not a new LLM call) rather than a silent empty
  graph.
- **Given** a label key/value matches zero services **when** the lookup
  runs **then** the response says so plainly — distinguishable from "not
  tracked" (previous case) and from a graph-render error.

## Interfaces

**Settings** (`src/agent/config/settings.py`, same `_list_env` pattern as
`cross_cluster_ingress_kinds`):

```python
tracked_label_keys: List[str]  # env TRACKED_LABEL_KEYS, comma-separated
label_key_aliases: Dict[str, str]  # e.g. {"apm": "apm-id"} — chat-layer shorthand only
```

**Storage** (Go, `postgres.go`) — see ADR 0043 Decision #2 for the full
`CREATE TABLE service_labels` statement.

**Capture** (Python, each miner — Discovery live scan, `dependency_miner.py`,
`splunk_dependency_miner.py`), symmetric to the existing `_push_edge`:

```python
async def _push_service_labels(
    backend_url: str, cluster_id: str, namespace: str, service: str,
    labels: Dict[str, str], tracked_keys: Set[str],
) -> None:
    """POST /api/service-labels for every (key, value) in `labels` whose
    key is in `tracked_keys`. Best-effort, log-and-swallow on failure,
    same convention as _push_edge."""
```

**Read endpoints** (Go, `handlers.go`):

```
GET /api/service-labels?label_key=X&label_value=Y
  -> 200 [{cluster_id, namespace, service}, ...]
  -> 400 {"error": "label_key 'X' is not tracked; tracked keys: [...]"}

GET /api/service-dependencies-for-services?services=<json-encoded [{cluster_id,namespace,service},...]>
  -> 200 [ServiceDependency, ...]  # same shape ListServiceDependencies already returns,
                                    # union over either endpoint matching the given set
```

**Chat tool** (Python, registered in `k8fy/skills/router.py`):

```python
def lookup_services_by_label(label_key: str, label_value: str) -> DependencyGraphResult:
    """Generic — never APM-specific. Validates label_key against
    settings.tracked_label_keys before calling the Hub."""
```

**Tier-1 fast-path recognizer** (Python, checked before any tool-use
turn, same place other Tier-1 intents are checked per ADR 0006):

```python
def try_fast_path_label_lookup(message: str) -> Optional[Tuple[str, str]]:
    """Returns (label_key, label_value) if `message` matches either:
    1. explicit `key=value` syntax, or
    2. an alias-table key (settings.label_key_aliases) whose configured
       regex matches a token in the message.
    Returns None otherwise, falling through to normal tool-use."""
```

**Frontend** (`DependencyFlow.tsx`): nodes in the matched service set get
an "in scope" visual treatment; nodes reached only via an edge (not
themselves in the matched set) get a distinct "out of scope" treatment —
reusing the badge/tooltip pattern already established for cross-cluster
platform labeling (ADR 0037's amendment), not a new visual language.

## Open questions

- [ ] Does `label_key_aliases` live in `settings.py` (code-adjacent,
      requires a deploy to change) or in a small Hub-side config table
      (editable without a redeploy)? ADR 0043 leans toward the latter if
      the list grows past a handful of entries, but doesn't commit v1 to
      building that admin surface.
- [ ] Should `GET /api/service-dependencies-for-services` be a new
      endpoint, or should `ListServiceDependencies` grow an optional
      service-set filter parameter instead? Leaning toward a new endpoint
      to avoid overloading the existing namespace-scoped one's semantics,
      but worth a second look once the Go handler is actually written.
- [ ] Exact visual treatment for "out of scope" nodes — a badge (like the
      platform label) or a muted/greyed style? Needs a quick design pass,
      not a blocking decision for the backend work.

## Acceptance criteria

- [ ] A pod label whose key is in `TRACKED_LABEL_KEYS` is captured into
      `service_labels` by all three miners (live, Glue, Splunk) within
      one of their normal mining cycles — no new scan.
- [ ] A pod label whose key is NOT in `TRACKED_LABEL_KEYS` is never
      written to `service_labels`.
- [ ] `GET /api/service-labels` returns 400 for an untracked key, 200
      with an empty list for a tracked key with no matches, and 200 with
      the matching service set otherwise.
- [ ] The dependency-set query returns edges where either endpoint is in
      the matched service set, including edges to/from services with no
      matching label at all.
- [ ] Explicit `key=value` chat input and alias-table-matched input (e.g.
      `APM123456`) both resolve with zero LLM calls, verified by
      asserting no `anthropic` API call is made in the relevant test.
- [ ] Free-form conversational phrasing resolves via the existing
      tool-use turn with no additional LLM call beyond what a normal
      tool-backed chat turn already makes.
- [ ] `DependencyFlow` renders in-scope and out-of-scope nodes with
      visibly distinct treatment, for a fixture where both exist.
