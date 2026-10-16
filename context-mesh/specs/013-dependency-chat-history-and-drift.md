# 013 – Dependency chat history + drift assessment

## Goal

Let a reader using the Dependencies panel's docked chat see recent
conversations (tenant-wide), and have a new question automatically flag
what's changed since a relevant past conversation — deterministically for
the mined-graph routes, as a soft observation for free-form questions.

## Depends on

- Policies: none of the four core "brain" policies apply directly — this
  is a chat-UX/retrieval feature on top of existing storage, not a
  storage-strategy, pod-formation, refinement-loop, or correlation
  decision.
- Other specs: [012](012-label-scoped-dependency-view.md) (the
  `label_scoped_dependencies` route this diffs against).
- ADR: [0044](../decisions/0044-dependency-chat-history-and-drift-assessment.md).

## Context / constraints

- Must work with: `DependencyChatPanel.tsx`, the existing
  `GET /api/chat/sessions`/`ChatPanel.tsx` list pattern, the three
  Tier-1 routes' existing `details` shapes (`dependencies`,
  `label_scoped_dependencies`, `cold_services`), the P8 Voyage/pgvector
  embedding mechanism.
- **Non-goals:**
  - Per-user identity or history isolation (ADR 0044 Decision #1 —
    confirmed out of scope).
  - A general conversation-memory system for all of chat — scoped to the
    Dependencies panel's three Tier-1 routes (Tier A) and its free-form
    questions (Tier B) only, not `ChatPanel.tsx`'s own general-purpose
    conversations.
  - Retroactively diffing against conversations from before this ships —
    Tier A/B both need a *prior* stored answer to compare against; the
    first question of a kind has nothing to diff.

## Behavior

- **Given** at least one past session exists for this namespace **when**
  the docked chat panel loads **then** a collapsible history list shows
  recent sessions scoped to this namespace (optionally further to
  `focus`, when one is set), reusing `ChatPanel.tsx`'s existing list
  pattern — clicking one resumes it (same `initialSessionId` mechanism
  `StandaloneDependencyChat.tsx` already uses).
- **Given** a past session answered "what does payment-api depend on"
  for this same namespace+focus **when** a new question resolves to the
  `dependencies` route for the same namespace+focus **then** the answer
  includes a deterministic diff against that past session's stored
  `service_graph`: edges added/removed since, and any `evidence_count`
  change past a noise threshold — zero additional LLM calls.
- **Given** a past session resolved `label_scoped_dependencies` for
  `apm-id=APM123456` **when** a new question resolves the same
  `label_key`/`label_value` **then** the same diff applies to the
  label-scoped service/edge sets (ADR 0043's `services`/
  `out_of_scope_services`).
- **Given** no past session matches the new question's exact scope
  **when** the Tier-1 route answers **then** no diff section appears —
  silence, not a fabricated "nothing changed."
- **Given** a free-form question semantically similar to a past resolved
  question exists **when** the new question is asked **then** the past
  summary is retrieved (cosine similarity) and handed to the model as
  context, with the answer explicitly framing any noted difference as an
  observation ("a similar question was asked on <date>; note: ..."), not
  an assertion.
- **Given** no similar past question exists, or embeddings are
  unconfigured (no `VOYAGE_API_KEY`, same degrade-gracefully posture P8
  already has) **when** a free-form question is asked **then** it
  answers exactly as it does today — this is additive context, never a
  blocking dependency.

## Interfaces

**Frontend** (`DependencyChatPanel.tsx`):

```tsx
// New: a collapsible list above (or beside) the thread, reusing
// ChatPanel.tsx's existing <SessionList> shape/component rather than a
// parallel implementation. Filters the existing GET /api/chat/sessions
// response client-side to this panel's namespace (+ focus, if set) —
// no new backend query needed for the list itself.
```

**Tier A — deterministic diff** (Go, `internal/api/handlers.go` or a new
`dependency_chat_diff.go`):

