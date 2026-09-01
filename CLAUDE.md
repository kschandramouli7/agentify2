# agentify

> One-line product description: _<fill in: what does agentify do and for whom?>_

## How this repo is organized

- **`context-mesh/`** — the *design-time* brain: the policies, specs, and decisions that govern how the product behaves. **Read the relevant file here before implementing anything.**
- **`src/`** — the code that implements the policies and specs.

## Working agreement (for Claude)

1. **Before implementing a feature**, read its spec in `context-mesh/specs/` and any policy it depends on under `context-mesh/policies/`.
2. **Before changing storage, routing, or pod behavior**, read the four core policies (see below). These are the product's brain — do not silently deviate from them.
3. **When you make a hard-to-reverse or non-obvious decision**, write an ADR in `context-mesh/decisions/` (use the `_TEMPLATE.md`).
4. **Specs and policies are the source of truth.** If code and spec disagree, surface it — don't quietly "fix" one to match the other.
5. Prefer English-first: refine the spec/policy, then generate code from it.

## The core policies (the product's brain)

| Policy | Question it answers |
|--------|---------------------|
| [storage-strategy](context-mesh/policies/storage-strategy.md) | Given an event, *where & how* do we store it? |
| [pod-formation](context-mesh/policies/pod-formation.md) | When does a pod get born / split / merge / retire? |
| [refinement-loop](context-mesh/policies/refinement-loop.md) | How does query feedback reshape storage over time? |
| [correlation](context-mesh/policies/correlation.md) | How do we fan out & correlate across multiple pods? |

Routing logic lives in [_orchestrator.md](context-mesh/_orchestrator.md).
Domain terms live in [glossary.md](context-mesh/glossary.md).

## Tech stack

**Languages & Frameworks:**
- **Backend Core (Orchestrator + API):** Go 1.21+, using `net/http` and `gorilla/websocket`
- **Agent Layer (Claude reasoning):** Python 3.11+, FastAPI + Pydantic
- **Adapters (Integration workers):** Python 3.11+ (or Go for high-volume adapters)
- **Frontend (Ops + Admin UI):** TypeScript + React 18+, Vite build tool

**Data & Storage:**
- **Relational:** AWS RDS PostgreSQL (Multi-AZ for production)
- **Key-Value:** AWS ElastiCache Redis (single-node MVP, cluster for scale)
- **Vector:** Pinecone (SaaS) or Weaviate (self-hosted for enterprise)
- **Time-Series:** Prometheus (local) or managed Time-Series DB (AWS)
- **Logs/Search:** Loki or Elasticsearch
- **Configuration:** AWS DynamoDB (low-latency metadata)
- **Secrets:** AWS Secrets Manager
- **Object Storage:** AWS S3 (pod-registry backups, cold data)

**Deployment & Infrastructure:**
- **Orchestration:** AWS ECS Fargate (no EC2 management)
- **Load Balancing:** AWS Application Load Balancer (ALB)
- **Monitoring:** AWS CloudWatch (Logs, Metrics, Alarms) + X-Ray (optional)
- **IaC:** Terraform for AWS resource definitions
- **Containerization:** Docker (all services in containers)
- **API:** REST + WebSocket (for real-time chat)

**Key Dependencies:**
- **Go:** `gorilla/websocket`, `aws-sdk-go-v2`, `pq` (PostgreSQL driver)
- **Python:** `fastapi`, `anthropic` (Claude SDK), `pydantic`, `boto3` (AWS SDK), `redis`, `sqlalchemy`
- **React:** `@tanstack/react-query`, `zustand` (state mgmt), `shadcn/ui` (components)

**Third-party APIs:**
- Anthropic Claude API (with prompt caching for cost savings)
- Kubernetes API (via `client-go` or Python `kubernetes` library)
- AWS APIs (via SDK)

See [ADR 0004](context-mesh/decisions/0004-tech-stack-polyglot-go-python-typescript.md) for rationale and scaling strategies.

## Conventions

