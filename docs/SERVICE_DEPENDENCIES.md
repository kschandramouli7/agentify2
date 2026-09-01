# Service Dependencies — the dashboard and the data behind it

The **Dependencies** tab (`⇄`) in the ops console shows the mined
service-to-service call graph. ROADMAP P18 use case #2;
[ADR 0029](../context-mesh/decisions/0029-glue-based-dependency-mining.md).

> **Read this first.** Every edge is *evidence that a caller logged a callee's
> hostname*, not an observed network flow. There is no sidecar, no eBPF, no
> service mesh here. So:
>
> - an edge present → that call almost certainly happens;
> - an edge **absent → no evidence**, which is *not* the same as no dependency.
>
> Treat the graph as a lower bound. Never conclude "nothing depends on X, so X
> is safe to remove" from this panel alone.

## 1. Where to find it

| | |
|---|---|
| Tab | **Dependencies** (`⇄`) — "Mined service-to-service call graph (P18 #2)" |
| Component | [TopologyPanel.tsx](../src/frontend/src/components/TopologyPanel.tsx) |
| Diagram | [DependencyFlow.tsx](../src/frontend/src/components/DependencyFlow.tsx) |
| Data | `GET /api/service-dependencies?namespace=<ns>` |
| Namespace list | `GET /admin/tracked` (the distinct prefixes of its `ns/service` pairs) |

