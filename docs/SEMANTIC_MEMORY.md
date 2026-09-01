# Semantic Memory (P8)

How past incidents are embedded, stored and retrieved — and how to tell whether
it is actually working, which for most of its life it was not.

> **Scope, stated up front:** this is a **few-shot cache for one skill**. It
> indexes *conclusions* from Tier-2 `diagnose` — one line of
> `headline | likely_cause` — so a recurring incident gets diagnosed with past
> diagnoses in view. It is **not** a conversation memory and is not the substrate
> for [ROADMAP P19](../context-mesh/ROADMAP.md): chat is never embedded, and
> per-turn evidence is not in the index. P19 reads `traces` and Langfuse
> observations instead.

## Is Voyage AI just an embedding API?

Yes. Voyage stores nothing and searches nothing. One call, one purpose
(`app.py`'s `/embed`):

```python
client = voyageai.Client(api_key=settings.voyage_api_key)
result  = client.embed([request.text], model=model)   # voyage-3-lite, 512-dim
vec     = result.embeddings[0]
```

Text in, 512 floats out. **All storage and similarity search is ours, in
Postgres.**

## Storage

```sql
CREATE TABLE incident_embeddings (
    id         TEXT PRIMARY KEY,
    trace_id   TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    namespace  TEXT, service TEXT,
    summary    TEXT,            -- "headline | likely_cause", human-readable
    created_at TIMESTAMP
);
-- added ONLY when pgvector is present:
ALTER TABLE incident_embeddings ADD COLUMN embedding vector(512);
CREATE INDEX ... USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);
```

Two things to know about that DDL:

- **The `embedding` column is conditional.** It is added inside a
  `DO $$ … EXCEPTION WHEN OTHERS THEN NULL` block so the schema still
  initialises on embedded-postgres in CI, which has no pgvector. Without
  pgvector the column simply does not exist and the table is a plain incident
  log.
- **`trace_id` references `traces(id)` — the traces PRIMARY KEY**, not the
  correlation `traces.trace_id`. Those are different UUIDs. Writing the wrong
  one violates the foreign key on every insert (see Bug 3 below). The
  correlation id remains reachable by joining
  `traces.id = incident_embeddings.trace_id`.

## Write path

Only Tier-2 `diagnose`, asynchronously, never blocking the answer:

```
/api/query ──► diagnose answers ──► response returned to the caller
                    │
                    └─ go h.embedAndStoreIncident(...)     [handlers.go]
                           │  summary = details.headline + " | " + likely_cause
                           ▼
                    POST agent:/embed  ───► Voyage AI (voyage-3-lite)
                           │                 512 floats, ~$0.00002 per trace
                           ▼
                    INSERT incident_embeddings (summary, embedding, trace_id=rowID)
```

The gate is exactly:

```go
if tier == "tier2" && intent == "diagnose" && agentResp != nil { ... }
```

So **what is not embedded**: chat turns (`intent = "chat"`), every non-diagnose
intent, any diagnose answered by the Tier-1 fast path, cert renewal, remediation.
And not the conversation — only the one-line conclusion. Question, reasoning,
tool calls and evidence stay in `traces`, linked by `trace_id`.

## Read path

`get_similar_incidents` is a tool `DiagnoseSkill` may call:

```
embed(description) via Voyage        [tools.py — a second, independent call site]
        ▼
FindSimilarIncidents(namespace, service, queryVec, limit=3)
        ├─ queryVec present ─► ORDER BY embedding <-> $1::vector   (cosine)
        └─ queryVec empty   ─► ORDER BY created_at DESC             (recency)
        ▼
dedup by normalised headline ──► up to `limit` distinct incidents
```

**Deduplication is on read, not write.** There is no write-side dedup, so
diagnosing one problem repeatedly writes a near-identical row each time.
Observed 2026-09-01: four of five stored rows were the same `payment-worker`
Pending incident, so an undeduplicated top-3 would have returned three copies of
it and no other incident at all — the feature degrading with use.

`FindSimilarIncidents` over-fetches 4× (capped at 60) and collapses rows sharing
a normalised **headline**, since summaries are `headline | likely_cause` and the
cause wording varies between runs while the headline identifies the incident.
Read-side rather than write-side so storage stays an honest log: how often an
incident recurs is real information, it just should not consume every few-shot
slot.

## Every failure mode is silent — by design, and at a cost

| Failure | Behaviour |
|---|---|
| `VOYAGE_API_KEY` unset | `/embed` returns `available: false`; write skipped |
| Voyage errors or rate-limits | logged, skipped, query unaffected |
| pgvector absent | `embedding` column never created; search falls back to **recency** |
| Read-path embed fails | `queryVec` empty → **recency** |
| Insert fails | logged at `Warn` in a background goroutine, swallowed |

Individually correct — semantic memory must never break a diagnosis. Collectively
there is **no path by which you learn it isn't working**, which is why three
separate bugs coexisted undetected for the life of the feature.

## Why it never worked until 2026-09-01

Three independent bugs, stacked. Each was necessary to fix and none was
sufficient — worth knowing, because the same shape will recur.

**Bug 1 — the Anthropic API key was JSON-wrapped three deep.** Every Tier-2 call
returned `401 invalid x-api-key`, so no diagnose ever completed and nothing was
ever offered for embedding. See `docs/DEPLOYMENT.md` Step 4.

**Bug 2 — `service` was the namespace.** It was reverse-engineered as
`strings.TrimPrefix(src, "k8fy.live-state.")`, but that suffix is the *namespace*
partition (`ingester.go` `routeAndCreatePod`). Every row would have stored
`service = namespace`, and the recency fallback filters `WHERE service = $2` with
a real service name — matching nothing. Fixed by taking `service` from the
request context, where `namespace` was already read from.

**Bug 3 — the insert violated its own foreign key.** `embedAndStoreIncident`
passed the correlation `traceID` as `trace_id`, but the FK references
`traces(id)` = `rowID`. Every insert failed, logged at `Warn` and swallowed. The
test suite could not catch it: its fixture set `traces.id` and `traces.trace_id`
to the *same* value, satisfying the FK in tests while production passed two
different ones. Both are now seeded distinctly, with a subtest asserting the FK
rejects the correlation id and accepts the PK.

## Verifying it works

RDS is in the VPC, so CloudShell cannot reach it directly. Run the query from a
throwaway pod **inside the cluster**, pulling credentials from the existing
secret so no password appears on a command line:

```bash
kubectl run pgcheck -n agentify --rm -i --restart=Never --image=postgres:16-alpine \
  --overrides='{"spec":{"containers":[{"name":"pgcheck","image":"postgres:16-alpine",
    "command":["sh","-c","psql \"postgresql://$DB_USER:$DB_PASSWORD@$DB_HOST:$DB_PORT/$DB_NAME\" -c \"SELECT count(*) AS total, count(embedding) AS with_vector FROM incident_embeddings;\" -c \"SELECT namespace, service, left(summary,60) AS summary FROM incident_embeddings ORDER BY created_at DESC LIMIT 5;\""],
    "env":[
      {"name":"DB_HOST","valueFrom":{"secretKeyRef":{"name":"agentify-db-secret","key":"host"}}},
      {"name":"DB_PORT","valueFrom":{"secretKeyRef":{"name":"agentify-db-secret","key":"port"}}},
      {"name":"DB_NAME","valueFrom":{"secretKeyRef":{"name":"agentify-db-secret","key":"dbname"}}},
      {"name":"DB_USER","valueFrom":{"secretKeyRef":{"name":"agentify-db-secret","key":"username"}}},
      {"name":"DB_PASSWORD","valueFrom":{"secretKeyRef":{"name":"agentify-db-secret","key":"password"}}}
    ]}]}}'
```

Add `-c "SELECT extname FROM pg_extension WHERE extname='"'"'vector'"'"';"` to
check pgvector.

| Observation | Meaning |
|---|---|
| `total > 0`, `with_vector = total` | **Working.** Rows written, Voyage returning vectors |
| `total > 0`, `with_vector < total` | Voyage throttled (3 RPM without a payment method) — summaries stored, some vectors missing, those rows fall back to recency |
| `total > 0`, `with_vector = 0` | An incident log, not semantic memory — check pgvector |
| `total = 0` after a successful diagnose | Insert failing. Check the agent/backend logs for `incident embedding store failed` |
| `namespace = service` on every row | Bug 2 has regressed |
| `extname` returns no rows | pgvector absent — an RDS parameter-group / `CREATE EXTENSION` change, not code |

Known-good reference (2026-09-01, after all three fixes):

```
 total | with_vector
-------+-------------
     5 |           5

 namespace |    service     | summary
-----------+----------------+-------------------------------------------
 payments  | payment-worker | 🔴 payment-worker has no running replica …
```

## Configuration

| Variable | Where | Notes |
|---|---|---|
| `VOYAGE_API_KEY` | GitHub secret → `agentify-voyage-secret` | Unset = semantic memory disabled cleanly |
| `voyage_model` | `config/settings.py` | `voyage-3-lite`, 512-dim, ~$0.00002/trace. **Changing it changes the vector width** — `vector(512)` and every stored row would need migrating |
| pgvector | RDS extension | Required for similarity search; without it, recency only |

## References

- [ADR 0018](../context-mesh/decisions/0018-three-layer-memory-architecture.md) — three-layer memory architecture
- [ADR 0010](../context-mesh/decisions/0010-postgres-single-store.md) — Postgres as the single store
- [ROADMAP P8](../context-mesh/ROADMAP.md) — RAG + pgvector + semantic memory
- [docs/PROMPT_LIFECYCLE.md](PROMPT_LIFECYCLE.md) — the adjacent runbook for prompts
