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

- **Given** at least one session exists for this tenant **when** the
  docked chat panel loads **then** a collapsible history list shows the
  tenant's 20 most recently active sessions, exactly as
  `GET /api/chat/sessions` already returns them — **not filtered to the
  current namespace** (a deliberate simplification: this list is a
  resume-a-conversation convenience, not the mechanism that needs to be
  precise — see the next three behaviors for where precision actually
  matters) — reusing `ChatPanel.tsx`'s existing list pattern; clicking
  one resumes it (same `initialSessionId` mechanism
  `StandaloneDependencyChat.tsx` already uses).
- **Given** a past session answered "what does payment-api depend on"
  for this same namespace+focus **when** a new question resolves to the
  `dependencies` route for the same namespace+focus **then** the answer
  includes a deterministic diff against that past session's stored
  `service_graph`: edges added (each with its real `first_seen` date)
  and edges gone quiet since (each with its real `last_seen` date — see
  Interfaces for "gone quiet"'s precise definition). No `evidence_count`
  magnitude comparison in v1. Zero additional LLM calls.
- **Given** a past session resolved `label_scoped_dependencies` for
  `apm-id=APM123456` **when** a new question resolves the same
  `label_key`/`label_value` **then** the same diff applies to the
  label-scoped service/edge sets (ADR 0043's `services`/
  `out_of_scope_services`).
- **Given** no past session matches the new question's exact scope key
  (same route + same namespace/focus, or same label key=value) **when**
  the Tier-1 route answers **then** no diff section appears — silence,
  not a fabricated "nothing changed." This match is deliberately narrow
  — a conversation about a different, even related, scope is never used.
- **Given** the matched prior session is old (no hard cutoff — see ADR
  0044 Decision/Consequences) **when** the diff is shown **then** the
  comparison window itself (`PriorAskedAt`) is always stated plainly, and
  phrasing softens past a rough age (e.g. "treat as directional" beyond
  ~30 days) rather than asserting an old comparison with the same
  confidence as a same-day one.
- **Given** a free-form question semantically similar to a past resolved
  question **in the same namespace AND the same service/focus** exists
  **when** the new question is asked **then** the past summary is
  retrieved (cosine similarity, scoped by namespace + service, not
  namespace alone) and handed to the model as context, with the answer
  explicitly framing any noted difference as an observation ("a similar
  question was asked on <date>; note: ..."), not an assertion. A
  similar-sounding question about a *different* service never qualifies,
  however close the text match.
- **Given** no similar past question exists, or embeddings are
  unconfigured (no `VOYAGE_API_KEY`, same degrade-gracefully posture P8
  already has) **when** a free-form question is asked **then** it
  answers exactly as it does today — this is additive context, never a
  blocking dependency.

## Interfaces

**Frontend** (`DependencyChatPanel.tsx`, implemented as built):

```tsx
// A collapsible list ("≡" toggle next to the existing "⤢" expand
// button), rendered between the header and the thread rather than a
// permanent sidebar — the docked rail is too narrow for
// ChatPanel.tsx's full <SessionList/>, so this reuses its underlying
// CSS classes (.chat-session-list/-item/-title/-meta) and its exported
// relTime() helper, not the sidebar component itself. Calls
// listChatSessions() -> GET /api/chat/sessions UNFILTERED — the same
// tenant-wide top-20 ChatPanel.tsx shows, sharing its "chatSessions"
// query-cache key so the two views don't double-fetch. Fetched lazily
// (enabled: showHistory) since it's tenant-wide and most panel opens
// never need it. Clicking an entry sets sessionId directly (same state
// the "resume via initialSessionId" path already uses) — picking a
// session from a different namespace than the panel's own props is
// allowed, per Decision #1: this list is a resume convenience, not the
// mechanism that needs to be precise.
```

**Tier A — deterministic diff** (Go, `internal/api/handlers.go` or a new
`dependency_chat_diff.go`):

```go
// DiffTier1Answer looks up the most recent past session (tenant-wide,
// via chatStore) whose last message on this route+scope key EXACTLY
// matches, and returns a structured diff or nil (no prior match).
// Called from answerDependencies (ADR 0043's shared bypass) before
// composing the response, for all three Tier-1 routes it covers.
func DiffTier1Answer(ctx context.Context, tenantID, route, scopeKey string, fresh ServiceGraphDetails) (*GraphDiff, error)

type GraphDiff struct {
    PriorSessionID string      `json:"prior_session_id"`
    PriorAskedAt   time.Time   `json:"prior_asked_at"`
    EdgesAdded     []EdgeDiffEntry `json:"edges_added"`
    EdgesGoneQuiet []EdgeDiffEntry `json:"edges_gone_quiet"`
}

// EdgeDiffEntry carries the edge PLUS the one timestamp that actually
// explains the finding -- "added" reports when it first appeared,
// "gone quiet" reports the last time any evidence for it arrived at
// all. Never just the edge with a blanket PriorAskedAt stamped on it.
type EdgeDiffEntry struct {
    ServiceDependency
    ChangedAt time.Time `json:"changed_at"` // first_seen for added, last_seen for gone-quiet
}
```

"Gone quiet" (`EdgesGoneQuiet`) is defined specifically for this cross-
session comparison, NOT the live diagram's 15-minute real-time
staleness marker (DependencyFlow.tsx's `isStale`) — a different time
scale for a different purpose. An edge qualifies here when its
`last_seen` at fetch time equals its `last_seen` as stored in the prior
session's snapshot: no new evidence arrived anywhere in the gap between
the two conversations, however long that gap was.

`scopeKey` is route-specific: `"{namespace}|{focus}"` for `dependencies`,
`"{label_key}={label_value}"` for `label_scoped_dependencies`,
`"{namespace}"` for `cold_services`. No evidence_count magnitude
comparison in v1 (ADR 0044 Decision #2/Consequences) — structural change
only.

**Tier B — semantic retrieval** (Go, `internal/api/handlers.go` —
**revised from the original draft**, which sketched this as a new
Python module + a new public HTTP endpoint. Neither was built that way:
`embedAndStoreIncident`'s own embedding call already lives Go-side, not
Python — it POSTs straight to the agent's `/embed` endpoint — so Tier B
follows that same, already-established convention instead of
introducing a second, inconsistent one):

```go
// embedText is the shared helper both embedAndStoreIncident and Tier B
// use to call the agent's /embed endpoint. Extracted, not duplicated.
func (h *Handler) embedText(text string) []float32

// Read side, in HandleSendChatMessage, BEFORE the agent/model call
// (Decision #3 — retrieval feeds the call, it isn't a tool the model
// decides whether to invoke): embed this turn's own question, then
// look up a close match scoped by namespace AND service — never
// namespace alone, so a semantically-close question about a DIFFERENT
// service in the same namespace never qualifies. A match is handed to
// the agent as context["similar_past_query"] = {question, summary}.
type similarChatFinder interface {
    FindSimilarChatQueries(ctx context.Context, namespace, service string, queryVec []float32, limit int) ([]pgstore.ChatQueryEmbedding, error)
}

// Write side, same function, only for a model-answered (non-Tier-1)
// turn: redact the question and the answer, embed the question, store
// both via UpsertChatQueryEmbedding. Best-effort, logged on failure,
// never blocks the response — same posture embedAndStoreIncident
// already has for P8. Both sides are a no-op (not an error) when
// h.orch is nil or the relational backend isn't configured.
type chatEmbStorer interface {
    UpsertChatQueryEmbedding(ctx context.Context, e pgstore.ChatQueryEmbedding) error
}
```

No new public HTTP endpoint exists for this (the originally-sketched
`GET /api/chat-queries/similar` was never built) — the lookup happens
internally inside `HandleSendChatMessage` itself, the same way
`embedAndStoreIncident`'s own embed call is internal to its caller
rather than a separately-exposed route.

On the Python side (`k8fy/agent.py`'s `reason_chat`), no new module or
function was added either — `context.get("similar_past_query")` is read
directly where the model's system prompt is assembled, and turned into
one explicit instruction block: state what was found before, then tell
the model to note any difference as an *observation*, never assert it
as settled fact. Covered by
`test_model_path_includes_a_similar_past_query_as_an_observation_not_fact`
and `test_model_path_ignores_a_similar_past_query_with_no_summary` in
`test_chat_service_graph.py`.

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

RLS: `ENABLE`/`FORCE` + a `tenant_isolation` policy from this table's
first migration — the newer-tables convention (`security_findings`,
`service_labels`), not the older `tenant_id`-column-but-no-RLS gap
`incident_embeddings`/`chat_sessions` still have. No new public HTTP
endpoint — see the Tier B interface above for why.

## Open questions

All three original open questions are resolved (confirmed with the user,
2026-10-16):

- [x] **Evidence-count noise threshold for Tier A?** Resolved: not built
      in v1 at all. Tier A reports structural change only
      (added/gone-quiet edges); magnitude comparison is deferred until
      real production evidence_count growth-rate data exists to
      calibrate a threshold against, rather than guessing one now and
      risking exactly the noise problem this feature exists to avoid.
- [x] **Staleness cutoff on which past session to diff against?**
      Resolved: no hard cutoff — an old comparison stays informative for
      infrequently-asked namespaces, which is arguably where this
      feature matters most. Mitigated by always surfacing `PriorAskedAt`
      plus each finding's own `ChangedAt` timestamp, and softening the
      framing (directional, not precise) past a rough age, rather than
      silently suppressing old comparisons.
- [x] **History list: server-side filter vs. client-side filter?**
      Resolved differently than either original option: **no filtering
      at all.** The list reuses `GET /api/chat/sessions` completely
      unchanged (tenant-wide top-20) — filtering was the wrong fix for
      the wrong layer. The precision this question was really protecting
      against belongs to the diff/retrieval matching (Tier A's exact
      scope-key match, Tier B's namespace+service-scoped similarity
      search), which was already narrow by design and is now made
      explicit for Tier B too (service, not just namespace).

## Acceptance criteria

- [x] The docked chat panel's history list shows the tenant's 20 most
      recently active sessions, unfiltered, resuming one on click via
      the existing `initialSessionId` mechanism (sessionId state, same
      effect).
- [x] A `dependencies` question with a matching-scope prior session
      produces a non-empty `GraphDiff` when edges genuinely changed —
      each entry carrying its own `ChangedAt` (not a blanket
      `PriorAskedAt`) — and no diff section when nothing did.
- [x] The same holds for `label_scoped_dependencies` (scoped by
      label_key=label_value) and `cold_services` (scoped by namespace).
- [x] A session with a DIFFERENT scope (different focus, different
      label value) is never used as the diff baseline, however recent
      (`FindPriorTier1Session`'s exact scope-key match).
- [x] Tier A adds zero LLM calls — the diff path never calls
      `agentClient`, confirmed by code-path inspection and the handler
      tests in `chat_session_drift_test.go`.
- [x] A free-form question similar to a past one, for the SAME service,
      surfaces that past summary to the model, explicitly framed as an
      observation in the resulting answer text, never asserted as fact.
- [x] A free-form question textually similar to a past one but for a
      DIFFERENT service never surfaces that past summary (enforced by
      `FindSimilarChatQueries`'s namespace+service scoping, not a
      post-hoc filter).
- [x] Embeddings unconfigured (no Voyage key) degrades Tier B to "answer
      as today," never an error or a blocked response — same
      `embedText`/`FindSimilarChatQueries` nil/error-tolerant paths
      `embedAndStoreIncident` already relies on for P8.
