# 001 – Event Ingestion (starter spec)

> Example/starter spec — the natural first feature: get events *into* the mesh
> before worrying about routing them perfectly. Edit freely or delete.

> **Status note (2026-10-18), found during a documentation audit:** the
> feature this sketches shipped long ago (`HandleIngestEvent`, `POST
> /api/ingest`, `src/backend/internal/api/handlers.go`) and is a cited
> dependency of specs 002/003, but — unlike [spec
> 003](003-tier1-deterministic-queries.md), which says outright "written
> post-implementation to pin the contract" — this one was never revisited
> against what actually got built. The real, current ingestion contract
> (per-source push over plain HTTP, not a generic `ingest(event)` pod-mesh
> call) is better read from ADR 0022/0027 and `docs/HUB_DATA_PATH.md` than
> from this spec's Interfaces section below, which is left as the original
> starter sketch rather than rewritten to match.

## Goal

Accept a streamed data event, decide where to store it, and write it to the
correct pod (creating one if needed) — emitting feedback for later refinement.

## Depends on

- Policies: [storage-strategy](../policies/storage-strategy.md), [pod-formation](../policies/pod-formation.md)
- Other specs: none

## Context / constraints

- Must update the pod-registry whenever a pod is created or its freshness/count changes.
- **Non-goals:** query routing (separate spec), cross-pod correlation, refinement decisions.

## Behavior

- **Given** an event arrives **and** a matching active pod exists **when** ingested **then** it is written to that pod and the pod's `freshness`/`event_count` update.
- **Given** an event arrives **and** no matching pod exists **when** ingested **then** a new pod is formed per [pod-formation](../policies/pod-formation.md) and registered.
- **Given** ingestion succeeds **when** complete **then** an observation event (pod id, store type, latency) is emitted for the [refinement-loop](../policies/refinement-loop.md).

## Interfaces

```
ingest(event) -> { pod_id, store_type, created_pod: bool }
event = { id, type, payload, source, timestamp }
```

## Open questions

- [ ] v1: single catch-all pod, or attempt clustering from the start?
- [ ] Synchronous write vs. queue + async write?
- [ ] What's the durability guarantee on accept (at-least-once? exactly-once?)?

## Acceptance criteria

- [ ] An event with a known type lands in exactly one pod.
- [ ] A brand-new event type causes a pod to be created and registered.
- [ ] The pod-registry reflects the new/updated pod after ingestion.
- [ ] An observation is emitted for every ingested event.
