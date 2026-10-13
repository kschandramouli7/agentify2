# 0042 – Splunk as a continuous dependency-mining source

## Status

Proposed   ·   (date: 2026-10-13)

## Context

ADR 0041 built a Splunk connector for exactly one use case: an on-demand,
single-pod log *read*, triggered reactively by a chat question or a
diagnose/incident-response prefetch. It does not touch the Dependencies
diagram at all — that graph is built by two *continuously running*
processes, neither of which goes through `log_router.py`:

- **Discovery's live scan** (`src/adapters/discovery/main.py::_scan_namespace`) —
  per-cluster, reads live pod logs via the K8s API every ~60s.
- **The Glue/Athena miner** (`k8fy/dependency_miner.py`, ADR 0029) — runs
  centrally in the Agent process, every `DEPENDENCY_MINING_INTERVAL_SECONDS`
  (300s), querying the shared Firehose-fed Glue table across every
  onboarded cluster/namespace.

**The ask:** give Splunk the same role the Glue miner already has — a
continuous, periodic producer of `service_dependencies` evidence — so a
customer whose logs already land in Splunk gets the *same* upstream/
downstream diagram, with the *same* connectivity/failure highlighting,
without needing Athena/Firehose at all. Framed explicitly as a real-time(-
ish) investigation aid for Ops/Support, not just a diagnose-time
convenience.

## Decision

### Build `k8fy/splunk_dependency_miner.py`, structurally identical to `dependency_miner.py`

Same periodic-background-task shape (`run_once`/`run_forever`, started
from `app.py`'s startup event alongside the existing Glue task), same
cluster/namespace loop (`_fetch_registered_clusters` → each cluster's
`Integration.Namespaces` — the same registry the Glue miner already
iterates, not a new onboarding list; a namespace the customer hasn't
routed to Splunk just comes back empty, same graceful-no-data posture
Athena already has for non-Fargate-onboarded namespaces), same extraction
call (`extract_service_calls`, imported and used **verbatim** — zero new
extraction logic), same push call (`POST /api/service-dependencies`,
**zero backend changes** — `source` is already a free string field; this
adds a fourth producer value, `"splunk"`, alongside `live`/`glue`/
`agent_skill`, ADR 0036). This is deliberately the same discipline ADR
0041 already committed to: build against the shape that's actually
proven, not a new one.

### Why connectivity/failure highlighting needs ZERO new UI work

This is the part worth stating plainly: **the mechanism the ask describes
already exists and is producer-agnostic.** `edgeHealth()` (`DependencyFlow.tsx`)
classifies an edge as healthy/degraded/failing from
`outcome_success_count`/`outcome_failure_count`/`outcome_timeout_count` —
three columns on `service_dependencies` that `extract_service_calls`
already populates from outcome keywords/status codes in the log text
(ADR 0031), regardless of which miner called it. The Dependencies diagram,
the health banner, the per-edge "N/M failed" label, ADR 0032's expected-
failure annotation, ADR 0040's rising/declining trend — **every one of
these already works for a Splunk-sourced edge the moment it lands in the
table with a `source="splunk"` tag**, because none of that UI branches on
`source` at all. The only genuinely new work is getting Splunk's log text
*into* `extract_service_calls` in the first place; everything downstream
of that call is already built and shared.

### Query construction — one broad search per namespace per cycle, not per pod

ADR 0041's `_build_spl` is scoped to one pod (the on-demand read's unit of
work). This miner needs the Glue miner's own unit of work instead: **every
pod in the namespace, in one search**, so the extraction can attribute
each line to its own `from_service` the same way `_mine_namespace` already
does for Athena rows:

```spl
search index=<configured index> kubernetes.namespace_name="<namespace>"
| table kubernetes.pod_name, kubernetes.labels.*, _raw
| head <limit>
```