Open the console via the ALB — see
[CLOUDSHELL_RUNBOOK.md §3b](CLOUDSHELL_RUNBOOK.md#3b-open-the-ops-console) — or
run it locally against the cluster:

```bash
kubectl port-forward -n agentify svc/agentify-backend 18080:8080 &
cd src/frontend && VITE_BACKEND_URL=http://localhost:18080 npm run dev
```

## 1b. Asking in chat

You do not have to open the tab. Ask in the Chat page:

> what are the upstream and downstream dependencies for payment-api?

The answer carries a **Service dependencies** section rendering the same
diagram, focused on the service you named, plus the upstream/downstream sets in
words and the blast radius.

### The turn is routed, not just decorated

A **pure** dependency question never reaches the model. `_chat_route()` in
`src/agent/k8fy/agent.py` returns `"dependencies"`, the agent reads the graph
from `GET /api/service-dependencies` and composes the answer itself, and the
turn comes back as **`tier1`** — no model call, no tokens, no cost, no
latency beyond one database read.

Three reasons, and the second is the load-bearing one:

- reading the graph back is a traversal with one correct answer — the same
  argument ADR 0029 makes for mining it ("a plain extraction task; no Claude
  call belongs in this pipeline");
- `_structure_chat_answer` rebuilds `details` from the model's **prose**, so
  edges the model saw in a tool result were being discarded before the UI could
  draw them. A paraphrase is not drawable;
- a model asked to restate counts can get them wrong. These counts are already
  easy to misread (§3) without adding a paraphrase step.

### What is *not* routed, deliberately

Routing only fires when the question is about the call graph **and nothing
else**. If the turn also mentions health, logs, certs, metrics, changes,
remediation, or uses diagnostic phrasing (`why`, `root cause`, `broken`, …), it
goes to the model as normal — and the graph is still attached to the answer, so
nothing is lost.

That precedence mirrors Go's `inferIntent()`, which checks diagnostic phrasing
first: *"why is payment-api slow, does it depend on vault?"* is a diagnosis, and
the graph is context for it, not the answer. The deterministic path holds no
health, log, cert or metric data, so answering that question from the graph
alone would silently drop half of it.

| Question | Route |
|---|---|
| what are the upstream and downstream dependencies of payment-api? | `tier1`, deterministic |
| who calls payment-api? | `tier1`, deterministic |
| show me the call graph for payments | `tier1`, deterministic |
| why is payment-api slow, does it depend on vault? | model, graph attached |
| what are payment-api's dependencies and is it healthy? | model, graph attached |
| what changed in payment-api's dependencies recently? | model, graph attached |

It also falls through to the model when there is **no graph** for the
namespace — a bare "no evidence" would be a dead end, while the model can
explain why the namespace is empty (§4).

### Details that matter if you change this

- **The turn is still traced.** The route sits inside `reason_chat`, below its
  decorators, so a deterministic turn is recorded as a conversation turn like
  any other (P19 gap E). A route placed in the FastAPI layer would have been
  marginally cheaper and would have lost that.
- **A `tier1` answer carries no prompt provenance.** `@_with_system_prompt`
  stamps `prompt_name`/`prompt_version` on every response; it now skips `tier1`,
  because attributing a deterministic answer to a prompt version would distort
  what the promotion gate measures ([PROMPT_LIFECYCLE.md](PROMPT_LIFECYCLE.md)).
- **Go logs the tier the agent reports**, defaulting to `tier2` when the field
  is absent, so an older agent build still records correctly.
- **Focus** is resolved by matching the question against the graph's own service
  names, longest first, so `payment-api` is never collapsed to `payment`;
  failing that, the session's context service. It cannot focus a service the
  graph does not contain.
- **Only the latest user turn is inspected**, so a conversation that discussed
  dependencies five turns ago does not attach a graph to every later answer.

## 2. What the panel shows

**Namespace picker.** Populated from `/admin/tracked`, so it lists only
namespaces Discovery has actually reported. It defaults to `payments` when
present (the namespace with deliberate test traffic), else the first tracked
one. While the list is still empty the control degrades to a free-text box —
unpopulated, not unusable.

**Stat row.** Services · Edges · Entry points · Terminal · Stale edges.
"Stale" is **no new evidence in 15 minutes** — roughly 15 Discovery scan cycles
at the 60s default, so one missed cycle never trips it. There is deliberately no
"total observations" figure: a sum of cycle-sightings (see §3) is close to
meaningless.

**Flow diagram (primary).** Left-to-right layered dataflow: an arrow means
*from calls to*. Layers come from longest-path layering, so a service sits one
column right of its furthest upstream caller, with one barycenter pass to reduce
crossings. An edge that skips a column is routed through a waypoint in each
column it crosses, so it passes *between* the intervening boxes rather than
underneath them. Each node's subtitle is its in/out degree, or its role when one
side is zero — `entry` (nothing observed calling it) or `terminal` (observed
calling nothing). Degrees are always computed over the whole graph, so they do
not change when you focus.

Line weight is **confidence in three bands**, not volume — see §3. The count is
printed on every arrow regardless.

Click a node (or a chip below) to focus it. Focus draws the **transitive**
closure with hop distance, not one hop, and states the blast radius in prose:
*"if payment-api fails, 2 services upstream are affected: payment-batch
(direct), payment-worker (direct)."* One hop answers "who calls this"; during an
incident the question is "who breaks with it". Cycles are legal and render as an
arrow bowed underneath.

Past **24 nodes or 60 edges** the diagram deliberately refuses to draw and tells
you to focus a service or read the table. A picture of 200 edges is a hairball,
not an answer.

**Focus lists.** For the focused service: what it calls, and what calls it, each
with evidence and freshness.

**Table (the text alternative).** Every edge with From / To / Evidence / Last
seen / First seen, sorted by evidence. This exists both for accessibility and
because sometimes you want the numbers, not the shape.

**Mermaid export.** A `graph LR` block to paste into a PR, ADR or incident
writeup — GitHub renders it, and it costs no runtime library. Edge labels are
observation counts.

### How it is coloured, and why so little

There are no categorical series in this data, so no hue means "identity." The
accent marks the focused subgraph; hop distance is shown by opacity, not a
second hue. Freshness is a state → the app's reserved status tokens, always with
a word (`fresh` / `stale`), never colour alone. Everything reuses the existing
CSS token system, so dark mode and the rest of the console stay consistent.

## 3. Where the data comes from

Three producers write to one table. All three share the *same* extraction
function, so the matching rule below is true of all of them.

| Producer | Reads | Cadence | Scope |
|---|---|---|---|
| **Live miner** — `_scan_namespace`, [discovery/main.py](../src/adapters/discovery/main.py) | pod logs via the K8s API, on the cluster | `SCAN_INTERVAL_SECONDS`, **60s** | the cluster it runs in |
| **Glue/Athena miner** — [dependency_miner.py](../src/agent/k8fy/dependency_miner.py) | shipped logs in Glue, via Athena | `DEPENDENCY_MINING_INTERVAL_SECONDS`, **3600s** | every onboarded cluster |
| **Agent skill path** — `mine_service_dependencies`, [service_topology.py](../src/agent/k8fy/service_topology.py) | the log tail a diagnose already fetched | per query | whatever was diagnosed |

Live mining is the **faster** signal; Glue mining is the **broader** one (a
cluster that ships logs but was never onboarded for live scanning still
contributes). Neither backfills.

Three *readers* consume the table: the Dependencies tab, the deterministic chat
route (§1b), and every Pattern-A skill's prefetch — which is why ADR 0029 could
add the Glue miner with no changes to any call site.

`extract_service_mentions` is duplicated verbatim between
`src/agent/k8fy/` and `src/adapters/discovery/` — a deliberate copy, per ADR
0029 — and the Glue miner *imports* the agent copy rather than adding a third.
**Both copies carry the same test file**, so they cannot drift silently. Change
one, change the other.

### What `evidence_count` actually measures

**It is not call volume.** It is the number of scan cycles in which a miner saw
the caller log the callee's hostname. So it tracks three things that have
nothing to do with traffic: how chatty the caller's logging is, how long the
edge has existed, and sampling luck (§4, items 4–5).

The payments namespace demonstrates it exactly. payment-batch's three edges all
read **187** — identical, because it logs all three targets in every burst — and
187 × the 60s scan interval is precisely their 3h age. payment-worker's **13** is
the same 3 hours seen in only 13 cycles, not 14× less traffic.

Read it as **confidence**: how many independent times the system confirmed the
edge is real. That is why the diagram uses three *absolute* bands (1–2 / 3–19 /
20+) rather than a width ramp relative to the graph's maximum — 40 sightings
means the same thing whether or not some other edge has 4000 — and why the table
column is named "Cycles seen".

The earlier design ramped line width continuously across `evidence_count`, which
turned "payment-batch logs more" into a visual claim that its dependencies were
far more important. Do not reintroduce that.

### The matching rule

A name in log text becomes a candidate in one of two forms:

| Form | Example | Condition |
|---|---|---|
| **Qualified** | `payment-api.payments.svc.cluster.local` | the namespace segment must equal the scanned namespace |
| **Bare** | `http://agentify-backend:8080` | must be in a *hostname context* — right after `//`, or right before `:<port>` |

Then, in both cases, **the name must be in that namespace's live Service list**.
Ground truth, not regex-shaped text.

The bare form was added 2026-09-01 and matters more than it sounds. Kubernetes
resolves a short name through the pod's search domain, so real in-cluster callers
write `http://agentify-backend:8080` and almost never the FQDN. Before the fix,
`payments` showed five edges purely because its test workloads were written to
log FQDNs on purpose, while `agentify` and `vault` showed **zero** while calling
each other constantly. The subsystem reported success and observed nothing.

A bare name counts only in a hostname context because a service named `payment`
would otherwise match the word "payment" in ordinary log prose. That boundary is
pinned by a test — `payment-backend restarted due to OOMKilled` must yield
nothing — and it will fail if anyone loosens the regex.

Redaction ([log_redaction.py](../src/adapters/discovery/log_redaction.py)) runs
**before** extraction and is compatible: it rewrites URL userinfo
(`//user:pass@host` → `//user:***@host`) but leaves the host and port intact, so
the edge is still found.

## 4. Why a namespace shows nothing

Work down this list — it is ordered by how often each one is the answer.

1. **Nobody logs the hostname.** The most common cause by far. A service can
   call another all day; if the client library logs nothing, or logs only a
   path (`POST /api/query`), there is no evidence to mine. Structured loggers
   that record `host` as a separate field the miner never sees are a frequent
   offender.
2. **The caller has no Service.** `from_service` is resolved by matching pod
   labels against Service selectors; a pod matching no Service is
   unattributable and its mentions are dropped, however faithfully it logs.
3. **The callee isn't in the Service list.** External hosts (`api.anthropic.com`)
   and non-Service targets are correctly ignored.
4. **Only the first 5 pods per namespace are sampled.**
   `MAX_PODS_PER_NAMESPACE=5`, and the order is whatever the K8s API returns —
   so in a namespace with more pods, mining is a *sample*, and an edge may take
   several cycles to appear or never appear.
5. **Only the last 200 log lines are read.** `LOG_TAIL_LINES=200`. A chatty pod
   can push its own outbound calls out of the window between scans.
6. **Multi-container pods return no logs at all.** `get_pod_logs` sends no
   `container` parameter, so the K8s API returns 400, the function warns and
   returns `""`. Every pod in a service-mesh cluster is multi-container, which
   makes this the limitation most likely to bite next (tracked as OPS-9).
7. **The direction is inbound-only.** A service that is *only called* — Vault,
   for instance, reached by init containers — produces no outbound evidence of
   its own. Its edges must come from its callers logging it.
8. **Glue rows predate onboarding.** Rows captured before a cluster got the
   Fluent Bit `cluster_id` tag carry no `cluster_id` and are invisible to the
   Glue miner (ADR 0029, Phase 1). Not an error.

An empty panel that survives all eight is worth investigating. An empty panel
that matches one of them is the system being honest.

## 5. Verifying it end to end

**The table.** Run from CloudShell via the psql-in-a-pod recipe in
[CLOUDSHELL_RUNBOOK.md §2](CLOUDSHELL_RUNBOOK.md#2-query-the-database):

```sql
SELECT namespace, from_service, to_service, evidence_count, last_seen
  FROM service_dependencies ORDER BY last_seen DESC;
```

`evidence_count` **climbing** on a repeat run is the real proof — it shows the
upsert path works, not just the first insert. The unique key is
`(tenant_id, cluster_id, namespace, from_service, to_service)`.

**The API** (behind a port-forward):

```bash
curl -sS "localhost:18080/api/service-dependencies?namespace=agentify" | python3 -m json.tool
```

Returns `[]` rather than an error when the store is unconfigured or the query
fails — deliberate, but it means an empty array is not proof of an empty graph.

**The miner's own logs:**

```bash
kubectl logs -n agentify -l app=agentify-discovery --tail=100 | grep -iE "scan cycle|push_dependency|scan failed"
kubectl logs -n agentify -l app=agentify-agent    --tail=200 | grep -i "dependency_miner"
```

`dependency_miner: Athena not configured, skipping this cycle` means the Glue
path is off; the live path is unaffected.

**Generating traffic on purpose.**
[payment-batch.yaml](../infra/kubernetes/payments-test/payment-batch.yaml) is a
single-container Deployment that bursts calls every 30s and logs each target
hostname whether or not the call succeeds. Its header explains why it is a
Deployment and not a CronJob (a CronJob pod exits before the live miner can read
its logs) and why it has its own Service (so its calls are attributable).

## 6. Known limits, stated plainly

- **Lower bound, always.** See the note at the top.
- **The counts are confidence, not volume.** See §3, "What `evidence_count`
  actually measures" — the single easiest thing to misread here.
- **No backfill.** Both miners reflect state going forward.
- **Sampling.** 5 pods, 200 lines, per cycle.
- **Up to an hour of lag on the Glue path**, ~60s on the live path.
- **`cluster_id` is nullable**, so today's single-cluster rows and a
  multi-cluster future coexist in one unique key. Two clusters running the same
  namespace produce separate rows, which is correct but can read as duplicates.
- **Self-mentions are dropped** — a service logging its own name is not a
  dependency.
- **No edge weighting by time.** `evidence_count` is cumulative since
  `first_seen`; a dependency removed months ago keeps its count and only its
  `last_seen` goes stale. Read freshness, not volume, to judge whether an edge
  is current.

## References

- [ADR 0029](../context-mesh/decisions/0029-glue-based-dependency-mining.md) — the mining architecture and its four phases
- [ADR 0028](../context-mesh/decisions/0028-live-query-cluster-fanout.md) — the cross-cluster fan-out that consumes these edges
- [CLOUDSHELL_RUNBOOK.md](CLOUDSHELL_RUNBOOK.md) — logs, psql, port-forwards
- [SEQUENCE_FLOWS.md](SEQUENCE_FLOWS.md) — how a query moves through the system