```go
// DiffTier1Answer looks up the most recent past session (tenant-wide,
// via chatStore) whose last message on this route+scope key matches,
// and returns a structured diff or nil (no prior match). Called from
// answerDependencies (ADR 0043's shared bypass) before composing the
// response, for all three Tier-1 routes it covers.
func DiffTier1Answer(ctx context.Context, tenantID, route, scopeKey string, fresh ServiceGraphDetails) (*GraphDiff, error)

type GraphDiff struct {
    PriorSessionID string    `json:"prior_session_id"`
    PriorAskedAt    time.Time `json:"prior_asked_at"`
    EdgesAdded      []ServiceDependency `json:"edges_added"`
    EdgesRemoved    []ServiceDependency `json:"edges_removed"`
    EvidenceDeltas  []EvidenceDelta     `json:"evidence_deltas"` // past noise threshold only
}
```

`scopeKey` is route-specific: `"{namespace}|{focus}"` for `dependencies`,
`"{label_key}={label_value}"` for `label_scoped_dependencies`,
`"{namespace}"` for `cold_services`.

**Tier B — semantic retrieval** (Python, new `k8fy/chat_memory.py`
mirroring `_get_similar_incidents`'s shape):

```python
async def embed_and_store_chat_query(
    backend_url: str, question: str, summary: str, namespace: str, service: str,
) -> None:
    """Best-effort, never blocks the answer — same gate shape as
    embedAndStoreIncident (handlers.go): only for resolved, non-Tier-1
    answers."""

async def find_similar_chat_query(
    backend_url: str, question: str, namespace: str, limit: int = 1,
) -> List[Dict[str, Any]]:
    """Same embed-then-GET-with-vec-param shape as _get_similar_incidents
    / /api/incidents/similar."""
```

**Storage** (Go, `postgres.go`):

```sql
-- Sibling to incident_embeddings, not a widening of it (P8's own
-- narrow-scope-per-table discipline) -- same conditional `embedding`
-- column pattern (absent when pgvector isn't installed).
CREATE TABLE IF NOT EXISTS chat_query_embeddings (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    namespace  TEXT, service TEXT,
    question   TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
-- embedding vector(512) added conditionally, same DO $$ ... EXCEPTION
-- block incident_embeddings already uses.
```

New endpoint, mirroring `/api/incidents/similar`:
`GET /api/chat-queries/similar?namespace=&vec=&limit=`.

## Open questions

- [ ] Evidence-count noise threshold for Tier A (ignore a delta under
      what %, or under what absolute count, so a diff doesn't fire on
      routine evidence accumulation)? Needs a look at real evidence_count
      growth rates, not a number to guess at now.
- [ ] Does Tier A's "most recent past session" lookup need a staleness
      cutoff (e.g. don't diff against something from 6 months ago), or is
      "always the most recent, however old" the right default? Leaning
      toward no cutoff for v1 — an old comparison is still informative,
      and the answer already states `PriorAskedAt`.
- [ ] Should the history list (Decision #1) show ALL tenant sessions
      filtered to this namespace, or literally reuse
      `GET /api/chat/sessions`'s unfiltered top-20 and filter client-side
      (meaning a namespace with low traffic could show zero history even
      though older sessions exist outside the top 20)? Leaning toward a
      namespace-filtering query param on the existing endpoint instead of
      client-side filtering, to avoid that silent gap — worth resolving
      before implementation, not after.

## Acceptance criteria

- [ ] The docked chat panel's history list shows recent sessions for the
      current namespace, resuming one on click via the existing
      `initialSessionId` mechanism.
- [ ] A `dependencies` question with a matching-scope prior session
      produces a non-empty `GraphDiff` when edges genuinely changed, and
      no diff section when nothing did.
- [ ] The same holds for `label_scoped_dependencies` (scoped by
      label_key=label_value) and `cold_services` (scoped by namespace).
- [ ] Tier A adds zero LLM calls, verified the same way P32's own
      zero-LLM-call criteria were — by code-path inspection (no client
      call in the diff path) or an explicit mock-assertion test.
- [ ] A free-form question similar to a past one surfaces that past
      summary to the model, explicitly framed as an observation in the
      resulting answer text, never asserted as fact.
- [ ] Embeddings unconfigured (no Voyage key) degrades Tier B to "answer
      as today," never an error or a blocked response.
