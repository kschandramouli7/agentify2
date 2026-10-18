# 0004 – Tech stack: polyglot (Go + Python + TypeScript) on AWS, cost-optimized

## Status

Accepted   ·   2026-05-30   ·   **Placement superseded 2026-06-06 by [ADR
0017](0017-all-on-eks-topology.md)** — the ECS Fargate orchestration choice
below is no longer current; backend, agent, and adapters all run on a
single EKS cluster today. The polyglot *language* choice (Go/Python/
TypeScript) made in this ADR is unaffected and still stands.

## Context

Agentify is a distributed system with distinct layers:
- **Orchestrator + API** — deterministic routing, needs concurrency + low latency.
- **Agent layer** — Claude SDK integration, flexible reasoning; can tolerate cold-starts in dev, but costs must be predictable.
- **Adapters** — integration-specific logic; needs rapid iteration early, performance scaling later.
- **Frontend** — real-time chat + admin CRUD; needs rich UX toolkit.

The question: pick one language (monolith) or multiple (polyglot)? And which compute model for the agent (serverless vs containerized)?

## Decision

**Polyglot stack:**
- **Backend Core (Orchestrator + API + WebSocket handler):** Go
- **Agent Layer (Claude integration + reasoning):** Python (FastAPI), containerized on ECS Fargate (NOT Lambda)
- **Adapters (K8s, CRM, webhooks):** Python initially; Go for high-volume adapters (future)
- **Frontend (Ops chat UI + Admin config UI):** TypeScript + React

**Why:**
- **Go for orchestrator:** compiled, concurrent, single binary deployment, negligible operational overhead. Handles 10k+ WebSocket connections with minimal resources.
- **Python for agent:** Claude SDK native, flexibility for system prompts + tool calling, rapid iteration. Containerized (not Lambda) to avoid cost surprises during dev/testing — a Fargate task (0.25 CPU, 512 MB RAM) costs ~$5/month idle vs. Lambda's per-invocation surprise spikes.
- **Adapters in Python:** quick prototyping. If an adapter needs scale (e.g. 1000s events/sec), rewrite in Go — adapters are decoupled, so isolated rewrites don't break the core.
- **TypeScript for frontend:** unified frontend/backend types, real-time WebSocket handling, rapid CRUD interfaces for admin panels.

**AWS for deployment:**
- Fully managed services (RDS, ElastiCache, DynamoDB, Secrets Manager) for operational simplicity.
- ECS Fargate (not EC2) for container orchestration — no EC2 fleet management, just "run this container."
- Cost-first: start with minimal Fargate tasks (0.25 CPU, 512 MB), scale horizontally on demand.

## Consequences

- **Positive:** each layer uses the best tool for its job; low operational overhead; predictable costs (no serverless surprises); easy to rewrite one component later without affecting others.
- **Negative / cost accepted:** polyglot means multiple runtimes to deploy and monitor; orchestration complexity (though ECS handles this); developer context-switching (though layers are well-separated, so teams can own one language each).
- **Revisit if:** a single language (e.g. all Go, or all Python) becomes simpler to operate; or if Lambda costs prove cheaper in production than containerized agents.

## Scaling path (no major refactoring)

- **Orchestrator overloaded?** → add more Go Fargate tasks; ALB round-robins.
- **Agent response slow?** → add more Python tasks; queue incoming requests in SQS if needed.
- **Adapter throughput bottleneck?** → rewrite that adapter in Go; keep the Python API wrapper the same.
- **Storage queries slow?** → RDS reads → add read replicas; Redis → upgrade to cluster; switch vector DB if needed.

All of this is possible without changing the core design — the layers are decoupled by contracts (APIs, events, configurations).
