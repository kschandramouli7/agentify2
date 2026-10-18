# 010 – Skill router (Pattern A: deterministic pre-fetch + single Claude call per skill)

> Builds on [ADR 0006](../decisions/0006-two-tier-query-path.md) (Tier-2 agentic
> path), [spec 005](005-root-cause-correlation.md) (diagnose intent), the P5
> skills design in [ROADMAP.md](../ROADMAP.md#p5--supporting-tooling-when-scaling),
> and [ADR 0026](../decisions/0026-pattern-a-skills-standardisation.md)
> (Pattern A standardisation across all skills).

## Goal

Replace the single monolithic `K8fyAgent` (all tools, one general prompt) with a
lightweight skill router that dispatches each intent to a focused sub-agent, reducing
token cost and improving answer quality by giving each skill only the data and prompt
context relevant to its domain.

All five skill classes use **Pattern A**: pre-fetch all predictable data in parallel
before the Claude call, then make a single Claude call with the assembled payload.
No tools are declared to Claude; the data is injected directly into the user message.

## Depends on

- Policies: [data-governance](../policies/data-governance.md) (redaction happens
  upstream in Go before the agent sees data — unchanged).
- Specs: [003](003-tier1-deterministic-queries.md) (Tier-1 exits before the router
  runs), [005](005-root-cause-correlation.md) (diagnose intent + structured output
  schema is preserved as-is).

## Context / constraints

- The intent is **already classified** by `inferIntent()` in Go (`handlers.go`)
  before the HTTP call reaches the Python agent — the router is a pure dispatch
  table, zero extra classification cost.
- The Go backend, the `/reason` HTTP contract, and `AgentResponse`/`QueryRequest`
  Pydantic models are **untouched** — the router is Python-layer only.
- The `REASONING_SCHEMA` (structured JSON output) is unchanged — all skills use it.
- `K8fyAgent` remains the fallback for unrecognised intents (`general_query`,
  `metrics_query`) — all 7 tools, full prompt, existing behaviour preserved exactly.

## Skills

| Intent | Skill class | Pre-fetch calls | Data keys injected | Claude model |
|--------|-------------|-----------------|-------------------|--------------|
| `health_check` | `HealthSkill` | `get_service_health` + `get_pod_events` (degraded pods) | `service_health`, `events.<pod-id>` | `settings.claude_model` (Sonnet) |
| `cert_check` | `CertAuditSkill` | `get_certificates(namespace)` | `certificates` | `settings.claude_model` |
| `change_history` | `ChangeHistorySkill` | `get_change_history(namespace, service_name)` | `change_history` | `settings.claude_model` |
| `metrics_history` | `RestartTrendSkill` | `get_metrics_history(namespace, service_name, asc)` | `metrics_history` | `settings.claude_model` |
| `diagnose` | `DiagnoseSkill` | `get_service_health` + `get_pod_events` (all pods) + `get_metrics_history` + `get_change_history` + `get_logs` (crashing pods only — routes to the Glue/Athena log platform first when configured, ADR 0021, else the live cluster) | `service_health`, `events.<pod-id>`, `metrics_history`, `change_history`, `logs.<pod-id>` | `ADVISOR_MODEL` (Opus 4.8) |
| anything else | `K8fyAgent` (fallback) | none (agentic loop) | — | `settings.claude_model` |

### Pattern A — all five skill classes

Every skill overrides `reason()` with the same two-step sequence:

```python
async def reason(self, intent, data, context):
    prefetched = await self._prefetch(data, context)   # parallel asyncio.gather
    return await self._reason_pattern_a(intent, data, context, prefetched)  # 1 call
```

`_prefetch()` fires all predictable tool calls in parallel. Pre-fetch failures are
caught per-key, logged as warnings, and excluded from the payload — a failed signal
does not abort the request.

`_reason_pattern_a()` (in `K8fyAgent`) merges `data` + `prefetched`, builds the user
message with a "All relevant data has been pre-fetched and is included above" suffix,
and makes a single `messages.create` call with **no tools declared**. It records
`tool_iterations=0` in metrics.

Cost per request: **N parallel backend fetches + exactly 1 Claude call** (predictable).

### Pre-fetch sequences

**HealthSkill** (`health_check`):
- `get_service_health(service_name, namespace)` — when `service_name` is in context.
- `get_pod_events(pod_id, namespace)` — for every pod with `restarts > 0` or
  `ready=False`. These are the only calls the agentic loop would have made anyway.

**CertAuditSkill** (`cert_check`):
- `get_certificates(namespace)` — unconditionally; cert data is always the sole
  data source for this intent.

**ChangeHistorySkill** (`change_history`):
- `get_change_history(namespace, service_name)` — unconditionally.

**RestartTrendSkill** (`metrics_history`):
- `get_metrics_history(namespace, service_name, order=asc)` — unconditionally.

**DiagnoseSkill** (`diagnose`):
- `get_service_health(service_name, namespace)` — when `service_name` is known.
- `get_pod_events(pod_id, namespace)` — for every pod in initial `data`.
- `get_metrics_history(namespace, service_name, order=asc)` — always.
- `get_change_history(namespace, service_name)` — always.
- `get_logs(namespace, pod, previous=True)` — only for crashing pods
  (`restarts >= 3` or `phase in {Failed, Unknown, CrashLoopBackOff}`). Routes
  to the Glue/Athena log platform first when configured (ADR 0021), else the
  live cluster — see `log_router.py`.
- `DiagnoseSkill` overrides `self.model = ADVISOR_MODEL` (`claude-opus-4-8`) after
  `super().__init__()` — diagnosis warrants the most capable model.

## Behavior

- **Given** intent `health_check` **then** `HealthSkill._prefetch()` fires service
  health + degraded-pod events; one Sonnet call assembles the answer.
- **Given** intent `cert_check` **then** `CertAuditSkill._prefetch()` fetches all
  namespace certs; one Sonnet call evaluates them.
- **Given** intent `change_history` **then** `ChangeHistorySkill._prefetch()` fetches
  change events; one Sonnet call produces the timeline.
- **Given** intent `metrics_history` **then** `RestartTrendSkill._prefetch()` fetches
  the restart time-series (asc); one Sonnet call produces the trend analysis.
- **Given** intent `diagnose` **then** `DiagnoseSkill._prefetch()` fires up to
  (4 + crashing-pod-count) calls in parallel; one Opus call synthesises the
  causal narrative.
- **Given** any other intent **then** the full `K8fyAgent` agentic loop runs
  unchanged (backward-compatible fallback).
- **Given** a pre-fetch call fails **then** that signal is excluded from the payload
  with a warning log; the Claude call proceeds with available data.

## Interfaces

```python
# src/agent/k8fy/skills/router.py
class SkillRouter:
    async def dispatch(intent: str, data: dict, context: dict) -> AgentResponse: ...

def get_skill_router() -> SkillRouter: ...   # singleton, thread-safe

# src/agent/k8fy/agent.py  (base — _reason_pattern_a is defined here)
class K8fyAgent:
    async def _reason_pattern_a(
        self, intent: str, data: dict, context: dict, prefetched: dict
    ) -> AgentResponse: ...

    async def _fetch(self, tool_name: str, args: dict) -> Any: ...

# src/agent/app.py  (one-line change from monolithic agent)
# BEFORE: agent = get_k8fy_agent(); response = await agent.reason(...)
# AFTER:  response = await get_skill_router().dispatch(request.intent, ...)
```

## Acceptance criteria

> Checked 2026-10-18. `SkillRouter`/`dispatch` (`router.py`), `_reason_pattern_a`
> and `metrics.record_tool_iterations` (`agent.py`), and `DiagnoseSkill`'s
> `ADVISOR_MODEL = claude-opus-4-8` override (`diagnose.py:85`) all verified
> present. Router now dispatches 13 intents (3 more than this spec's
> original 5, per ADR 0038/0043/ROADMAP P18#2 — same Pattern A shape, see
> `docs/AGENT_INTEGRATION.md`). Shipped and tested; simply never checked off.

- [x] `health_check` query reaches `HealthSkill`; Claude is called with **no tools**
      declared; `tool_iterations` metric records 0.
- [x] `cert_check` query reaches `CertAuditSkill`; Claude is called with **no tools**
      declared; `tool_iterations` records 0.
- [x] `change_history` query reaches `ChangeHistorySkill`; change events appear in
      the user message; `tool_iterations` records 0.
- [x] `metrics_history` query reaches `RestartTrendSkill`; restart time-series appears
      in the user message; `tool_iterations` records 0.
- [x] `diagnose` query reaches `DiagnoseSkill`; all parallel pre-fetch signals appear
      in the user message; `tool_iterations` records 0; model used is `claude-opus-4-8`.
- [x] `general_query` / `metrics_query` fall through to `K8fyAgent` (agentic loop,
      all 7 tools).
- [x] `AgentResponse` schema is identical for all skill paths — Go backend unchanged.
- [x] A pre-fetch failure on one signal does not fail the request; it is logged as a
      warning and excluded from the assembled payload.