**Naming:**
- **Go:** CamelCase for exported, camelCase for unexported. Interfaces end in `-er` (e.g., `Orchestrator`, `Router`).
- **Python:** snake_case for functions/vars, PascalCase for classes. Pydantic models use `model_config` for validation.
- **TypeScript:** camelCase for functions/vars, PascalCase for types/interfaces/components.
- **Filenames:** lowercase with hyphens (e.g., `pod-registry.py`, `chat-handler.go`).

**Testing:**
- **Unit tests:** Every public function in `*_test.go` (Go) or `test_*.py` (Python). Target 70%+ coverage.
- **Integration tests:** Test orchestrator + pod interactions; mock external APIs (AWS, K8s, Claude).
- **E2E tests:** Full query flow from frontend → backend → pods → response. Run in staging environment.
- **Run tests before merge:** `make test` in each service directory.

**Logging:**
- **Format:** Structured JSON (CloudWatch-compatible). Levels: DEBUG, INFO, WARN, ERROR.
- **Go:** Use `log/slog` (Go 1.21+) with JSON handler.
- **Python:** Use `logging` with JSON formatter (or `structlog`).
- **React:** Console logs prefixed with `[agentify]`.
- **Always log:** request start/end (with latency), errors (with stack trace), state changes (integration added, pod split).
- **Never log:** secrets, API tokens, user PII. Use `***` masking if necessary.

**Error handling:**
- **Go:** explicit `if err != nil` checks; wrap errors with context (`fmt.Errorf("context: %w", err)`).
- **Python:** let exceptions propagate to FastAPI (returns 500); catch and log specific errors (e.g., K8s API timeout → log, return 503 "Service Unavailable").
- **Frontend:** catch promise rejections; show user-friendly error toast messages. Log to CloudWatch.
- **Never swallow errors:** if an error occurs, it must either be handled, logged, or returned to the user.

**Comments:**
- **Go:** start package comments with `// Package name:` and function comments with `// FunctionName does ...`.
- **Python:** use docstrings for functions and classes (one-liner for simple ones).
- **Comment the WHY, not the WHAT:** code is self-documenting; comment surprising design decisions, workarounds for bugs, or non-obvious invariants.
- **Example:** "// We cache queries for 1h because K8fy state is eventually consistent; shorter TTL creates thrashing."

**Configuration:**
- **Environment-based:** use `.env` (dev) or AWS Parameter Store (prod). Never hardcode secrets.
- **Go:** `Config` struct loaded from env at startup.
- **Python:** use `pydantic_settings.BaseSettings` for env-based config.
- **React:** `import.meta.env.VITE_*` for build-time vars; `.env.local` for dev.

**Deployment & Operations:**
- **All services containerized:** Docker images built and tagged per commit.
- **All deployments via Terraform:** no manual AWS console changes.
- **All configs externalized:** pod profiles, thresholds, integration settings → DynamoDB or Secrets Manager.
- **Health checks:** each service exposes `/health` endpoint (Go/Python); responds with service status + dependencies.
- **Graceful shutdown:** on SIGTERM, finish in-flight requests (up to 30s timeout) before exiting.

**Documentation:**
- **API:** OpenAPI/Swagger schema (auto-generated from Go handlers + Pydantic models).
- **Database:** schema diagram and ER relationships (in `docs/`).
- **Deployment:** `docs/DEPLOYMENT.md` — step-by-step AWS setup and runbooks.
- **Semantic memory:** `docs/SEMANTIC_MEMORY.md` — how diagnose conclusions are
  embedded (Voyage), stored (`incident_embeddings` + pgvector) and retrieved, why
  every failure mode is silent, and how to verify it is actually working.
- **Prompts:** `docs/PROMPT_LIFECYCLE.md` — prompts are data served from Langfuse,
  not code in the container. Editing `src/agent/k8fy/prompts.py` changes only the
  fallback; shipping a prompt change means publishing a candidate, gating it, and
  promoting a label. Read this before touching any prompt.
- **Code comments:** see above.

**Versioning & Releases:**
- **Semantic Versioning:** `MAJOR.MINOR.PATCH` (e.g., `0.1.0` for MVP, `1.0.0` for production).
- **Git commits:** conventional commits format (e.g., `feat: add K8fy health queries`, `fix: pod split race condition`).
- **Releases:** tag commit and build Docker images tagged with version.

See `ARCHITECTURE.md` for implementation details and code structure.
