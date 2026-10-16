# 0044 – Dependency chat history + cross-session drift assessment

## Status

Proposed   ·   (date: 2026-10-16)

## Context

The Dependencies panel's docked chat (`DependencyChatPanel.tsx`, ROADMAP
P29) deliberately ships without a session-list sidebar — its own comment
says so explicitly: "without a session-list sidebar: this is a single
running conversation for the current view." The ask: (1) surface recent
conversation history in that panel, and (2) when a new question comes in,
look at *past* conversations that assessed something similar and flag
what's changed since — scoped, per the request, by "a specific query"
across every user, not just the one asking now.

**What already exists, not assumed:**

- Session listing is **not a gap** — `GET /api/chat/sessions` ("20 most
  recently active sessions") and a full list-sidebar pattern already ship
  in `ChatPanel.tsx` (the standalone Investigate/Chat page).
  `DependencyChatPanel`'s own docked rail just never adopted it, by
  original design choice, not oversight.
- **There is no per-user identity anywhere in this product.**
  `chat_sessions` has no `user_id` column; there is no login, no `users`
  table — only tenant/cluster-scoped collector-token auth. Confirmed by
  reading the schema and grepping the backend, not assumed. "The user's
  own history" as literally stated isn't buildable today without a
  separate, much larger identity project.
- **Confirmed with the user**: history is tenant-wide, not per-user —
  the existing session list's own scope, unchanged. This also means "across
  all users" for the drift-assessment half needs no extra scoping work:
  nothing today isolates by user, so tenant-wide pooling is already the
  default, not something to build.
- **Semantic memory (P8, `incident_embeddings`) is explicitly NOT a fit
  to extend directly** — its own doc states the scope up front: "a
  few-shot cache for ONE skill... not a conversation memory... chat is
  never embedded." The *mechanism* (Voyage embed → pgvector cosine search
  in Postgres, `get_similar_incidents`' retrieval shape) is the right
  precedent to repeat, not a table to repurpose — P8's own narrow-scope-
  per-table discipline argues for a sibling table, not widening that one.
- **The deterministic Tier-1 routes already store exactly the structured
  data a diff needs, for free.** `dependencies`, `label_scoped_dependencies`
  (ADR 0043), and `cold_services` all attach structured `details`
  (`service_graph` edges, `services`/`out_of_scope_services`) to the chat
  message they answer, persisted verbatim in `chat_sessions.messages`
  (JSONB). A later question resolving to the *same* route with the *same*
  scope has a ready-made "before" snapshot sitting in storage already —
  no new capture, no new table, just a read and a comparison.

## Decision

**1. History: reuse the existing session list, tenant-wide, unchanged
scope.** `DependencyChatPanel` gains the same list-sidebar pattern
`ChatPanel.tsx` already has, calling the same `GET /api/chat/sessions`
endpoint, filtered client-side to sessions matching the panel's own
`namespace` (optionally `focus`) — not a new backend capability, a UI
pattern reuse. No per-user split; building real identity for this is
explicitly rejected as disproportionate (ADR amendment candidate if that
ever changes for unrelated reasons).

**2. Drift assessment splits into two tiers by how the earlier
conversation was answered — never one mechanism pretending to cover both.**

- **Tier A — deterministic diff, zero LLM cost, for the Tier-1 routes**
  (`dependencies`, `label_scoped_dependencies`, `cold_services`). When a
  new question resolves to one of these routes, before composing the
  fresh deterministic answer, look up the most recent *past* session
  (tenant-wide) whose last answer on this same route had the same scope
  key (namespace+focus for `dependencies`; label_key+label_value for
  `label_scoped_dependencies`; namespace for `cold_services`). Diff the
  stored structured `details` against the freshly-fetched one: edges
  added/removed, `evidence_count` deltas past a noise threshold, a
  service that was cold and isn't (or vice versa). Appended as a
  **deterministic finding**, same "a model only adds latency and a
  chance of misreporting it" posture every Tier-1 route in this codebase
  already states for itself — this is a straight structural comparison
  of two JSON blobs already sitting in Postgres, not a judgment call.
- **Tier B — semantic retrieval + LLM-narrated comparison, for
  free-form questions.** A new `chat_query_embeddings` table, one row
  per *resolved* (non-Tier-1) chat answer: the question text, its Voyage
  embedding, a short summary of what was answered, `namespace`/`service`
  when known, `created_at`. Same write-path shape as `incident_embeddings`
  (`POST agent:/embed` → store), gated the same way (best-effort, never
  blocking the answer). On a new free-form question, embed it and search
  for a close match (cosine similarity, same mechanism
  `get_similar_incidents`/`/api/incidents/similar` already proves live)
  — if found, both the past summary and the fresh tool results are
  handed to the model with an explicit instruction to note what differs,
  framed to the reader as an **observation, not a guarantee** (matching
  `get_similar_incidents`' own "past incidents similar to..." framing,
  not asserted as fact).

**3. Both tiers are read-only additions to the existing answer, not a new
chat turn.** Neither tier generates a second message or a separate LLM
call on its own — Tier A appends a deterministic finding to the answer
already being composed; Tier B's retrieval happens before the existing
model call, feeding its context, not after it.

## Consequences

- **Positive:** Tier A costs nothing extra (one more read of already-
  stored JSONB, one structural diff) and is the highest-confidence half
  of the ask — exactly the kind of comparison this codebase already
  trusts code, not a model, to get right.
- **Positive:** Tier B reuses a proven mechanism end-to-end
  (`get_similar_incidents`' embed-then-cosine-search shape has been live
  since P8) rather than inventing a new retrieval path.
- **Positive:** no new identity system, no new auth — the single most
  expensive thing this request could have implied is explicitly out of
  scope, confirmed with the user rather than assumed.
- **Negative / cost accepted:** Tier A's "same scope" matching is
  deliberately narrow (exact namespace+focus, or exact label key=value)
  — two conversations about *related but not identical* scopes (e.g.
  `payment-api` vs. `payment` in the same namespace) will not be diffed
  against each other. Widening this is a tunable threshold, not a
  redesign, if it proves too narrow in practice.
- **Negative / cost accepted:** Tier B's "what changed" is prose,
  model-generated, and can be wrong or vague — explicitly a soft
  observation layer, not a source of truth, same caveat this codebase
  already applies everywhere an LLM narrates over evidence rather than
  the evidence speaking for itself.
- **Negative / cost accepted:** tenant-wide history (Decision #1) means
  two people's conversations are visible to each other on a shared
  tenant — acceptable for this product's current single-shared-ops-team
  reality (confirmed with the user), revisit if a tenant ever needs
  per-operator isolation for a real reason (compliance, multi-team
  tenants), which would be a materially larger, separate project.
- **Revisit if:** per-user identity gets built for an unrelated reason
  (e.g. audit/compliance) — at that point Decision #1's tenant-wide
  default should be reconsidered, not left stale by default.