Grouped by `kubernetes.pod_name` after fetch, mirroring `_mine_namespace`'s
own `by_pod`/`pod_labels` dicts exactly, then `_service_for_labels` (reused
unchanged — it's already generic over where the labels came from) resolves
each pod's `from_service` against the cluster's known Service selectors.

### A second unverified-schema risk, named honestly (stacks on ADR 0041's)

ADR 0041 already flagged `kubernetes.pod_name`/`kubernetes.container_name`/
`_raw` as an assumption, unverified against a real index. This design adds
a harder requirement on top: **the Splunk events also need to carry
Kubernetes label metadata** (`kubernetes.labels.*` or equivalent), because
label-to-Service-selector matching is how `from_service` gets resolved at
all — without it, every row is unattributable and the cycle mines nothing,
silently (same `if not selectors: return` / `if not from_service: continue`
posture the Glue miner already has, which fails safe but also fails
*quiet*). Whether a given shipper (Splunk Universal Forwarder, a CIM-
compliant app, Fluent Bit's Splunk output plugin) actually forwards pod
labels is customer-shipper-configuration-dependent and cannot be verified
without a real index. **This is the first thing to check against a real
deployment, before trusting any edge this miner produces.**

### Interval: a real tuning question, not inherited from Glue by default

The Glue miner's 300s interval exists because that's Firehose's own
`buffering_interval` — polling faster would just rescan data that hasn't
landed yet (ADR 0029's 2026-10-09 amendment). **Splunk has no equivalent
hard floor** — its own indexing latency is typically seconds, not minutes,
so a shorter interval is plausible and would serve the "real-time
investigation" framing better than inheriting Glue's cadence by default.
This isn't decided here: `SPLUNK_MINING_INTERVAL_SECONDS` is its own
config knob (not shared with `DEPENDENCY_MINING_INTERVAL_SECONDS`),
default proposed at **60s** (matching Discovery's own live-scan cadence,
the fastest existing precedent in this codebase) but explicitly flagged as
worth tuning against real search latency/load once a real instance exists
— the same "named tradeoff, not hidden" posture the Glue interval change
itself already modeled. A broad namespace-wide search every 60s against a
real enterprise Splunk index has real cost/concurrency implications
(unlike Athena's near-zero-idle-cost test harness) — this is a dial to
turn carefully in production, not a constant to set once and forget.

### "Real-time" — accurate framing, not oversold

This is still a **periodic poll**, the same architectural class as the
Glue miner, not literal push/streaming (that would be option 2 from the
earlier options discussion: Splunk's own saved-search alerting pushing to
the ingest API). At a 60s interval it should *feel* close to real-time for
an Ops/Support investigation, which is what the ask is actually for, but
it is not sub-second and should not be described as such. If a customer
deployment later needs lower latency than polling can give, that's a
distinct future decision (the push-based option), not something this
design should be stretched to pretend it already does.

## Consequences

- **Positive:** the Dependencies diagram, its health/failure highlighting,
  the trend indicator, and the expected-failure annotation all work for
  Splunk-sourced data with **zero frontend changes and zero backend schema
  changes** — the entire new surface is one Python mining task.
- **Positive:** reuses `extract_service_calls`/`_service_for_labels`
  verbatim — no new, separately-maintained extraction logic to drift from
  the Discovery/Glue copies (ADR 0029's "the logic doesn't get rebuilt,
  just re-hosted" principle extended to a third host).
- **Negative / cost accepted:** a second unverified schema assumption
  (label metadata in Splunk events) stacked on top of ADR 0041's first one
  — both need checking against the same real instance, together, before
  this is trusted in production.
- **Negative / cost accepted:** a broad, recurring, namespace-wide search
  against a real Splunk deployment is a real cost/load item in a way
  Athena's test harness never was — the interval needs real operational
  tuning, not a default trusted blindly.
- **Negative / cost accepted:** "real-time" is approximate (poll interval,
  not push) — correctly scoped in this ADR, but worth restating to
  Ops/Support stakeholders so the feature isn't expected to be more
  immediate than a ~60s-interval poll can deliver.
- **Revisit if:** label metadata turns out to be unavailable in the
  customer's actual Splunk events (the fix is likely a different
  attribution strategy, e.g. resolving `from_service` from the pod-name
  pattern instead of labels — a materially different design, not a tweak);
  or if 60s polling proves too expensive/slow against real search load,
  in which case the push-based alerting option from the earlier options
  discussion becomes the next thing to design, not a fallback to silently
  reach for.
