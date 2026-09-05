# Service Dependencies — the dashboard and the data behind it

The **Dependencies** tab (`⇄`) in the ops console shows the mined
service-to-service call graph. ROADMAP P18 use case #2;
[ADR 0029](../context-mesh/decisions/0029-glue-based-dependency-mining.md).

**This panel is also ROADMAP P22 (Architecture View) — it *is* that item's
deliverable, as of the 2026-09-05 rename.** P22 was previously called
"Architecture Autodoc" and promised a generated document; that promise was
dropped in favour of this panel, so nothing is written to a file and nothing
is owed one. Practically: work that makes the architecture more legible
belongs here, not in a parallel generator, and the panel is held to a
document's standard of honesty about its own gaps — hence the coverage
figure on every node and the incomplete-graph banner ("Confidence is
coverage, not the count", §3).

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

> **If a dependency question comes back as prose with no diagram**, the running
> agent image predates this route, or the namespace could not be resolved. The
> tell is the answer's shape: the deterministic answer is templated and always
> ends with the "lower bound" sentence, while a model answer carries timestamps
> and a Recommendations section. Check with
> `kubectl logs -n agentify -l app=agentify-agent | grep -i "answered deterministically\|resolved namespace"`.

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
- **The namespace is resolved from the question too, because chat has none.**
  `ChatPanel` calls `createChatSession()` with no arguments, so a session's
  namespace is `""` and the Go handler forwards that verbatim. `_resolve_namespace`
  therefore falls back to the Hub's tracked `namespace/service` list: the longest
  service name mentioned wins, then an explicit namespace mention, then the sole
  tracked namespace if there is only one. It returns nothing rather than guessing
  when several namespaces are plausible — answering about the wrong namespace is
  worse than handing the turn to the model. An explicit context namespace always
  wins; this is a fallback, never an override.
- **Only the latest user turn is inspected**, so a conversation that discussed
  dependencies five turns ago does not attach a graph to every later answer.

## 1c. Asking via the API

`inferIntent()` now classifies a pure graph question as the **`dependencies`**
intent, so `POST /api/query` answers it the same way chat does — the same
`DependencyGraphSkill`, the same prose, the same `details.service_graph`, and
`tier1`.

```bash
curl -sS -X POST localhost:18080/api/query -H 'Content-Type: application/json' \
  -d '{"question":"what are the upstream and downstream dependencies for payment-api?",
       "context":{"namespace":"payments"}}' | python3 -m json.tool
```

The response carries `"tier": "tier1"` and `"intent": "dependencies"` — proof it
cost nothing and involved no model.

**This intent bypasses the pod lookup**, unlike every other one.
`HandleQuery` normally routes to storage pods first and returns *"No data
available for this query"* when none match. The graph lives in
`service_dependencies`, not in any pod, so a namespace with a good mined graph
and no registered pods was hitting that bail and never reaching the skill. The
route therefore runs immediately after intent classification, before any pod
work (`answerDependencies`).

The routing rule is the **same** as chat's (§1b) and is implemented twice —
`isDependencyQuestion` in Go for this path, `_chat_route` in Python for chat.
Both are exercised by the same table of questions, in
`src/backend/internal/api/intent_test.go` and
`src/agent/tests/test_chat_service_graph.py`, so drift fails a test rather than
quietly making this document wrong for one entry point.

> **Keyword stems are matched at a word boundary, not as bare substrings.** With
> plain substring matching, `log` matched `topo`**`log`**`y`, so "service
> topology for payments" was excluded as if it had asked about logs —
> `technology`, `logical` and `catalog` fail the same way. Anchoring at a word
> boundary while still matching forward keeps `log` → `logs` and `expir` →
> `expiring` working.

**There is currently no free-text UI for `/api/query`.** `AskPanel` is wired to
render the graph but is not mounted in `App.tsx`; the mounted surfaces
(`ServiceEvaluator`) only send fixed questions. So this path is reachable by API
clients and the eval harness, and **the chat page is the UI** for asking. Mounting
`AskPanel` is a one-line change if a free-text box is wanted.

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

Four *readers* consume the table: the Dependencies tab, the deterministic chat
route (§1b), the `dependencies` intent on `/api/query` (§1c), and every
Pattern-A skill's prefetch — which is why ADR 0029 could add the Glue miner with
no changes to any call site.

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

### Confidence is coverage, not the count

The count alone is still ambiguous, which took two attempts to get right.

Absolute bands on the raw count (1–2 / 3–19 / 20+) were the second attempt, and
real data broke them: payment-batch's edges read **318** over a 5.0h lifetime —
299 scans, so confirmed in essentially every one — while payment-worker's read
**17** over 5.8h, or 350 scans, confirmed in **1 scan in 20**. Both fell in the
same "20+, consistently observed" band. A count with no duration cannot
distinguish "always seen" from "seen a lot, over a very long time".

So the band is **coverage**: observed sightings over the edge's own lifetime in
scans. Scale-free, and it answers the useful question — is this dependency
continuously re-confirmed, or does the miner mostly miss it?

| Coverage | Band | Line |
|---|---|---|
| ≥ 75% | confirmed in nearly every scan | thickest |
| 25–75% | confirmed intermittently | medium |
| < 25% | rarely caught — the miner mostly misses this call | thinnest |
| lifetime < 3 scans | too new to judge | medium |

Coverage is **approximate on purpose** and clamped to 100%: three producers push
at different cadences (60s live, hourly Glue, plus each diagnose), so sightings
can exceed elapsed scans. It is shown as a band and a ratio, never as a precise
figure.

**A low-coverage edge is a finding, not a styling detail.** If the miner catches
a call it *knows about* in 5% of scans, it is very likely missing calls it never
catches at all — so the panel says so above the diagram rather than leaving it in
a tooltip. In the payments namespace, `payment-worker`'s two edges are exactly
this case, while the panel's "Stale edges: 0 · all fresh" reads reassuringly:
freshness and coverage are different questions, and both matter.

Freshness is now visible in the diagram too — a stale edge is dashed. Previously
it showed only in the table, so a dependency going cold looked identical to one
confirmed a minute ago.

The first attempt ramped line width continuously across `evidence_count`, which
turned "payment-batch logs more" into a visual claim that its dependencies were
far more important. Do not reintroduce either that or the raw-count bands.

### Three trust tiers, and why they must stay apart

As of 2026-09-05 an edge carries a `target_kind`, because the diagram used to
claim each namespace was a closed system and that is false as architecture —
`agentify-agent` calls `vault.vault`, `api.anthropic.com` and an RDS endpoint,
and none could ever appear.

| `target_kind` | Validated against | Strength |
|---|---|---|
| `service` | the live Service list for that namespace | **strong** — a real object confirms the name |
| `cross_namespace` | **both** segments — the namespace must exist and the service must be a real Service in it | strong |
| `external` | **nothing** | **weak** — there is no Service list for the internet, so this rests on hostname-shape heuristics alone |

The weak tier is drawn lighter, its nodes sit unfilled in a boundary column,
and its tooltip says so. **Do not merge the tiers** — a generated
NetworkPolicy (P24) built from `external` edges without review would encode
guesses as firewall rules.

> ### The `external` tier is OFF by default. It was shipped and disabled the same day.
>
> `MINE_EXTERNAL_EGRESS=false` (2026-09-05). It produced, in one afternoon on a
> real cluster:
>
> | Fabricated dependency | Actual source |
> |---|---|
> | `www.nokia.com` | a `Referer` header in the frontend's nginx access log |
> | `internet-measurement.com` | an internet scanner's `User-Agent`, which embeds `+https://…` |
> | `dashboard.voyageai.com`, `docs.voyageai.com` | URLs quoted inside Voyage's own 402 error body |
>
> **The flaw is structural, not a tuning problem.** The extractor cannot
> distinguish a host we *called* from a host that merely *appears* in the log
> text. The in-namespace miner never had this problem because it validates
> every candidate against the live Service list — a scanner's `Referer` will
> never be a Service name. Removing that validation removed the only thing
> keeping it honest, and no amount of hostname-shape heuristics replaces it.
>
> It was also worse than it looked: the backend that received these edges
> predated the `target_kind` column, so it **silently stored every one as
> `service`** — the strong tier — and they rendered as `terminal · 1 caller`,
> indistinguishable from a validated edge. The UI now falls back to
> classifying by shape (an RFC 1123 Service name cannot contain a dot), so it
> is honest against old rows too.
>
> **Cross-namespace mining stays on** — but validating only its namespace
> segment turned out to be the same mistake one level narrower. On 2026-09-05 a
> trace UUID followed by a real namespace
> (`c53b9dca-f4c0-44f9-….vault`) passed as a service call and drew three phantom
> boxes, because `_HOSTNAME_RE`'s character class accepts hex and hyphens. It
> now validates **both** segments against the real Service list of the target
> namespace, which is the guard the same-namespace miner has always had and the
> reason that tier never had this problem. Rejecting UUID *shapes* would have
> been another heuristic; requiring a real Service removes the class.
>
> **Conditions for turning it back on**, none of which are met:
> 1. a discriminator on the *log line shape*, not the hostname — an outbound
>    client call (`POST https://host/…`, an httpx/client-library prefix) looks
>    nothing like an access-log line with a status code and a quoted
>    User-Agent, and that difference is the real signal;
> 2. rejection of hosts appearing inside quoted strings, which is where
>    `Referer` and `User-Agent` always live;
> 3. an operator-declared expected-egress allow-list, so a novel host is a
>    *finding to confirm* rather than a fact asserted on the diagram.
>
> Cleaning up rows already written (a Service name never contains a dot, so
> this is exact):
>
> ```sql
> DELETE FROM service_dependencies
>  WHERE to_service LIKE '%.%'
>    AND to_service NOT LIKE '%.svc.cluster.local';
> ```

**The guards on the weak tier are the specification, and they live in tests.**
A log line is full of dotted strings that are not hosts, and every one of these
would otherwise become a fabricated dependency:

| Rejected | Why it would have matched |
|---|---|
| `v1.2.3` | dots, but the last label is numeric |
| `com.example.Handler.process` | dots and alphabetic labels |
| `config.yaml`, `go.sum`, `package.json` | passes the TLD shape test — hence an explicit suffix deny-list |
| `localhost`, `10.0.1.5`, `127.0.0.1`, `169.254.169.254` | loopback, private, and IMDS are not egress |
| `1.234s`, `99.9th` | measurements |
| `printer.local`, `*.svc.cluster.local` | in-cluster; the qualified pass owns them |

Matching happens in a hostname context only (`//host` or `host:port`), the last
label must be alphabetic and at least two characters, and single-label names are
rejected because the pod search domain makes them in-cluster.

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
