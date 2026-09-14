package postgres

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"time"

	_ "github.com/lib/pq" // registers the "postgres" SQL driver
)

// k8sPodSuffix matches the two trailing hash segments K8s appends to pod names:
// {deployment}-{rs-hash(6-12 hex chars)}-{pod-hash(5 alphanum)}.
// Stripping them recovers the deployment name (e.g. payment-worker-68795899ff-kngf7 → payment-worker).
var k8sPodSuffix = regexp.MustCompile(`-[a-z0-9]{6,12}-[a-z0-9]{5}$`)

// DefaultTenantID is the tenant every row created before multi-tenancy
// (ADR 0022) existed is migrated onto — today's single deployment becomes
// "tenant #1". Referenced by both the ALTER TABLE ... DEFAULT migrations in
// initSchema and anything that needs to compare against "the legacy tenant."
const DefaultTenantID = "00000000-0000-0000-0000-000000000001"

// Client wraps a PostgreSQL connection. A single Client (one connection pool)
// backs both store families for the MVP (see ADR 0010):
//   - the append-only "relational" store (events/certs) — Client itself,
//   - the "kv"/current-state store — via Client.CurrentStateStore().
type Client struct {
	db     *sql.DB
	logger *slog.Logger
}

// pinUTC forces every pooled connection's session time zone to UTC.
//
// WHY THIS IS NECESSARY, not cosmetic. Every timestamp column in this schema is
// `TIMESTAMP` — naive, no zone — and is written with `NOW()`, which yields the
// SERVER's local time. lib/pq then hands the value back to Go labelled UTC. The
// two only agree when the server's zone IS UTC, and nothing was enforcing that:
//
//   - AWS RDS defaults to UTC, so production has always been correct by luck;
//   - the embedded-postgres test instance inherits the host zone, so on an
//     AEST machine every timestamp read back ten hours in the future.
//
// The consequence is not a cosmetic offset. Any freshness comparison —
// `Date.now() - last_seen` for edge staleness, `observed_at` for the live-state
// banner — is a subtraction between a real clock and a mislabelled one, so on a
// non-UTC deployment the panel would declare everything permanently stale (or,
// with the sign the other way, permanently fresh, which is worse). Pinning the
// session zone makes the assumption the schema already depends on explicit, and
// is a no-op wherever the server is already UTC.
//
// Done per connection via the DSN rather than as `ALTER DATABASE`: it needs no
// privileges beyond connecting, applies to every connection in the pool, and is
// reverted by deleting this function. A caller that sets its own timezone wins.
func pinUTC(connStr string) string {
	if strings.Contains(strings.ToLower(connStr), "timezone=") {
		return connStr // caller was explicit; do not override
	}
	// The two DSN forms lib/pq accepts separate parameters differently.
	if strings.HasPrefix(connStr, "postgres://") || strings.HasPrefix(connStr, "postgresql://") {
		if strings.Contains(connStr, "?") {
			return connStr + "&timezone=UTC"
		}
		return connStr + "?timezone=UTC"
	}
	if strings.TrimSpace(connStr) == "" {
		return "timezone=UTC"
	}
	return connStr + " timezone=UTC"
}

// NewClient opens the connection and initializes both schemas.
//
// ctx controls how long to wait for the initial Postgres ping. Pass a context
// with a generous deadline in production (e.g. 3 minutes) so the backend
// survives the 30–60 s window where AWS RDS is marked "available" but not yet
// accepting TCP connections — the common cause of both the namespace autocomplete
// and Query History being empty after a scale-up / resume cycle.
//
// Pass a context with a short deadline (e.g. 5 s) in tests so a missing Postgres
// instance returns an error quickly and the test can call t.Skip().
func NewClient(ctx context.Context, connStr string, logger *slog.Logger) (*Client, error) {
	db, err := sql.Open("postgres", pinUTC(connStr))
	if err != nil {
		return nil, fmt.Errorf("failed to open postgres: %w", err)
	}

	// Retry the initial ping at 15 s intervals until the context is cancelled.
	// Each failure is logged at WARN so the cause is visible in CloudWatch.
	const retryInterval = 15 * time.Second
	var pingErr error
	for attempt := 1; ; attempt++ {
		if pingErr = db.PingContext(ctx); pingErr == nil {
			break
		}
		// If the context deadline was exceeded or cancelled, give up immediately.
		if ctx.Err() != nil {
			_ = db.Close()
			return nil, fmt.Errorf("postgres unavailable (context: %w): %v", ctx.Err(), pingErr)
		}
		logger.Warn("postgres not ready, will retry",
			"attempt", attempt, "error", pingErr)
		select {
		case <-ctx.Done():
			_ = db.Close()
			return nil, fmt.Errorf("postgres unavailable (context: %w): %v", ctx.Err(), pingErr)
		case <-time.After(retryInterval):
		}
	}

	c := &Client{db: db, logger: logger}
	if err := c.initSchema(context.Background()); err != nil {
		return nil, fmt.Errorf("failed to initialize schema: %w", err)
	}

	logger.Info("postgres client initialized")
	return c, nil
}

// initSchema creates all tables and extensions if they don't exist.
func (c *Client) initSchema(ctx context.Context) error {
	schema := `
	-- pgvector: enable vector similarity search (P8 — semantic memory layer).
	-- Wrapped in DO $$ so the error is swallowed when the OS package is absent
	-- (e.g. embedded-postgres in CI tests which don't ship pgvector).
	DO $$ BEGIN
		CREATE EXTENSION IF NOT EXISTS vector;
	EXCEPTION WHEN OTHERS THEN NULL; END $$;

	-- Append-only events/certs (relational store).
	CREATE TABLE IF NOT EXISTS events (
		id UUID PRIMARY KEY,
		pod_id TEXT NOT NULL,
		event_namespace TEXT NOT NULL,
		event_type TEXT NOT NULL,
		timestamp TIMESTAMP NOT NULL,
		payload JSONB NOT NULL,
		created_at TIMESTAMP DEFAULT NOW()
	);
	CREATE INDEX IF NOT EXISTS idx_events_pod_id ON events(pod_id);
	CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC);
	CREATE INDEX IF NOT EXISTS idx_events_namespace ON events(event_namespace);

	-- Current-state snapshots (kv store): latest value per (pod_id, entity_key).
	CREATE TABLE IF NOT EXISTS current_state (
		pod_id TEXT NOT NULL,
		entity_key TEXT NOT NULL,
		event_namespace TEXT,
		event_type TEXT,
		source TEXT,
		payload JSONB NOT NULL,
		updated_at TIMESTAMP DEFAULT NOW(),
		PRIMARY KEY (pod_id, entity_key)
	);
	CREATE INDEX IF NOT EXISTS idx_current_state_pod ON current_state(pod_id);

	-- Query traces: persisted per-query provenance records (spec 004).
	CREATE TABLE IF NOT EXISTS traces (
		id TEXT PRIMARY KEY,
		trace_id TEXT NOT NULL,
		question TEXT NOT NULL,
		intent TEXT NOT NULL DEFAULT '',
		namespace TEXT NOT NULL DEFAULT '',
		tier TEXT NOT NULL DEFAULT '',
		status TEXT NOT NULL DEFAULT '',
		confidence FLOAT NOT NULL DEFAULT 0,
		sources JSONB NOT NULL DEFAULT '[]',
		tool_calls JSONB NOT NULL DEFAULT '[]',
		latency_ms BIGINT NOT NULL DEFAULT 0,
		started_at TIMESTAMP,
		created_at TIMESTAMP DEFAULT NOW(),
		input_tokens BIGINT NOT NULL DEFAULT 0,
		output_tokens BIGINT NOT NULL DEFAULT 0,
		estimated_cost_usd FLOAT NOT NULL DEFAULT 0,
		-- Which Langfuse prompt version produced this answer. prompt_version is
		-- NULL for Tier-1 (no LLM call) and when the agent used its local
		-- fallback string, so a proposed prompt fix can tell "this version
		-- produced this failure" from "there was no version".
		prompt_name TEXT NOT NULL DEFAULT '',
		prompt_version INT,
		-- Which conversation this turn belongs to, so a whole conversation can be
		-- evaluated rather than isolated turns. Empty for single-shot /api/query;
		-- set to the chat session id for chat turns. Without it, evidence (this
		-- table) and conversation text (chat_sessions.messages) cannot be joined
		-- — see ROADMAP P19 gap F.
		session_id TEXT NOT NULL DEFAULT '',
		-- Produced by /admin/eval/query rather than by real traffic (ADR 0030).
		-- Eval runs go through the real deployed path on purpose, so their traces
		-- are production-shaped; without this flag P19's sampler would judge the
		-- system's own synthetic test traffic and partly learn from its own test
		-- set. Every consumer of this table must filter on it.
		is_eval BOOLEAN NOT NULL DEFAULT FALSE
	);
	-- Idempotent migrations: add new columns to pre-existing tables.
	ALTER TABLE IF EXISTS traces ADD COLUMN IF NOT EXISTS started_at TIMESTAMP;
	ALTER TABLE IF EXISTS traces ADD COLUMN IF NOT EXISTS input_tokens BIGINT NOT NULL DEFAULT 0;
	ALTER TABLE IF EXISTS traces ADD COLUMN IF NOT EXISTS output_tokens BIGINT NOT NULL DEFAULT 0;
	ALTER TABLE IF EXISTS traces ADD COLUMN IF NOT EXISTS estimated_cost_usd FLOAT NOT NULL DEFAULT 0;
	ALTER TABLE IF EXISTS traces ADD COLUMN IF NOT EXISTS prompt_name TEXT NOT NULL DEFAULT '';
	ALTER TABLE IF EXISTS traces ADD COLUMN IF NOT EXISTS prompt_version INT;
	ALTER TABLE IF EXISTS traces ADD COLUMN IF NOT EXISTS session_id TEXT NOT NULL DEFAULT '';
	ALTER TABLE IF EXISTS traces ADD COLUMN IF NOT EXISTS is_eval BOOLEAN NOT NULL DEFAULT FALSE;
	CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id, created_at)
		WHERE session_id <> '';
	ALTER TABLE IF EXISTS traces ADD COLUMN IF NOT EXISTS cache_creation_input_tokens BIGINT NOT NULL DEFAULT 0;
	ALTER TABLE IF EXISTS traces ADD COLUMN IF NOT EXISTS cache_read_input_tokens BIGINT NOT NULL DEFAULT 0;
	CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at DESC);
	CREATE INDEX IF NOT EXISTS idx_traces_intent  ON traces(intent);

	-- Admin integrations: configured K8s fleet-collector connections.
	CREATE TABLE IF NOT EXISTS integrations (
		id TEXT PRIMARY KEY,
		name TEXT NOT NULL,
		namespaces JSONB NOT NULL DEFAULT '[]',
		status TEXT NOT NULL DEFAULT 'inactive',
		token TEXT NOT NULL DEFAULT '',
		created_at TIMESTAMP DEFAULT NOW(),
		updated_at TIMESTAMP DEFAULT NOW()
	);
	-- adapter_url: dropped — was the Hub's outbound URL for calling the
	-- retired k8fy-adapter's log-server directly (ADR 0027 retired that
	-- whole path; live_get_pod_logs/agentify-discovery's relay replaced it).
	-- Nothing has read this column since.
	ALTER TABLE IF EXISTS integrations DROP COLUMN IF EXISTS adapter_url;
	-- token_secret_arn (ADR 0025): when set, the real credential lives in AWS
	-- Secrets Manager and the token column is left empty; when unset, token
	-- still carries the plaintext value (today's behavior, unchanged).
	ALTER TABLE IF EXISTS integrations ADD COLUMN IF NOT EXISTS token_secret_arn TEXT NOT NULL DEFAULT '';
	-- Multi-turn chat sessions: persists conversation history across pod restarts.
	CREATE TABLE IF NOT EXISTS chat_sessions (
		id                 TEXT PRIMARY KEY,
		title              TEXT NOT NULL DEFAULT '',
		namespace          TEXT NOT NULL DEFAULT '',
		service            TEXT NOT NULL DEFAULT '',
		messages           JSONB NOT NULL DEFAULT '[]',
		context_cache      JSONB NOT NULL DEFAULT '{}',
		context_fetched_at TIMESTAMP,
		created_at         TIMESTAMP DEFAULT NOW(),
		last_active        TIMESTAMP DEFAULT NOW(),
		expires_at         TIMESTAMP
	);
	CREATE INDEX IF NOT EXISTS idx_chat_sessions_active ON chat_sessions(last_active DESC);

	-- Model pricing: retail $/MTok rates shown in Admin UI and used for trace cost estimates.
	-- Seeded with Anthropic list prices (June 2026). cache_write_per_mtok = 5-min TTL rate.
	CREATE TABLE IF NOT EXISTS model_pricing (
		model_id             TEXT PRIMARY KEY,
		display_name         TEXT NOT NULL DEFAULT '',
		input_per_mtok       FLOAT NOT NULL DEFAULT 0,
		output_per_mtok      FLOAT NOT NULL DEFAULT 0,
		cache_write_per_mtok FLOAT NOT NULL DEFAULT 0,
		cache_read_per_mtok  FLOAT NOT NULL DEFAULT 0,
		updated_at           TIMESTAMP DEFAULT NOW()
	);
	INSERT INTO model_pricing (model_id, display_name, input_per_mtok, output_per_mtok, cache_write_per_mtok, cache_read_per_mtok) VALUES
		('claude-fable-5',    'Claude Fable 5',    10.0, 50.0, 12.50, 1.00),
		('claude-mythos-5',   'Claude Mythos 5',   10.0, 50.0, 12.50, 1.00),
		('claude-opus-4-8',   'Claude Opus 4.8',    5.0, 25.0,  6.25, 0.50),
		('claude-opus-4-7',   'Claude Opus 4.7',    5.0, 25.0,  6.25, 0.50),
		('claude-opus-4-6',   'Claude Opus 4.6',    5.0, 25.0,  6.25, 0.50),
		('claude-opus-4-5',   'Claude Opus 4.5',    5.0, 25.0,  6.25, 0.50),
		('claude-sonnet-4-6', 'Claude Sonnet 4.6',  3.0, 15.0,  3.75, 0.30),
		('claude-sonnet-4-5', 'Claude Sonnet 4.5',  3.0, 15.0,  3.75, 0.30),
		('claude-haiku-4-5',  'Claude Haiku 4.5',   1.0,  5.0,  1.25, 0.10),
		('claude-haiku-3-5',  'Claude Haiku 3.5',   0.8,  4.0,  1.00, 0.08)
	ON CONFLICT (model_id) DO NOTHING;

	-- Semantic memory base table (always created — no pgvector type here).
	-- The embedding column is added below only when pgvector is available.
	CREATE TABLE IF NOT EXISTS incident_embeddings (
		id          TEXT PRIMARY KEY,
		trace_id    TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
		namespace   TEXT NOT NULL DEFAULT '',
		service     TEXT NOT NULL DEFAULT '',
		summary     TEXT NOT NULL DEFAULT '',
		created_at  TIMESTAMP DEFAULT NOW()
	);
	CREATE INDEX IF NOT EXISTS idx_incident_embeddings_ns_svc
		ON incident_embeddings (namespace, service);

	-- Remediation proposals (ADR 0020 / spec 011 Use Cases 1+2): every Phase-3
	-- write action (restart/scale/rollback) is proposed here first and only
	-- executed after an explicit human approval — never auto-executed.
	CREATE TABLE IF NOT EXISTS remediation_proposals (
		id               TEXT PRIMARY KEY,
		trace_id         TEXT NOT NULL DEFAULT '',
		use_case         TEXT NOT NULL,                  -- incident_responder | deployment_guardian
		namespace        TEXT NOT NULL DEFAULT '',
		service          TEXT NOT NULL DEFAULT '',
		proposed_action  TEXT NOT NULL,                  -- restart_deployment | scale_deployment | rollback_deployment | rotate_cert | human_escalation
		action_params    JSONB NOT NULL DEFAULT '{}',
		analysis         JSONB NOT NULL DEFAULT '{}',     -- evidence, reasoning, blast_radius, confidence
		status           TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected | executed | failed | expired
		source_event_id  TEXT NOT NULL DEFAULT '',        -- dedupes deploy-guardian checks against k8fy.events
		created_at       TIMESTAMP DEFAULT NOW(),
		expires_at       TIMESTAMP NOT NULL,
		decided_at       TIMESTAMP,
		decided_by       TEXT NOT NULL DEFAULT '',
		executed_at      TIMESTAMP,
		result           JSONB NOT NULL DEFAULT '{}',
		error            TEXT NOT NULL DEFAULT ''
	);
	CREATE INDEX IF NOT EXISTS idx_remediation_status  ON remediation_proposals(status);
	CREATE INDEX IF NOT EXISTS idx_remediation_created  ON remediation_proposals(created_at DESC);
	CREATE UNIQUE INDEX IF NOT EXISTS idx_remediation_source_event
		ON remediation_proposals(source_event_id) WHERE source_event_id != '';

	-- Service dependency edges, mined from log text (agent-side, see
	-- k8fy/service_topology.py) — structured facts extracted from logs, not
	-- raw log text itself, so this doesn't revisit ADR 0014's "logs are
	-- ephemeral, never persisted" (same category of exception as
	-- k8fy.events/k8fy.metrics/incident_embeddings above).
	CREATE TABLE IF NOT EXISTS service_dependencies (
		id             TEXT PRIMARY KEY,
		namespace      TEXT NOT NULL,
		from_service   TEXT NOT NULL,
		to_service     TEXT NOT NULL,
		evidence_count INT NOT NULL DEFAULT 1,
		first_seen     TIMESTAMP DEFAULT NOW(),
		last_seen      TIMESTAMP DEFAULT NOW(),
		UNIQUE (namespace, from_service, to_service)
	);
	CREATE INDEX IF NOT EXISTS idx_service_deps_namespace ON service_dependencies(namespace);

	-- ROADMAP P27 phase 3: what KIND of thing the target is, and therefore how
	-- much the edge can be trusted.
	--
	--   service          an in-namespace Service, validated against the live
	--                    Service list. Strong evidence.
	--   cross_namespace  <service>.<namespace> where the namespace is one the
	--                    Hub tracks. Validated on the namespace only.
	--   external         a public hostname. NOT validated against anything —
	--                    no Service list exists for the internet, so this tier
	--                    rests entirely on hostname-shape heuristics.
	--
	-- Defaulting to 'service' keeps every existing row correct: they were all
	-- produced by the validated in-namespace path.
	--
	-- The unique key deliberately does NOT include this column. An external
	-- target is a dotted hostname and a cross-namespace one is "svc.ns";
	-- neither can collide with a bare Service name, so no key change is needed
	-- and no existing row moves.
	ALTER TABLE IF EXISTS service_dependencies ADD COLUMN IF NOT EXISTS target_kind TEXT NOT NULL DEFAULT 'service';

	-- Multi-tenancy (ADR 0022, reverses ADR 0009): tenant_id + cluster_id on
	-- every table holding per-customer operational data. Existing rows
	-- migrate onto DefaultTenantID ("tenant #1") automatically via the
	-- column default; cluster_id has no default (NULL) — unlike tenant,
	-- there's no obvious "default cluster" for today's single deployment to
	-- collapse onto. model_pricing is deliberately excluded — shared
	-- reference data, identical for every tenant. current_state/events are
	-- deliberately NOT touched here — they're generic map-based stores with
	-- no per-row Go struct; giving them tenant_id is a query-retrofit-phase
	-- decision, not a schema-only one.
	ALTER TABLE IF EXISTS integrations          ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
	ALTER TABLE IF EXISTS integrations          ADD COLUMN IF NOT EXISTS cluster_id TEXT;
	ALTER TABLE IF EXISTS chat_sessions         ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
	ALTER TABLE IF EXISTS chat_sessions         ADD COLUMN IF NOT EXISTS cluster_id TEXT;
	ALTER TABLE IF EXISTS remediation_proposals ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
	ALTER TABLE IF EXISTS remediation_proposals ADD COLUMN IF NOT EXISTS cluster_id TEXT;
	ALTER TABLE IF EXISTS incident_embeddings   ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
	ALTER TABLE IF EXISTS incident_embeddings   ADD COLUMN IF NOT EXISTS cluster_id TEXT;
	ALTER TABLE IF EXISTS traces                ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
	ALTER TABLE IF EXISTS traces                ADD COLUMN IF NOT EXISTS cluster_id TEXT;
	ALTER TABLE IF EXISTS current_state         ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
	ALTER TABLE IF EXISTS current_state         ADD COLUMN IF NOT EXISTS cluster_id TEXT;
	ALTER TABLE IF EXISTS events                ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
	ALTER TABLE IF EXISTS events                ADD COLUMN IF NOT EXISTS cluster_id TEXT;
	ALTER TABLE IF EXISTS service_dependencies  ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001';
	ALTER TABLE IF EXISTS service_dependencies  ADD COLUMN IF NOT EXISTS cluster_id TEXT;

	-- Multi-tenancy phase 2 (ADR 0022 Decisions #1/#5): service_dependencies
	-- is the first genuinely tenant-scoped, RLS-enforced table. collector_token
	-- is a SEPARATE credential from the existing outbound "token" column —
	-- token is this backend calling OUT to an adapter; collector_token is a
	-- collector calling IN to this backend. Conflating both directions on one
	-- field would mean a leaked outbound token also grants inbound push access.
	ALTER TABLE IF EXISTS integrations ADD COLUMN IF NOT EXISTS collector_token TEXT NOT NULL DEFAULT '';

	-- The original UNIQUE (namespace, from_service, to_service) constraint
	-- predates tenant_id/cluster_id and would silently merge two different
	-- tenants' (or two clusters' within one tenant's fleet) evidence under
	-- ON CONFLICT -- a real cross-tenant data-corruption bug, not just a
	-- read-isolation gap. Backfill cluster_id to '' first (never leave it
	-- NULL going forward) since Postgres treats every NULL as distinct from
	-- every other NULL in a UNIQUE constraint -- an unconstrained NULL would
	-- silently defeat the new constraint below for exactly the callers (no
	-- credential presented) this phase cares most about getting right.
	UPDATE service_dependencies SET cluster_id = '' WHERE cluster_id IS NULL;
	ALTER TABLE IF EXISTS service_dependencies ALTER COLUMN cluster_id SET DEFAULT '';
	-- BUG FOUND LIVE (2026-09-12): this block and the port-based one below
	-- (ROADMAP P27 phase 2 / ADR 0031) each only recognized their OWN target
	-- constraint name as "already migrated". Once the port-based migration
	-- ran once and renamed the constraint, THIS block ran again on the next
	-- pod start, saw a name it didn't recognize, dropped it, and tried to
	-- recreate the narrower 5-column constraint -- which then failed outright
	-- (a real unique-index violation, since real data already had multiple
	-- rows differing only by port) and took the WHOLE Postgres connection
	-- down with it (buildBackendFactory treats a schema-init failure as
	-- total relational+kv unavailability, not just this table). The two
	-- migrations must each recognize the OTHER's target name as an
	-- acceptable already-done state, not just their own.
	DO $$
	DECLARE
		old_constraint_name TEXT;
	BEGIN
		SELECT conname INTO old_constraint_name
		FROM pg_constraint
		WHERE conrelid = 'service_dependencies'::regclass
		  AND contype = 'u'
		  AND conname NOT IN (
		      'service_dependencies_tenant_cluster_ns_svc_key',
		      'service_dependencies_tenant_cluster_ns_svc_port_key'
		  );
		IF old_constraint_name IS NOT NULL THEN
			EXECUTE format('ALTER TABLE service_dependencies DROP CONSTRAINT %I', old_constraint_name);
		END IF;
		IF NOT EXISTS (
			SELECT 1 FROM pg_constraint WHERE conname IN (
			    'service_dependencies_tenant_cluster_ns_svc_key',
			    'service_dependencies_tenant_cluster_ns_svc_port_key'
			)
		) THEN
			ALTER TABLE service_dependencies ADD CONSTRAINT service_dependencies_tenant_cluster_ns_svc_key
				UNIQUE (tenant_id, cluster_id, namespace, from_service, to_service);
		END IF;
	END $$;

	ALTER TABLE IF EXISTS service_dependencies ENABLE ROW LEVEL SECURITY;
	-- FORCE is critical: without it, Postgres exempts the table OWNER role
	-- from RLS, and this app almost certainly connects as the owner —
	-- omitting FORCE would make the policy below a silent no-op.
	ALTER TABLE IF EXISTS service_dependencies FORCE ROW LEVEL SECURITY;

	-- CREATE POLICY has no IF NOT EXISTS — idempotency via pg_policies, same
	-- style as the pgvector block below. Unlike that block, no exception-
	-- swallowing here: RLS is core Postgres, always available, so a real
	-- failure here should surface loudly, not be silently ignored.
	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_policies
			WHERE tablename = 'service_dependencies' AND policyname = 'tenant_isolation'
		) THEN
			EXECUTE 'CREATE POLICY tenant_isolation ON service_dependencies
				USING (tenant_id = current_setting(''app.current_tenant_id'', true))';
		END IF;
	END $$;

	-- ROADMAP P27 phase 2 (ADR 0031): outcome and port capture. port is a
	-- sentinel INT (0 = "not captured"), never NULL -- Postgres treats every
	-- NULL as distinct from every other NULL in a UNIQUE constraint, exactly
	-- the bug backfilling cluster_id above already worked around once; NULL
	-- here would silently defeat evidence accumulation for every edge whose
	-- port was never observed. Real ports are 1-65535, so 0 is unambiguous.
	ALTER TABLE IF EXISTS service_dependencies ADD COLUMN IF NOT EXISTS port INT NOT NULL DEFAULT 0;
	ALTER TABLE IF EXISTS service_dependencies ADD COLUMN IF NOT EXISTS outcome_success_count INT NOT NULL DEFAULT 0;
	ALTER TABLE IF EXISTS service_dependencies ADD COLUMN IF NOT EXISTS outcome_failure_count INT NOT NULL DEFAULT 0;
	ALTER TABLE IF EXISTS service_dependencies ADD COLUMN IF NOT EXISTS outcome_timeout_count INT NOT NULL DEFAULT 0;
	-- No separate "unknown" counter: it is evidence_count minus the three
	-- above, derivable rather than stored.

	-- Splitting rows by port (ROADMAP P27's "one schema decision"):
	-- aggregating every port into one edge would lose exactly the field P24
	-- (Policy Synthesis) needs -- a NetworkPolicy with no port can only say
	-- "allow all ports". Same drop-and-recreate pattern as the tenant/cluster
	-- constraint migration above, generalized the same way: find whatever
	-- unique constraint currently exists on this table and replace it, so
	-- this runs correctly whether it follows the migration above or (on a
	-- fresh database) runs right after CREATE TABLE.
	DO $$
	DECLARE
		old_constraint_name TEXT;
	BEGIN
		SELECT conname INTO old_constraint_name
		FROM pg_constraint
		WHERE conrelid = 'service_dependencies'::regclass
		  AND contype = 'u'
		  AND conname != 'service_dependencies_tenant_cluster_ns_svc_port_key';
		IF old_constraint_name IS NOT NULL THEN
			EXECUTE format('ALTER TABLE service_dependencies DROP CONSTRAINT %I', old_constraint_name);
		END IF;
		IF NOT EXISTS (
			SELECT 1 FROM pg_constraint WHERE conname = 'service_dependencies_tenant_cluster_ns_svc_port_key'
		) THEN
			ALTER TABLE service_dependencies ADD CONSTRAINT service_dependencies_tenant_cluster_ns_svc_port_key
				UNIQUE (tenant_id, cluster_id, namespace, from_service, to_service, port);
		END IF;
	END $$;

	-- Service->cluster registry (ROADMAP P16 / ADR 0023): which cluster(s)
	-- run a given (namespace, service), populated deterministically by
	-- agentify-discovery's inventory push (POST /api/cluster-inventory) —
	-- same point-lookup + current-state + derived-authority shape as
	-- Integration/current_state (storage-strategy.md), so a small Postgres
	-- table, not a new store engine. Full delete-then-insert per push per
	-- (tenant, cluster) — same "full replace reflects live truth" semantics
	-- UpdateIntegrationNamespaces already uses.
	CREATE TABLE IF NOT EXISTS cluster_services (
		tenant_id  TEXT NOT NULL,
		cluster_id TEXT NOT NULL,
		namespace  TEXT NOT NULL,
		service    TEXT NOT NULL,
		updated_at TIMESTAMP DEFAULT NOW(),
		PRIMARY KEY (tenant_id, cluster_id, namespace, service)
	);
	CREATE INDEX IF NOT EXISTS idx_cluster_services_lookup ON cluster_services(tenant_id, namespace, service);

	-- Service profile (ROADMAP P22): what each service IS, alongside the
	-- selector that says which pods it fronts. Every column is populated from
	-- data the collector already fetched and discarded — list_services returned
	-- spec.type and spec.ports and kept only the selector, and the workload list
	-- was reduced to a boolean. No new K8s call, no new RBAC.
	--
	-- Replica counts are nullable on purpose: a CronJob has none, and "0
	-- replicas" (scaled to zero, a real finding) must not be stored the same as
	-- "we do not know".
	ALTER TABLE IF EXISTS cluster_services ADD COLUMN IF NOT EXISTS service_type     TEXT NOT NULL DEFAULT '';
	ALTER TABLE IF EXISTS cluster_services ADD COLUMN IF NOT EXISTS ports            JSONB NOT NULL DEFAULT '[]'::jsonb;
	ALTER TABLE IF EXISTS cluster_services ADD COLUMN IF NOT EXISTS workload_kind    TEXT NOT NULL DEFAULT '';
	ALTER TABLE IF EXISTS cluster_services ADD COLUMN IF NOT EXISTS replicas_desired INT;
	ALTER TABLE IF EXISTS cluster_services ADD COLUMN IF NOT EXISTS replicas_ready   INT;
	ALTER TABLE IF EXISTS cluster_services ADD COLUMN IF NOT EXISTS image            TEXT NOT NULL DEFAULT '';
	ALTER TABLE IF EXISTS cluster_services ADD COLUMN IF NOT EXISTS schedule         TEXT NOT NULL DEFAULT '';
	-- ADR 0029 (P18 use case #2's Glue extension): each Service's K8s
	-- selector, alongside its name. Discovery already fetches this on every
	-- scan (main.py's list_services, used for its own live from_service
	-- matching) — this just stops discarding it after that check, same
	-- rationale namespaceInventory's comment already gives for Services
	-- themselves. Lets a centralized Glue-based miner (which has no live
	-- cluster access) replicate the same selector-to-pod-label matching
	-- against stored data instead of a live K8s read.
	ALTER TABLE IF EXISTS cluster_services ADD COLUMN IF NOT EXISTS selector JSONB NOT NULL DEFAULT '{}'::jsonb;
	-- ADR 0032: an operator-set K8s annotation (agentify.io/expected-failure)
	-- on the Service, read by discovery's existing list_services scan and
	-- carried through this same push — no new pipe. Non-empty means "calls to
	-- this service are expected to fail/be unreachable by design"; joined
	-- into ListServiceDependencies by (tenant_id, cluster_id, namespace,
	-- service = to_service) so it suppresses only the health-alert banner,
	-- never the underlying color/counts. Empty on the next push clears it
	-- (same full-replace-per-cycle semantics as every other column here) —
	-- removing the annotation un-suppresses the edge on the next scan.
	ALTER TABLE IF EXISTS cluster_services ADD COLUMN IF NOT EXISTS expected_failure_reason TEXT NOT NULL DEFAULT '';

	ALTER TABLE IF EXISTS cluster_services ENABLE ROW LEVEL SECURITY;
	ALTER TABLE IF EXISTS cluster_services FORCE ROW LEVEL SECURITY;

	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_policies
			WHERE tablename = 'cluster_services' AND policyname = 'tenant_isolation'
		) THEN
			EXECUTE 'CREATE POLICY tenant_isolation ON cluster_services
				USING (tenant_id = current_setting(''app.current_tenant_id'', true))';
		END IF;
	END $$;

	-- Ingress/entry-point mapping (ROADMAP P18 use case #3): where traffic
	-- into this cluster actually enters, per agentify-discovery's Ingress /
	-- Gateway+HTTPRoute / OpenShift Route scan. Same flat-row,
	-- full-delete-then-insert-per-push shape as cluster_services above --
	-- kind distinguishes which K8s object produced a given row ("ingress" |
	-- "httproute" | "route"); host/backend_service may each be empty when
	-- only one side of a mapping is known (still recorded, not dropped).
	CREATE TABLE IF NOT EXISTS cluster_ingress_endpoints (
		tenant_id       TEXT NOT NULL,
		cluster_id      TEXT NOT NULL,
		namespace       TEXT NOT NULL,
		kind            TEXT NOT NULL,
		name            TEXT NOT NULL,
		host            TEXT NOT NULL DEFAULT '',
		backend_service TEXT NOT NULL DEFAULT '',
		updated_at      TIMESTAMP DEFAULT NOW(),
		PRIMARY KEY (tenant_id, cluster_id, namespace, kind, name, host, backend_service)
	);
	CREATE INDEX IF NOT EXISTS idx_cluster_ingress_lookup ON cluster_ingress_endpoints(tenant_id, namespace, backend_service);

	ALTER TABLE IF EXISTS cluster_ingress_endpoints ENABLE ROW LEVEL SECURITY;
	ALTER TABLE IF EXISTS cluster_ingress_endpoints FORCE ROW LEVEL SECURITY;

	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_policies
			WHERE tablename = 'cluster_ingress_endpoints' AND policyname = 'tenant_isolation'
		) THEN
			EXECUTE 'CREATE POLICY tenant_isolation ON cluster_ingress_endpoints
				USING (tenant_id = current_setting(''app.current_tenant_id'', true))';
		END IF;
	END $$;

	-- Fleet-wide health/version snapshot (ROADMAP P18 use case #5): one row
	-- per cluster, overwritten in place on every push (not a delete-then-
	-- insert row set like cluster_services/cluster_ingress_endpoints above —
	-- cluster_id is already a globally-unique Integration.ID, so it alone is
	-- the primary key). Capacity (node count/allocatable CPU-mem) is
	-- deliberately not part of this snapshot yet — needs a new nodes RBAC
	-- grant, flagged as a follow-up, not built here.
	-- ROADMAP P27 phase 1: the DENOMINATOR for dependency-graph coverage.
	--
	-- service_dependencies.evidence_count is a numerator with no denominator:
	-- "seen 51 times" cannot distinguish a service that is called rarely from
	-- one whose logs are unreadable from one whose pods are never among the
	-- MAX_PODS_PER_NAMESPACE sampled. That ambiguity made payment-worker's
	-- decline uninterpretable on 2026-09-03 (5% -> 1.8% of scans).
	--
	-- One row per (tenant, cluster, namespace, service), counters incremented
	-- per scan cycle, mirroring service_dependencies' upsert semantics. Coverage
	-- is then evidence_count / pods_sampled for that from_service.
	CREATE TABLE IF NOT EXISTS scan_coverage (
		tenant_id      TEXT NOT NULL,
		cluster_id     TEXT NOT NULL DEFAULT '',
		namespace      TEXT NOT NULL,
		service        TEXT NOT NULL,
		scan_cycles    INT NOT NULL DEFAULT 0,  -- cycles in which this namespace was scanned
		pods_seen      INT NOT NULL DEFAULT 0,  -- pods attributed to this service across cycles
		pods_sampled   INT NOT NULL DEFAULT 0,  -- of those, how many we actually read logs for
		logs_readable  INT NOT NULL DEFAULT 0,  -- of those, how many returned any log text
		log_lines      BIGINT NOT NULL DEFAULT 0,
		first_scan     TIMESTAMP DEFAULT NOW(),
		last_scan      TIMESTAMP DEFAULT NOW(),
		PRIMARY KEY (tenant_id, cluster_id, namespace, service)
	);
	CREATE INDEX IF NOT EXISTS idx_scan_coverage_lookup ON scan_coverage(tenant_id, namespace);

	ALTER TABLE IF EXISTS scan_coverage ENABLE ROW LEVEL SECURITY;
	ALTER TABLE IF EXISTS scan_coverage FORCE ROW LEVEL SECURITY;
	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_policies
			WHERE tablename = 'scan_coverage' AND policyname = 'tenant_isolation'
		) THEN
			EXECUTE 'CREATE POLICY tenant_isolation ON scan_coverage
				USING (tenant_id = current_setting(''app.current_tenant_id'', true))';
		END IF;
	END $$;

	CREATE TABLE IF NOT EXISTS cluster_health_snapshots (
		cluster_id  TEXT PRIMARY KEY,
		tenant_id   TEXT NOT NULL,
		k8s_version TEXT NOT NULL DEFAULT '',
		pods_total  INT NOT NULL DEFAULT 0,
		pods_ready  INT NOT NULL DEFAULT 0,
		updated_at  TIMESTAMP DEFAULT NOW()
	);
	CREATE INDEX IF NOT EXISTS idx_cluster_health_snapshots_tenant ON cluster_health_snapshots(tenant_id);

	ALTER TABLE IF EXISTS cluster_health_snapshots ENABLE ROW LEVEL SECURITY;
	ALTER TABLE IF EXISTS cluster_health_snapshots FORCE ROW LEVEL SECURITY;

	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_policies
			WHERE tablename = 'cluster_health_snapshots' AND policyname = 'tenant_isolation'
		) THEN
			EXECUTE 'CREATE POLICY tenant_isolation ON cluster_health_snapshots
				USING (tenant_id = current_setting(''app.current_tenant_id'', true))';
		END IF;
	END $$;

	-- ROADMAP OPS-10 (ADR 0022 amendment, 2026-09-13): current_state was
	-- listed in ADR 0022's own Decision #2 as a table that should get
	-- tenant_id + RLS, but the RLS half was deferred as "a query-retrofit-
	-- phase decision" and never completed. Closing it required first
	-- threading tenant resolution through every current_state reader/writer
	-- (CurrentState.Store/Query/TrackedEntities, Client.ListServiceHealth,
	-- and the /api/query and /api/agent/fetch handlers above them) — see the
	-- amendment for why this was a bigger retrofit than the one-line policy
	-- below suggests.
	ALTER TABLE IF EXISTS current_state ENABLE ROW LEVEL SECURITY;
	ALTER TABLE IF EXISTS current_state FORCE ROW LEVEL SECURITY;

	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_policies
			WHERE tablename = 'current_state' AND policyname = 'tenant_isolation'
		) THEN
			EXECUTE 'CREATE POLICY tenant_isolation ON current_state
				USING (tenant_id = current_setting(''app.current_tenant_id'', true))';
		END IF;
	END $$;

	-- Deployment security posture (ROADMAP P30 phase 1, ADR 0033). One row
	-- per (check, resource) — config-state, not accumulated evidence, so
	-- this follows cluster_services' "full replace reflects live truth"
	-- family: UpsertSecurityFindings upserts everything a scan cycle found
	-- and flips anything NOT in that push from 'open' to 'resolved' (never
	-- deletes, so history survives a fixed issue). confidence and
	-- verified_by_engagement_id are ADR 0033 Phase 2+ columns, inert here
	-- (always 'config-only'/NULL) — shipped now so Phase 2 is an additive
	-- migration, not an OPS-10-shaped retrofit onto a schema that never
	-- anticipated verification.
	CREATE TABLE IF NOT EXISTS security_findings (
		tenant_id     TEXT NOT NULL,
		cluster_id    TEXT NOT NULL DEFAULT '',
		namespace     TEXT NOT NULL,
		check_id      TEXT NOT NULL,
		resource_kind TEXT NOT NULL,
		resource_name TEXT NOT NULL,
		severity      TEXT NOT NULL,
		evidence      TEXT NOT NULL DEFAULT '',
		status        TEXT NOT NULL DEFAULT 'open',
		confidence    TEXT NOT NULL DEFAULT 'config-only',
		verified_by_engagement_id TEXT,
		first_seen    TIMESTAMP DEFAULT NOW(),
		last_seen     TIMESTAMP DEFAULT NOW(),
		PRIMARY KEY (tenant_id, cluster_id, namespace, check_id, resource_kind, resource_name)
	);
	CREATE INDEX IF NOT EXISTS idx_security_findings_lookup ON security_findings(tenant_id, namespace);
	-- ROADMAP P30 phase 2 (ADR 0033): the concrete thing an active-verification
	-- engagement actually checks. Added via ALTER since the table already
	-- shipped in phase 1 — same "extend an already-live table" idiom P27
	-- phase 2 used for service_dependencies' port/outcome columns. A plain
	-- TEXT column, not JSONB: phase 2's one technique needs exactly one
	-- string (a host to GET); a richer shape can be a future migration if a
	-- later technique actually needs one, not guessed at now.
	ALTER TABLE IF EXISTS security_findings ADD COLUMN IF NOT EXISTS target_host TEXT NOT NULL DEFAULT '';

	ALTER TABLE IF EXISTS security_findings ENABLE ROW LEVEL SECURITY;
	ALTER TABLE IF EXISTS security_findings FORCE ROW LEVEL SECURITY;
	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_policies
			WHERE tablename = 'security_findings' AND policyname = 'tenant_isolation'
		) THEN
			EXECUTE 'CREATE POLICY tenant_isolation ON security_findings
				USING (tenant_id = current_setting(''app.current_tenant_id'', true))';
		END IF;
	END $$;

	-- Active-verification engagements (ROADMAP P30 phases 2-4, ADR 0033) —
	-- the approval gate before anything network-active runs, modeled on
	-- remediation_proposals (ADR 0020) with one deliberate correction:
	-- remediation_proposals has NO row-level security at all (tenant_id/
	-- cluster_id were bolted on later via ALTER TABLE, never backed by a
	-- policy — the exact retrofit ADR 0033 warns against). RLS is enabled
	-- here from this table's FIRST migration instead, same as
	-- security_findings above.
	--
	-- Sized for phases 2-4 (phase, technique, scope) even though only phase
	-- 2 is wired up: one engagement authorizes exactly one technique against
	-- exactly one already-existing finding — never an untargeted "general
	-- verification" grant — mirroring remediation_proposals' own "one
	-- proposal, one action" simplicity rather than a multi-technique list
	-- phases 3/4 haven't earned a real design for yet.
	CREATE TABLE IF NOT EXISTS security_engagements (
		id                    TEXT PRIMARY KEY,
		tenant_id             TEXT NOT NULL,
		cluster_id            TEXT NOT NULL DEFAULT '',
		phase                 INT NOT NULL,             -- 2 | 3 | 4
		technique             TEXT NOT NULL,            -- e.g. confirm-http-reachable (phase 2)
		target_namespace      TEXT NOT NULL,
		target_check_id       TEXT NOT NULL,
		target_resource_kind  TEXT NOT NULL,
		target_resource_name  TEXT NOT NULL,
		scope                 JSONB NOT NULL DEFAULT '{}', -- room for phase 3/4's richer constraints
		status                TEXT NOT NULL DEFAULT 'pending', -- pending|approved|rejected|active|completed|failed|expired
		requested_by          TEXT NOT NULL DEFAULT '',
		approved_by           TEXT NOT NULL DEFAULT '',
		created_at            TIMESTAMP DEFAULT NOW(),
		expires_at            TIMESTAMP NOT NULL,
		decided_at            TIMESTAMP,
		started_at            TIMESTAMP,
		completed_at          TIMESTAMP,
		result                JSONB NOT NULL DEFAULT '{}',
		error                 TEXT NOT NULL DEFAULT ''
	);
	CREATE INDEX IF NOT EXISTS idx_security_engagements_status  ON security_engagements(status);
	CREATE INDEX IF NOT EXISTS idx_security_engagements_created ON security_engagements(created_at DESC);
	CREATE INDEX IF NOT EXISTS idx_security_engagements_target
		ON security_engagements(tenant_id, cluster_id, target_namespace, target_check_id, target_resource_kind, target_resource_name);

	ALTER TABLE IF EXISTS security_engagements ENABLE ROW LEVEL SECURITY;
	ALTER TABLE IF EXISTS security_engagements FORCE ROW LEVEL SECURITY;
	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_policies
			WHERE tablename = 'security_engagements' AND policyname = 'tenant_isolation'
		) THEN
			EXECUTE 'CREATE POLICY tenant_isolation ON security_engagements
				USING (tenant_id = current_setting(''app.current_tenant_id'', true))';
		END IF;
	END $$;

	-- Add the vector column + IVFFlat index only when pgvector is installed.
	-- Silently skipped on embedded-postgres (CI tests) which don't ship pgvector.
	DO $$
	BEGIN
		-- Add embedding column (vector(512) = voyage-3-lite dimensions)
		IF NOT EXISTS (
			SELECT 1 FROM information_schema.columns
			WHERE table_name = 'incident_embeddings' AND column_name = 'embedding'
		) THEN
			EXECUTE 'ALTER TABLE incident_embeddings ADD COLUMN embedding vector(512)';
		END IF;
		-- IVFFlat cosine index for fast similarity search
		IF NOT EXISTS (
			SELECT 1 FROM pg_indexes
			WHERE tablename = 'incident_embeddings' AND indexname = 'idx_incident_embeddings_vec'
		) THEN
			EXECUTE 'CREATE INDEX idx_incident_embeddings_vec
				ON incident_embeddings USING ivfflat (embedding vector_cosine_ops)
				WITH (lists = 10)';
		END IF;
	EXCEPTION WHEN OTHERS THEN
		NULL;  -- pgvector not available — vector search disabled, keyword fallback active
	END $$;
	`
	if _, err := c.db.ExecContext(ctx, schema); err != nil {
		return fmt.Errorf("failed to create schema: %w", err)
	}
	c.logger.Info("postgres schema initialized")
	return nil
}

// CurrentStateStore returns the current-state ("kv") store backed by the same DB.
func (c *Client) CurrentStateStore() *CurrentState {
	return &CurrentState{db: c.db, logger: c.logger}
}

// --- Relational (append-only) store: Client itself ---

// Store inserts an event row (append-only). tenant_id/cluster_id (ADR 0024)
// come from data["tenant_id"]/data["cluster_id"] — set server-side by
// Ingester.storeEvent from its already-resolved (never client-trusted)
// values, same convention as event_namespace/type/source above. Empty
// cluster_id (every ingest call that hasn't presented a collector
// credential) writes ” — unchanged from today's behavior in practice,
// since isolation is actually provided by pod_id (ADR 0024's PodID helper),
// not by filtering on these columns; they're written for observability.
func (c *Client) Store(ctx context.Context, podID string, data map[string]interface{}) (string, error) {
	id, ok := data["id"].(string)
	if !ok {
		return "", fmt.Errorf("missing id in data")
	}
	namespace, _ := data["event_namespace"].(string)
	eventType, _ := data["type"].(string)
	timestamp, ok := data["timestamp"].(string)
	if !ok {
		return "", fmt.Errorf("missing timestamp in data")
	}
	tenantID, _ := data["tenant_id"].(string)
	if tenantID == "" {
		tenantID = DefaultTenantID
	}
	clusterID, _ := data["cluster_id"].(string)
	payloadJSON, err := marshalPayload(data)
	if err != nil {
		return "", err
	}

	const q = `
	INSERT INTO events (id, pod_id, event_namespace, event_type, timestamp, payload, tenant_id, cluster_id)
	VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
	RETURNING id`
	var returnedID string
	if err := c.db.QueryRowContext(ctx, q, id, podID, namespace, eventType, timestamp, payloadJSON, tenantID, clusterID).Scan(&returnedID); err != nil {
		c.logger.Error("failed to store event", "error", err)
		return "", err
	}
	return returnedID, nil
}

// Query returns events for a pod. By default it returns the 100 most recent
// (recent-first) — which cert_check relies on. Optional params (spec 006) turn it
// into a time-windowed history read:
//   - since / until : RFC3339 bounds on the event timestamp
//   - entity        : restricts to rows whose payload pod_id/service matches
//   - type          : event_type filter
//   - order         : "asc" (chronological, for trend reading) | "desc" (default)
//   - limit         : default 100, capped at 1000
//
// tenantID is accepted for storage.Backend interface compliance (ROADMAP
// OPS-10 / ADR 0022 amendment) but not yet enforced here: events has the
// same missing-RLS gap current_state had, and closing it is a separate,
// not-yet-decided item — left alone rather than silently fixed as a
// side effect of the current_state retrofit.
func (c *Client) Query(ctx context.Context, tenantID, podID string, queryParams map[string]interface{}) ([]map[string]interface{}, error) {
	q := `SELECT id, pod_id, event_namespace, event_type, timestamp, payload
	FROM events WHERE pod_id = $1`
	args := []interface{}{podID}

	addArg := func(clause string, val interface{}) {
		args = append(args, val)
		q += fmt.Sprintf(" AND %s $%d", clause, len(args))
	}
	if v := stringParam(queryParams, "since"); v != "" {
		addArg("timestamp >=", v)
	}
	if v := stringParam(queryParams, "until"); v != "" {
		addArg("timestamp <=", v)
	}
	if v := stringParam(queryParams, "type"); v != "" {
		addArg("event_type =", v)
	}
	if v := entityParam(queryParams); v != "" {
		// Match the entity against pod_id, service, or deployment in the JSONB payload.
		args = append(args, v)
		n := len(args)
		q += fmt.Sprintf(" AND (payload->>'pod_id' = $%d OR payload->>'service' = $%d OR payload->>'deployment' = $%d)", n, n, n)
	} else if v := stringParam(queryParams, "namespace"); v != "" {
		// No entity filter but namespace present — filter by namespace in payload.
		// Used for cert queries: cert payloads have namespace but no service/pod_id.
		addArg("payload->>'namespace' =", v)
	}

	q += " ORDER BY timestamp " + sqlOrder(queryParams)
	q += fmt.Sprintf(" LIMIT %d", limitParam(queryParams))

	rows, err := c.db.QueryContext(ctx, q, args...)
	if err != nil {
		c.logger.Error("failed to query events", "error", err)
		return nil, err
	}
	defer rows.Close()

	results := []map[string]interface{}{}
	for rows.Next() {
		var id, pid, namespace, eventType, timestamp string
		var payload []byte
		if err := rows.Scan(&id, &pid, &namespace, &eventType, &timestamp, &payload); err != nil {
			c.logger.Error("failed to scan row", "error", err)
			continue
		}
		results = append(results, map[string]interface{}{
			"id":              id,
			"pod_id":          pid,
			"event_namespace": namespace,
			"type":            eventType,
			"timestamp":       timestamp,
			"payload":         decodePayload(payload), // map, not string — Tier-1/redaction expect a map
		})
	}
	return results, rows.Err()
}

// PurgeOlderThan deletes events whose event timestamp is older than cutoff and
// returns the number of rows removed (ADR 0015). Only the append-only events table
// is purged; current_state (latest-wins) is never touched.
//
// Per-pod TTLs: high-frequency pods (metrics, certificates) use a shorter window
// than sparse event pods, keeping storage bounded at ~20k rows steady-state.
func (c *Client) PurgeOlderThan(ctx context.Context, cutoff time.Time) (int64, error) {
	var total int64

	// High-frequency pods accumulate ~2,880 rows/day at 30s scrape intervals.
	// 7 days is enough for trend analysis; live-state is a latest-wins snapshot
	// so 2 days of history is more than sufficient.
	type podWindow struct {
		podID  string
		cutoff time.Time
	}
	windows := []podWindow{
		{"k8fy.metrics", time.Now().Add(-7 * 24 * time.Hour)},
		{"k8fy.certificates", time.Now().Add(-7 * 24 * time.Hour)},
	}
	for _, w := range windows {
		res, err := c.db.ExecContext(ctx,
			`DELETE FROM events WHERE pod_id = $1 AND timestamp < $2`,
			w.podID, w.cutoff.UTC().Format(time.RFC3339))
		if err != nil {
			c.logger.Error("failed to purge events", "pod_id", w.podID, "error", err)
			continue
		}
		n, _ := res.RowsAffected()
		total += n
	}

	// All other pods use the caller-supplied cutoff (EVENTS_RETENTION_DAYS env var).
	res, err := c.db.ExecContext(ctx,
		`DELETE FROM events WHERE pod_id NOT IN ('k8fy.metrics','k8fy.certificates')
		 AND timestamp < $1`, cutoff.UTC().Format(time.RFC3339))
	if err != nil {
		c.logger.Error("failed to purge old events", "error", err)
		return total, err
	}
	n, _ := res.RowsAffected()
	total += n
	return total, nil
}

// HealthCheck verifies the connection.
func (c *Client) HealthCheck(ctx context.Context) error { return c.db.PingContext(ctx) }

// Close closes the shared connection pool (owns the DB lifecycle).
func (c *Client) Close() error { return c.db.Close() }

// --- Traces (query history) ---

// TraceRecord is one persisted query provenance entry.
type TraceRecord struct {
	ID                       string
	TraceID                  string
	Question                 string
	Intent                   string
	Namespace                string
	Tier                     string
	Status                   string
	Confidence               float64
	Sources                  []string
	ToolCalls                []string
	LatencyMs                int64
	StartedAt                time.Time
	CreatedAt                time.Time
	InputTokens              int64
	OutputTokens             int64
	CacheCreationInputTokens int64
	CacheReadInputTokens     int64
	EstimatedCostUSD         float64
	PromptName               string
	PromptVersion            *int   // nil = local fallback or no LLM call
	SessionID                string // "" = single-shot query, not part of a conversation
	IsEval                   bool   // true = synthetic eval traffic; exclude from quality sampling
	TenantID                 string
	ClusterID                string
}

// TracesSummary holds aggregated statistics derived from the traces table.
type TracesSummary struct {
	TotalQueries      int64
	QueriesByTier     map[string]int64
	QueriesByStatus   map[string]int64
	QueriesByIntent   map[string]int64
	AvgAgentLatencyMs float64
	P95AgentLatencyMs float64
	Last24hCount      int64
}

// InsertTrace persists one query trace row. Errors are logged by the caller.
func (c *Client) InsertTrace(ctx context.Context, t TraceRecord) error {
	srcJSON, _ := json.Marshal(t.Sources)
	tcJSON, _ := json.Marshal(t.ToolCalls)
	startedAt := t.StartedAt
	if startedAt.IsZero() {
		startedAt = time.Now()
	}
	_, err := c.db.ExecContext(ctx,
		`INSERT INTO traces (id, trace_id, question, intent, namespace, tier, status,
		  confidence, sources, tool_calls, latency_ms, started_at, created_at,
		  input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens,
		  estimated_cost_usd, prompt_name, prompt_version, session_id, is_eval)
		 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,NOW(),$13,$14,$15,$16,$17,$18,$19,$20,$21)
		 ON CONFLICT (id) DO NOTHING`,
		t.ID, t.TraceID, t.Question, t.Intent, t.Namespace, t.Tier, t.Status,
		t.Confidence, srcJSON, tcJSON, t.LatencyMs, startedAt,
		t.InputTokens, t.OutputTokens,
		t.CacheCreationInputTokens, t.CacheReadInputTokens,
		t.EstimatedCostUSD, t.PromptName, t.PromptVersion, t.SessionID, t.IsEval)
	return err
}

const traceSelectCols = `
	SELECT id, trace_id, question, intent, namespace, tier, status,
	       confidence, sources, tool_calls, latency_ms,
	       COALESCE(started_at, created_at) AS started_at, created_at,
	       COALESCE(input_tokens, 0), COALESCE(output_tokens, 0),
	       COALESCE(cache_creation_input_tokens, 0), COALESCE(cache_read_input_tokens, 0),
	       COALESCE(estimated_cost_usd, 0),
	       COALESCE(prompt_name, ''), prompt_version, COALESCE(session_id, ''),
	       COALESCE(is_eval, FALSE),
	       tenant_id, COALESCE(cluster_id, '')
	FROM traces`

func scanTrace(row interface{ Scan(...any) error }) (TraceRecord, error) {
	var t TraceRecord
	var srcJSON, tcJSON []byte
	err := row.Scan(
		&t.ID, &t.TraceID, &t.Question, &t.Intent, &t.Namespace,
		&t.Tier, &t.Status, &t.Confidence, &srcJSON, &tcJSON, &t.LatencyMs,
		&t.StartedAt, &t.CreatedAt,
		&t.InputTokens, &t.OutputTokens,
		&t.CacheCreationInputTokens, &t.CacheReadInputTokens,
		&t.EstimatedCostUSD,
		&t.PromptName, &t.PromptVersion, &t.SessionID, &t.IsEval,
		&t.TenantID, &t.ClusterID)
	if err != nil {
		return t, err
	}
	_ = json.Unmarshal(srcJSON, &t.Sources)
	_ = json.Unmarshal(tcJSON, &t.ToolCalls)
	return t, nil
}

// GetTrace returns a single trace by its primary key ID.
func (c *Client) GetTrace(ctx context.Context, id string) (*TraceRecord, error) {
	row := c.db.QueryRowContext(ctx, traceSelectCols+` WHERE id = $1`, id)
	t, err := scanTrace(row)
	if err != nil {
		return nil, err
	}
	return &t, nil
}

// ListTraces returns the most recent traces (newest first), capped at limit.
func (c *Client) ListTraces(ctx context.Context, limit int) ([]TraceRecord, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	rows, err := c.db.QueryContext(ctx,
		traceSelectCols+` ORDER BY created_at DESC LIMIT $1`, limit)
	if err != nil {
		return nil, fmt.Errorf("list traces: %w", err)
	}
	defer rows.Close()

	var result []TraceRecord
	for rows.Next() {
		t, err := scanTrace(rows)
		if err != nil {
			return nil, fmt.Errorf("scan trace: %w", err)
		}
		result = append(result, t)
	}
	return result, rows.Err()
}

// GetTracesSummary returns aggregated query statistics for the metrics dashboard.
func (c *Client) GetTracesSummary(ctx context.Context) (*TracesSummary, error) {
	s := &TracesSummary{
		QueriesByTier:   make(map[string]int64),
		QueriesByStatus: make(map[string]int64),
		QueriesByIntent: make(map[string]int64),
	}

	// Total + last 24h
	if err := c.db.QueryRowContext(ctx,
		`SELECT COUNT(*), COALESCE(SUM(CASE WHEN created_at > NOW()-INTERVAL '24 hours' THEN 1 ELSE 0 END),0)
		 FROM traces`).Scan(&s.TotalQueries, &s.Last24hCount); err != nil {
		return nil, fmt.Errorf("summary totals: %w", err)
	}

	// By tier
	tierRows, err := c.db.QueryContext(ctx, `SELECT tier, COUNT(*) FROM traces GROUP BY tier`)
	if err == nil {
		defer tierRows.Close()
		for tierRows.Next() {
			var k string
			var v int64
			if tierRows.Scan(&k, &v) == nil {
				s.QueriesByTier[k] = v
			}
		}
	}

	// By status
	statusRows, err := c.db.QueryContext(ctx, `SELECT status, COUNT(*) FROM traces GROUP BY status`)
	if err == nil {
		defer statusRows.Close()
		for statusRows.Next() {
			var k string
			var v int64
			if statusRows.Scan(&k, &v) == nil {
				s.QueriesByStatus[k] = v
			}
		}
	}

	// By intent
	intentRows, err := c.db.QueryContext(ctx, `SELECT intent, COUNT(*) FROM traces GROUP BY intent`)
	if err == nil {
		defer intentRows.Close()
		for intentRows.Next() {
			var k string
			var v int64
			if intentRows.Scan(&k, &v) == nil {
				s.QueriesByIntent[k] = v
			}
		}
	}

	// Avg + P95 agent latency (tier2 only)
	c.db.QueryRowContext(ctx,
		`SELECT COALESCE(AVG(latency_ms),0),
		        COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms),0)
		 FROM traces WHERE tier='tier2'`).
		Scan(&s.AvgAgentLatencyMs, &s.P95AgentLatencyMs)

	return s, nil
}

// --- Integrations (admin CRUD) ---

// Integration is the storage-layer representation of an admin integration.
// Token is the outbound adapter credential; when Secrets Manager mode is
// enabled (ADR 0025) it is left empty and TokenSecretARN points to the real
// value instead. When Secrets Manager mode is off (no INTEGRATION_SECRETS_PREFIX
// configured — the default), Token still carries the plaintext value exactly
// as before, so every existing deployment is unaffected by this field's
// addition.
type Integration struct {
	ID             string
	Name           string
	Namespaces     []string
	Status         string
	Token          string // outbound: this backend calling OUT to the adapter
	TokenSecretARN string // outbound, Secrets-Manager mode only (ADR 0025); empty otherwise
	CollectorToken string // inbound: a collector calling IN to this backend (ADR 0022)
	TenantID       string
	ClusterID      string
	CreatedAt      time.Time
	UpdatedAt      time.Time
}

// ListIntegrations returns all integrations ordered by creation time.
func (c *Client) ListIntegrations(ctx context.Context) ([]Integration, error) {
	rows, err := c.db.QueryContext(ctx,
		`SELECT id, name, namespaces, status, token, token_secret_arn, collector_token, tenant_id, COALESCE(cluster_id, ''), created_at, updated_at
		 FROM integrations ORDER BY created_at ASC`)
	if err != nil {
		return nil, fmt.Errorf("list integrations: %w", err)
	}
	defer rows.Close()

	var result []Integration
	for rows.Next() {
		var in Integration
		var nsJSON []byte
		if err := rows.Scan(&in.ID, &in.Name, &nsJSON, &in.Status, &in.Token, &in.TokenSecretARN, &in.CollectorToken, &in.TenantID, &in.ClusterID, &in.CreatedAt, &in.UpdatedAt); err != nil {
			return nil, fmt.Errorf("scan integration: %w", err)
		}
		if err := json.Unmarshal(nsJSON, &in.Namespaces); err != nil {
			in.Namespaces = nil
		}
		result = append(result, in)
	}
	return result, rows.Err()
}

// GetIntegration returns one integration by ID, or sql.ErrNoRows if not found.
func (c *Client) GetIntegration(ctx context.Context, id string) (*Integration, error) {
	var in Integration
	var nsJSON []byte
	err := c.db.QueryRowContext(ctx,
		`SELECT id, name, namespaces, status, token, token_secret_arn, collector_token, tenant_id, COALESCE(cluster_id, ''), created_at, updated_at
		 FROM integrations WHERE id = $1`, id).
		Scan(&in.ID, &in.Name, &nsJSON, &in.Status, &in.Token, &in.TokenSecretARN, &in.CollectorToken, &in.TenantID, &in.ClusterID, &in.CreatedAt, &in.UpdatedAt)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(nsJSON, &in.Namespaces); err != nil {
		in.Namespaces = nil
	}
	return &in, nil
}

// GetIntegrationByCollectorToken looks up the Integration a collector's push
// credential belongs to — sql.ErrNoRows if the token is unrecognized (or
// empty; empty never matches, since every row defaults collector_token to ”).
func (c *Client) GetIntegrationByCollectorToken(ctx context.Context, token string) (*Integration, error) {
	var in Integration
	var nsJSON []byte
	err := c.db.QueryRowContext(ctx,
		`SELECT id, name, namespaces, status, token, token_secret_arn, collector_token, tenant_id, COALESCE(cluster_id, ''), created_at, updated_at
		 FROM integrations WHERE collector_token = $1 AND collector_token != ''`, token).
		Scan(&in.ID, &in.Name, &nsJSON, &in.Status, &in.Token, &in.TokenSecretARN, &in.CollectorToken, &in.TenantID, &in.ClusterID, &in.CreatedAt, &in.UpdatedAt)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(nsJSON, &in.Namespaces); err != nil {
		in.Namespaces = nil
	}
	return &in, nil
}

// CreateIntegration inserts a new integration row. in.ID must be set by the caller.
func (c *Client) CreateIntegration(ctx context.Context, in *Integration) error {
	nsJSON, err := json.Marshal(in.Namespaces)
	if err != nil {
		return fmt.Errorf("marshal namespaces: %w", err)
	}
	_, err = c.db.ExecContext(ctx,
		`INSERT INTO integrations (id, name, namespaces, status, token, token_secret_arn, collector_token, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW())`,
		in.ID, in.Name, nsJSON, in.Status, in.Token, in.TokenSecretARN, in.CollectorToken)
	return err
}

// UpdateIntegration replaces all mutable fields for an existing integration.
// Preserves created_at. CollectorToken is updated only when non-empty (empty
// = keep existing), independent of the other credentials.
//
// Token and TokenSecretARN are mutually exclusive: only one of them ever
// holds the real outbound credential at a time (ADR 0025). Supplying a
// non-empty value for either clears the other, so a row transitioning from
// plaintext to Secrets-Manager mode (or back) never ends up with a stale
// plaintext token sitting alongside a live secret reference. Supplying both
// empty keeps whichever one is currently set, same "empty = no change"
// convention as CollectorToken.
func (c *Client) UpdateIntegration(ctx context.Context, in *Integration) error {
	nsJSON, err := json.Marshal(in.Namespaces)
	if err != nil {
		return fmt.Errorf("marshal namespaces: %w", err)
	}
	_, err = c.db.ExecContext(ctx,
		`UPDATE integrations SET
		   name=$1, namespaces=$2, status=$3,
		   token = CASE WHEN $5 != '' THEN '' WHEN $4 = '' THEN token ELSE $4 END,
		   token_secret_arn = CASE WHEN $4 != '' THEN '' WHEN $5 = '' THEN token_secret_arn ELSE $5 END,
		   collector_token = CASE WHEN $6 = '' THEN collector_token ELSE $6 END,
		   updated_at=NOW()
		 WHERE id=$7`,
		in.Name, nsJSON, in.Status, in.Token, in.TokenSecretARN, in.CollectorToken, in.ID)
	return err
}

// DeleteIntegration removes an integration by ID.
func (c *Client) DeleteIntegration(ctx context.Context, id string) error {
	_, err := c.db.ExecContext(ctx, `DELETE FROM integrations WHERE id = $1`, id)
	return err
}

// UpdateIntegrationNamespaces overwrites just the namespaces column for one
// Integration row — used by the fleet collector's inventory push (ADR 0022 /
// ROADMAP P18 use case #1) to auto-populate Namespaces from what the
// collector actually sees in its own cluster, distinct from
// UpdateIntegration's full-row admin-form replace (the collector never knows
// Name/etc., only namespaces).
func (c *Client) UpdateIntegrationNamespaces(ctx context.Context, id string, namespaces []string) error {
	nsJSON, err := json.Marshal(namespaces)
	if err != nil {
		return fmt.Errorf("marshal namespaces: %w", err)
	}
	_, err = c.db.ExecContext(ctx,
		`UPDATE integrations SET namespaces=$1, updated_at=NOW() WHERE id=$2`,
		nsJSON, id)
	return err
}

// --- Current-state ("kv") store ---

// CurrentState is the current-state store: latest value per (pod_id, entity_key).
// It shares the parent Client's *sql.DB, so its Close is a no-op.
type CurrentState struct {
	db     *sql.DB
	logger *slog.Logger
}

// Store upserts the latest state for an entity (latest-wins). tenant_id/
// cluster_id (ADR 0024) come from data["tenant_id"]/data["cluster_id"] —
// same server-side-resolved convention as Client.Store. Isolation used to
// rest entirely on pod_id (ADR 0024's PodID helper); ROADMAP OPS-10 (ADR
// 0022 amendment) adds a real RLS backstop on top by setting the session's
// tenant context from this same already-trusted value before the write.
func (s *CurrentState) Store(ctx context.Context, podID string, data map[string]interface{}) (string, error) {
	entityKey, _ := data["entity_key"].(string)
	if entityKey == "" {
		entityKey, _ = data["id"].(string) // ingester sets id == entity key for kv
	}
	if entityKey == "" {
		return "", fmt.Errorf("missing entity_key for current-state store")
	}
	namespace, _ := data["event_namespace"].(string)
	eventType, _ := data["type"].(string)
	source, _ := data["source"].(string)
	tenantID, _ := data["tenant_id"].(string)
	if tenantID == "" {
		tenantID = DefaultTenantID
	}
	clusterID, _ := data["cluster_id"].(string)
	payloadJSON, err := marshalPayload(data)
	if err != nil {
		return "", err
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return "", fmt.Errorf("set tenant context: %w", err)
	}

	const q = `
	INSERT INTO current_state (pod_id, entity_key, event_namespace, event_type, source, payload, tenant_id, cluster_id, updated_at)
	VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
	ON CONFLICT (pod_id, entity_key) DO UPDATE SET
		event_namespace = EXCLUDED.event_namespace,
		event_type      = EXCLUDED.event_type,
		source          = EXCLUDED.source,
		payload         = EXCLUDED.payload,
		tenant_id       = EXCLUDED.tenant_id,
		cluster_id      = EXCLUDED.cluster_id,
		updated_at      = NOW()`
	if _, err := tx.ExecContext(ctx, q, podID, entityKey, namespace, eventType, source, payloadJSON, tenantID, clusterID); err != nil {
		s.logger.Error("failed to upsert current_state", "error", err)
		return "", err
	}
	if err := tx.Commit(); err != nil {
		return "", err
	}
	return fmt.Sprintf("%s:%s", podID, entityKey), nil
}

// Delete removes one entity's row entirely — the delete path ROADMAP OPS-12
// found missing: a K8s watch DELETED event used to be normalised to
// event_type "pod_deleted" and then UPSERTED like any other event (Store,
// above), so the row survived forever holding the dead pod's last-known
// state. ing.storeEvent (ingester.go) now calls this instead of Store when
// it sees that event type. Deleting zero rows (the entity was never stored,
// or a retry after an already-successful delete) is not an error — same
// idempotent-delete convention as the rest of this codebase.
func (s *CurrentState) Delete(ctx context.Context, tenantID, podID, entityKey string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return fmt.Errorf("set tenant context: %w", err)
	}
	if _, err := tx.ExecContext(ctx,
		`DELETE FROM current_state WHERE pod_id = $1 AND entity_key = $2`, podID, entityKey); err != nil {
		s.logger.Error("failed to delete current_state row", "error", err)
		return err
	}
	return tx.Commit()
}

// Query does a point lookup when given "key", a prefix scan when given "service",
// else returns all entities in the shard.
//
// "key"     — exact entity_key match (use for known full pod names)
// "service" — matches the K8s Service row exactly OR any pod replica whose
//
//	entity_key starts with "{service}-" (covers Deployment-only
//	workloads that have no K8s Service object)
//
// tenantID is server-resolved by the caller (ROADMAP OPS-10 / ADR 0022
// amendment) — never taken from queryParams, which on the /api/query path
// is built directly from client-supplied request context and so cannot be
// trusted to carry a tenant claim.
func (s *CurrentState) Query(ctx context.Context, tenantID, podID string, queryParams map[string]interface{}) ([]map[string]interface{}, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}

	var rows *sql.Rows
	if key, ok := queryParams["key"].(string); ok && key != "" {
		rows, err = tx.QueryContext(ctx,
			`SELECT entity_key, event_namespace, event_type, source, payload, updated_at
			 FROM current_state WHERE pod_id = $1 AND entity_key = $2`, podID, key)
	} else if svc, ok := queryParams["service"].(string); ok && svc != "" {
		rows, err = tx.QueryContext(ctx,
			`SELECT entity_key, event_namespace, event_type, source, payload, updated_at
			 FROM current_state WHERE pod_id = $1 AND (entity_key = $2 OR entity_key LIKE $3)`,
			podID, svc, svc+"-%")
	} else {
		rows, err = tx.QueryContext(ctx,
			`SELECT entity_key, event_namespace, event_type, source, payload, updated_at
			 FROM current_state WHERE pod_id = $1`, podID)
	}
	if err != nil {
		s.logger.Error("failed to query current_state", "error", err)
		return nil, err
	}
	defer rows.Close()

	results := []map[string]interface{}{}
	for rows.Next() {
		var entityKey, namespace, eventType, source, updatedAt string
		var payload []byte
		if err := rows.Scan(&entityKey, &namespace, &eventType, &source, &payload, &updatedAt); err != nil {
			s.logger.Error("failed to scan current_state row", "error", err)
			continue
		}
		results = append(results, map[string]interface{}{
			"entity_key":      entityKey,
			"event_namespace": namespace,
			"type":            eventType,
			"source":          source,
			"timestamp":       updatedAt,
			"payload":         decodePayload(payload),
		})
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	rows.Close()
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return results, nil
}

// TrackedEntities returns active namespace/service pairs from the live-state
// shards — used to power the frontend autocomplete. Each entry is formatted as
// "namespace/service_name" (e.g. "payments/payment-worker").
//
// K8s Services (event_type service_*) are included by name directly.
// Deployments that have no K8s Service (workers, consumers) are derived from
// pod_* rows by stripping the two trailing K8s hash segments
// ({rs-hash}-{pod-hash}), recovering the deployment name. Results are
// deduplicated so each namespace/name pair appears only once.
//
// tenantID (ROADMAP OPS-10 / ADR 0022 amendment): this had NO tenant
// filtering at all before — not even the manual-WHERE-clause pattern
// ListServiceHealth already used — despite being the one read here with the
// widest blast radius (whole-table, unkeyed by any single pod_id). Filtered
// explicitly here, belt-and-suspenders alongside the RLS backstop.
func (s *CurrentState) TrackedEntities(ctx context.Context, tenantID string) ([]string, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}

	const q = `
	SELECT pod_id, entity_key, event_type
	FROM current_state
	WHERE tenant_id = $1
	  AND pod_id LIKE 'k8fy.live-state.%'
	  AND (event_type LIKE 'service_%' OR event_type LIKE 'pod_%')
	ORDER BY pod_id, entity_key`

	rows, err := tx.QueryContext(ctx, q, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	seen := make(map[string]struct{})
	var result []string

	for rows.Next() {
		var podID, entityKey, eventType string
		if err := rows.Scan(&podID, &entityKey, &eventType); err != nil {
			continue
		}
		ns := strings.TrimPrefix(podID, "k8fy.live-state.")
		// ADR 0024: a cluster-scoped shard is "k8fy.live-state.{clusterID}.{namespace}"
		// instead of "k8fy.live-state.{namespace}" — take the segment after the
		// last dot as the actual K8s namespace (neither a clusterID nor a K8s
		// namespace can itself contain a dot), so a fleet cluster's data doesn't
		// show up as a bogus "{clusterID}.{namespace}" namespace in the
		// autocomplete. This flattens away which cluster an entity came from —
		// acceptable for this listing (still just namespace/service pairs), not
		// resolved further here.
		if idx := strings.LastIndex(ns, "."); idx >= 0 {
			ns = ns[idx+1:]
		}
		if ns == "" || entityKey == "" {
			continue
		}
		// For pod rows, strip K8s hash suffixes to recover the deployment name.
		name := entityKey
		if strings.HasPrefix(eventType, "pod_") {
			name = k8sPodSuffix.ReplaceAllString(entityKey, "")
		}
		key := ns + "/" + name
		if _, dup := seen[key]; !dup {
			seen[key] = struct{}{}
			result = append(result, key)
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	rows.Close()
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return result, nil
}

// HealthCheck verifies the connection.
func (s *CurrentState) HealthCheck(ctx context.Context) error { return s.db.PingContext(ctx) }

// Close is a no-op: the parent Client owns the shared connection pool.
func (s *CurrentState) Close() error { return nil }

// --- helpers ---

// marshalPayload returns the JSON text of data["payload"] (a map), or of the
// whole record if no nested payload is present. Returned as a string so lib/pq
// stores it in a JSONB column (a []byte would be sent as bytea).
func marshalPayload(data map[string]interface{}) (string, error) {
	payload, ok := data["payload"].(map[string]interface{})
	if !ok {
		payload = data
	}
	b, err := json.Marshal(payload)
	if err != nil {
		return "", fmt.Errorf("failed to marshal payload: %w", err)
	}
	return string(b), nil
}

// stringParam reads a string query parameter, or "" if absent/not a string.
func stringParam(params map[string]interface{}, name string) string {
	if v, ok := params[name].(string); ok {
		return v
	}
	return ""
}

// entityParam reads the entity to filter a time-series to, accepting the common
// aliases the agent/handlers use ("entity", "pod_id", "service").
func entityParam(params map[string]interface{}) string {
	for _, k := range []string{"entity", "pod_id", "service", "deployment"} {
		if v := stringParam(params, k); v != "" {
			return v
		}
	}
	return ""
}

// sqlOrder returns "ASC" only when explicitly requested, else "DESC" — preserving
// the recent-first default that cert_check depends on.
func sqlOrder(params map[string]interface{}) string {
	if strings.EqualFold(stringParam(params, "order"), "asc") {
		return "ASC"
	}
	return "DESC"
}

// limitParam returns a row cap: the requested limit clamped to [1,1000], else 100.
func limitParam(params map[string]interface{}) int {
	n := 0
	switch v := params["limit"].(type) {
	case int:
		n = v
	case int64:
		n = int(v)
	case float64: // JSON numbers decode to float64
		n = int(v)
	}
	if n <= 0 {
		return 100
	}
	if n > 1000 {
		return 1000
	}
	return n
}

// decodePayload turns a JSONB column back into a map (so Tier-1 and the redactor
// can read fields); falls back to the raw string on error.
func decodePayload(b []byte) interface{} {
	var m map[string]interface{}
	if err := json.Unmarshal(b, &m); err != nil {
		return string(b)
	}
	return m
}

// ── Chat sessions ────────────────────────────────────────────────────────────

// ChatMessage is one turn in a multi-turn conversation.
type ChatMessage struct {
	Role      string                 `json:"role"` // "user" | "assistant"
	Content   string                 `json:"content"`
	CreatedAt time.Time              `json:"created_at"`
	Details   map[string]interface{} `json:"details,omitempty"` // structured sections for the Chat UI (severity, timeline, findings, recommended_actions, ...); nil for user messages and old assistant messages
}

// ChatSession holds the full state of one multi-turn conversation.
type ChatSession struct {
	ID               string         `json:"id"`
	Title            string         `json:"title"`
	Namespace        string         `json:"namespace"`
	Service          string         `json:"service"`
	Messages         []ChatMessage  `json:"messages"`
	ContextCache     map[string]any `json:"context_cache"`
	ContextFetchedAt *time.Time     `json:"context_fetched_at,omitempty"`
	TenantID         string         `json:"tenant_id"`
	ClusterID        string         `json:"cluster_id,omitempty"`
	CreatedAt        time.Time      `json:"created_at"`
	LastActive       time.Time      `json:"last_active"`
	ExpiresAt        time.Time      `json:"expires_at"`
}

// CreateChatSession inserts a new session and returns it.
func (c *Client) CreateChatSession(ctx context.Context, s *ChatSession) error {
	msgsJSON, _ := json.Marshal(s.Messages)
	cacheJSON, _ := json.Marshal(s.ContextCache)
	_, err := c.db.ExecContext(ctx,
		`INSERT INTO chat_sessions (id, title, namespace, service, messages, context_cache,
		  created_at, last_active, expires_at)
		 VALUES ($1,$2,$3,$4,$5,$6,NOW(),NOW(),$7)`,
		s.ID, s.Title, s.Namespace, s.Service, msgsJSON, cacheJSON, s.ExpiresAt)
	return err
}

// GetChatSession loads a session by id.
func (c *Client) GetChatSession(ctx context.Context, id string) (*ChatSession, error) {
	row := c.db.QueryRowContext(ctx,
		`SELECT id, title, namespace, service, messages, context_cache,
		        context_fetched_at, tenant_id, COALESCE(cluster_id, ''), created_at, last_active, expires_at
		 FROM chat_sessions WHERE id = $1`, id)
	return scanChatSession(row)
}

// UpdateChatSession persists the full session state (messages, cache, timestamps).
func (c *Client) UpdateChatSession(ctx context.Context, s *ChatSession) error {
	msgsJSON, _ := json.Marshal(s.Messages)
	cacheJSON, _ := json.Marshal(s.ContextCache)
	_, err := c.db.ExecContext(ctx,
		`UPDATE chat_sessions SET
		   title = $2, messages = $3, context_cache = $4,
		   context_fetched_at = $5, last_active = NOW(), expires_at = $6
		 WHERE id = $1`,
		s.ID, s.Title, msgsJSON, cacheJSON, s.ContextFetchedAt, s.ExpiresAt)
	return err
}

// ListChatSessions returns the most recently active sessions (newest first).
func (c *Client) ListChatSessions(ctx context.Context, limit int) ([]ChatSession, error) {
	if limit <= 0 || limit > 100 {
		limit = 20
	}
	rows, err := c.db.QueryContext(ctx,
		`SELECT id, title, namespace, service, messages, context_cache,
		        context_fetched_at, tenant_id, COALESCE(cluster_id, ''), created_at, last_active, expires_at
		 FROM chat_sessions ORDER BY last_active DESC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []ChatSession
	for rows.Next() {
		s, err := scanChatSession(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, *s)
	}
	return result, rows.Err()
}

// DeleteChatSession removes a session permanently.
func (c *Client) DeleteChatSession(ctx context.Context, id string) error {
	_, err := c.db.ExecContext(ctx, `DELETE FROM chat_sessions WHERE id = $1`, id)
	return err
}

func scanChatSession(row interface{ Scan(...any) error }) (*ChatSession, error) {
	var s ChatSession
	var msgsJSON, cacheJSON []byte
	err := row.Scan(
		&s.ID, &s.Title, &s.Namespace, &s.Service,
		&msgsJSON, &cacheJSON, &s.ContextFetchedAt,
		&s.TenantID, &s.ClusterID,
		&s.CreatedAt, &s.LastActive, &s.ExpiresAt)
	if err != nil {
		return nil, err
	}
	_ = json.Unmarshal(msgsJSON, &s.Messages)
	if s.Messages == nil {
		s.Messages = []ChatMessage{}
	}
	if s.ContextCache == nil {
		s.ContextCache = map[string]any{}
	}
	_ = json.Unmarshal(cacheJSON, &s.ContextCache)
	return &s, nil
}

// ── Semantic memory (incident embeddings) ────────────────────────────────────

// IncidentEmbedding is one row in incident_embeddings: a Tier-2 trace
// paired with its vector representation for similarity search (P8).
type IncidentEmbedding struct {
	ID        string
	TraceID   string
	Namespace string
	Service   string
	Summary   string
	Embedding []float32 // nil when embed service was unavailable
	TenantID  string
	ClusterID string
}

// InsertIncidentEmbedding upserts an incident embedding row. The embedding
// column is set to NULL when vec is empty so the row is still queryable via
// keyword filters while the vector index is being populated.
func (c *Client) InsertIncidentEmbedding(ctx context.Context, e IncidentEmbedding) error {
	if e.Embedding == nil {
		_, err := c.db.ExecContext(ctx, `
			INSERT INTO incident_embeddings (id, trace_id, namespace, service, summary)
			VALUES ($1,$2,$3,$4,$5)
			ON CONFLICT (id) DO UPDATE SET summary=EXCLUDED.summary`,
			e.ID, e.TraceID, e.Namespace, e.Service, e.Summary)
		return err
	}

	// pgvector expects the vector as a Postgres-formatted literal: '[0.1,0.2,...]'
	var sb strings.Builder
	sb.WriteByte('[')
	for i, v := range e.Embedding {
		if i > 0 {
			sb.WriteByte(',')
		}
		sb.WriteString(fmt.Sprintf("%g", v))
	}
	sb.WriteByte(']')

	_, err := c.db.ExecContext(ctx, `
		INSERT INTO incident_embeddings (id, trace_id, namespace, service, summary, embedding)
		VALUES ($1,$2,$3,$4,$5,$6::vector)
		ON CONFLICT (id) DO UPDATE SET
			summary   = EXCLUDED.summary,
			embedding = EXCLUDED.embedding`,
		e.ID, e.TraceID, e.Namespace, e.Service, e.Summary, sb.String())
	return err
}

// incidentDedupKey groups rows describing the same incident.
//
// Summaries are built as "headline | likely_cause" (see embedAndStoreIncident),
// so the headline identifies the incident and the cause may vary in wording
// between runs. Keying on the normalised headline therefore collapses repeat
// diagnoses of one problem without merging genuinely different incidents that
// happen to share a cause.
func incidentDedupKey(summary string) string {
	head := summary
	if i := strings.Index(summary, " | "); i >= 0 {
		head = summary[:i]
	}
	return strings.ToLower(strings.Join(strings.Fields(head), " "))
}

// FindSimilarIncidents returns up to limit incidents whose embedding is closest
// to queryVec (cosine similarity). Falls back to recency order when pgvector is
// unavailable (vec is nil) so callers always get a useful result.
//
// Results are deduplicated by incident: diagnosing the same problem repeatedly
// writes a near-identical row each time (there is no write-side dedup), so an
// undeduplicated top-3 can be three copies of one incident, crowding out every
// genuinely different past incident. Observed 2026-09-01: four of five stored
// rows were the same payment-worker Pending incident.
//
// Deduplication happens on read rather than on write so storage stays an honest
// log of what actually happened — how often an incident recurs is real
// information, it just should not consume all three few-shot slots.
func (c *Client) FindSimilarIncidents(ctx context.Context, namespace, service string, queryVec []float32, limit int) ([]IncidentEmbedding, error) {
	if limit <= 0 {
		limit = 3
	}
	// Over-fetch so duplicates can be dropped without under-filling the result.
	// Bounded so a pathological table cannot turn this into a large scan.
	fetchLimit := limit * 4
	if fetchLimit > 60 {
		fetchLimit = 60
	}

	var rows *sql.Rows
	var err error

	if len(queryVec) > 0 {
		// Vector similarity search via pgvector (<-> = cosine distance, lower = more similar).
		var sb strings.Builder
		sb.WriteByte('[')
		for i, v := range queryVec {
			if i > 0 {
				sb.WriteByte(',')
			}
			sb.WriteString(fmt.Sprintf("%g", v))
		}
		sb.WriteByte(']')
		rows, err = c.db.QueryContext(ctx, `
			SELECT id, trace_id, namespace, service, summary, tenant_id, COALESCE(cluster_id, '')
			FROM incident_embeddings
			WHERE embedding IS NOT NULL
			ORDER BY embedding <-> $1::vector
			LIMIT $2`,
			sb.String(), fetchLimit)
	} else {
		// Fallback: most recent incidents matching namespace/service.
		rows, err = c.db.QueryContext(ctx, `
			SELECT id, trace_id, namespace, service, summary, tenant_id, COALESCE(cluster_id, '')
			FROM incident_embeddings
			WHERE ($1 = '' OR namespace = $1)
			  AND ($2 = '' OR service  = $2)
			ORDER BY created_at DESC
			LIMIT $3`,
			namespace, service, fetchLimit)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var result []IncidentEmbedding
	seen := make(map[string]struct{}, fetchLimit)
	for rows.Next() {
		var e IncidentEmbedding
		if serr := rows.Scan(&e.ID, &e.TraceID, &e.Namespace, &e.Service, &e.Summary, &e.TenantID, &e.ClusterID); serr != nil {
			continue
		}
		key := incidentDedupKey(e.Summary)
		if _, dup := seen[key]; dup {
			continue
		}
		seen[key] = struct{}{}
		if len(result) >= limit {
			break
		}
		result = append(result, e)
	}
	return result, rows.Err()
}

// ── Remediation proposals (ADR 0020) ────────────────────────────────────────

// RemediationProposal is one propose→approve/reject→execute record for a
// Phase-3 write action (spec 011 Use Cases 1+2). Producing a proposal makes
// no infrastructure calls; only an approved proposal is executed.
type RemediationProposal struct {
	ID             string
	TraceID        string
	UseCase        string // incident_responder | deployment_guardian
	Namespace      string
	Service        string
	ProposedAction string // restart_deployment | scale_deployment | rollback_deployment | rotate_cert | human_escalation
	ActionParams   map[string]interface{}
	Analysis       map[string]interface{}
	Status         string // pending | approved | rejected | executed | failed | expired
	SourceEventID  string
	CreatedAt      time.Time
	ExpiresAt      time.Time
	DecidedAt      *time.Time
	DecidedBy      string
	ExecutedAt     *time.Time
	Result         map[string]interface{}
	Error          string
	TenantID       string
	ClusterID      string
}

// CreateRemediationProposal inserts a new pending proposal. p.ID must be set by the caller.
func (c *Client) CreateRemediationProposal(ctx context.Context, p *RemediationProposal) error {
	paramsJSON, err := json.Marshal(p.ActionParams)
	if err != nil {
		return fmt.Errorf("marshal action_params: %w", err)
	}
	analysisJSON, err := json.Marshal(p.Analysis)
	if err != nil {
		return fmt.Errorf("marshal analysis: %w", err)
	}
	_, err = c.db.ExecContext(ctx, `
		INSERT INTO remediation_proposals
		  (id, trace_id, use_case, namespace, service, proposed_action,
		   action_params, analysis, status, source_event_id, created_at, expires_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending',$9,NOW(),$10)`,
		p.ID, p.TraceID, p.UseCase, p.Namespace, p.Service, p.ProposedAction,
		paramsJSON, analysisJSON, p.SourceEventID, p.ExpiresAt)
	return err
}

const remediationSelectCols = `
	SELECT id, trace_id, use_case, namespace, service, proposed_action,
	       action_params, analysis, status, source_event_id,
	       created_at, expires_at, decided_at, decided_by, executed_at, result, error,
	       tenant_id, COALESCE(cluster_id, '')
	FROM remediation_proposals`

func scanRemediationProposal(row interface{ Scan(...any) error }) (*RemediationProposal, error) {
	var p RemediationProposal
	var paramsJSON, analysisJSON, resultJSON []byte
	err := row.Scan(
		&p.ID, &p.TraceID, &p.UseCase, &p.Namespace, &p.Service, &p.ProposedAction,
		&paramsJSON, &analysisJSON, &p.Status, &p.SourceEventID,
		&p.CreatedAt, &p.ExpiresAt, &p.DecidedAt, &p.DecidedBy, &p.ExecutedAt, &resultJSON, &p.Error,
		&p.TenantID, &p.ClusterID)
	if err != nil {
		return nil, err
	}
	_ = json.Unmarshal(paramsJSON, &p.ActionParams)
	_ = json.Unmarshal(analysisJSON, &p.Analysis)
	_ = json.Unmarshal(resultJSON, &p.Result)
	return &p, nil
}

// GetRemediationProposal returns one proposal by ID.
func (c *Client) GetRemediationProposal(ctx context.Context, id string) (*RemediationProposal, error) {
	row := c.db.QueryRowContext(ctx, remediationSelectCols+` WHERE id = $1`, id)
	return scanRemediationProposal(row)
}

// ListRemediationProposals returns proposals newest-first, optionally filtered by status.
// An empty status returns all proposals.
func (c *Client) ListRemediationProposals(ctx context.Context, status string, limit int) ([]RemediationProposal, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	var rows *sql.Rows
	var err error
	if status != "" {
		rows, err = c.db.QueryContext(ctx,
			remediationSelectCols+` WHERE status = $1 ORDER BY created_at DESC LIMIT $2`, status, limit)
	} else {
		rows, err = c.db.QueryContext(ctx,
			remediationSelectCols+` ORDER BY created_at DESC LIMIT $1`, limit)
	}
	if err != nil {
		return nil, fmt.Errorf("list remediation proposals: %w", err)
	}
	defer rows.Close()

	var result []RemediationProposal
	for rows.Next() {
		p, err := scanRemediationProposal(rows)
		if err != nil {
			return nil, fmt.Errorf("scan remediation proposal: %w", err)
		}
		result = append(result, *p)
	}
	return result, rows.Err()
}

// DecideRemediationProposal transitions a proposal from pending to approved/rejected.
// The WHERE status='pending' guard makes this idempotent: a duplicate click or
// webhook retry after the first decision affects zero rows (ok==false), so the
// caller must treat that as "already decided" rather than re-executing.
func (c *Client) DecideRemediationProposal(ctx context.Context, id, status, decidedBy string) (bool, error) {
	res, err := c.db.ExecContext(ctx, `
		UPDATE remediation_proposals
		SET status = $1, decided_at = NOW(), decided_by = $2
		WHERE id = $3 AND status = 'pending'`,
		status, decidedBy, id)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

// ProposalExistsForEvent reports whether a proposal already exists for the
// given source deploy event (dedupes DeploymentGuardian's sweep so the same
// deploy never generates a second proposal).
func (c *Client) ProposalExistsForEvent(ctx context.Context, sourceEventID string) (bool, error) {
	var exists bool
	err := c.db.QueryRowContext(ctx,
		`SELECT EXISTS(SELECT 1 FROM remediation_proposals WHERE source_event_id = $1)`,
		sourceEventID).Scan(&exists)
	return exists, err
}

// CompleteRemediationProposal records the outcome of executing an approved proposal.
func (c *Client) CompleteRemediationProposal(ctx context.Context, id, status string, result map[string]interface{}, errMsg string) error {
	resultJSON, err := json.Marshal(result)
	if err != nil {
		return fmt.Errorf("marshal result: %w", err)
	}
	_, err = c.db.ExecContext(ctx, `
		UPDATE remediation_proposals
		SET status = $1, executed_at = NOW(), result = $2, error = $3
		WHERE id = $4`,
		status, resultJSON, errMsg, id)
	return err
}

// ── Model pricing ────────────────────────────────────────────────────────────

// ModelPricing holds the indicative retail $/MTok rates for one Claude model.
type ModelPricing struct {
	ModelID           string    `json:"model_id"`
	DisplayName       string    `json:"display_name"`
	InputPerMTok      float64   `json:"input_per_mtok"`
	OutputPerMTok     float64   `json:"output_per_mtok"`
	CacheWritePerMTok float64   `json:"cache_write_per_mtok"`
	CacheReadPerMTok  float64   `json:"cache_read_per_mtok"`
	UpdatedAt         time.Time `json:"updated_at"`
}

// ListModelPricing returns all rows from the model_pricing table, sorted by model_id.
func (c *Client) ListModelPricing(ctx context.Context) ([]ModelPricing, error) {
	rows, err := c.db.QueryContext(ctx, `
		SELECT model_id, display_name, input_per_mtok, output_per_mtok,
		       cache_write_per_mtok, cache_read_per_mtok, updated_at
		FROM model_pricing ORDER BY model_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var result []ModelPricing
	for rows.Next() {
		var p ModelPricing
		if err := rows.Scan(&p.ModelID, &p.DisplayName, &p.InputPerMTok, &p.OutputPerMTok,
			&p.CacheWritePerMTok, &p.CacheReadPerMTok, &p.UpdatedAt); err != nil {
			return nil, err
		}
		result = append(result, p)
	}
	return result, rows.Err()
}

// UpsertModelPricing inserts or updates a model pricing row.
func (c *Client) UpsertModelPricing(ctx context.Context, p *ModelPricing) error {
	_, err := c.db.ExecContext(ctx, `
		INSERT INTO model_pricing
		  (model_id, display_name, input_per_mtok, output_per_mtok, cache_write_per_mtok, cache_read_per_mtok, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, NOW())
		ON CONFLICT (model_id) DO UPDATE SET
		  display_name         = EXCLUDED.display_name,
		  input_per_mtok       = EXCLUDED.input_per_mtok,
		  output_per_mtok      = EXCLUDED.output_per_mtok,
		  cache_write_per_mtok = EXCLUDED.cache_write_per_mtok,
		  cache_read_per_mtok  = EXCLUDED.cache_read_per_mtok,
		  updated_at           = NOW()`,
		p.ModelID, p.DisplayName, p.InputPerMTok, p.OutputPerMTok,
		p.CacheWritePerMTok, p.CacheReadPerMTok)
	return err
}

// ── Service dependencies (mined from log text, see k8fy/service_topology.py) ─

// ServiceDependency is one directed edge in the mined service-call graph:
// from_service was observed (via log text) calling to_service, within namespace.
type ServiceDependency struct {
	ID            string    `json:"id"`
	Namespace     string    `json:"namespace"`
	FromService   string    `json:"from_service"`
	ToService     string    `json:"to_service"`
	EvidenceCount int       `json:"evidence_count"`
	// "service" | "cross_namespace" | "external" — see the schema note. The
	// UI must keep the tiers visually distinct; the external tier is a
	// heuristic, not a validated fact.
	TargetKind    string    `json:"target_kind"`
	FirstSeen     time.Time `json:"first_seen"`
	LastSeen      time.Time `json:"last_seen"`
	TenantID      string    `json:"tenant_id"`
	ClusterID     string    `json:"cluster_id,omitempty"`
	// ROADMAP P27 phase 2. Port is 0 when never captured for this edge (only
	// the bare host:port log form carries one) — a row is now split by port,
	// so two ports for the same (from, to) pair are two distinct rows, one of
	// which may be port=0 (unknown). The outcome counters are cumulative
	// since first_seen; there is no separate "unknown" counter because it is
	// EvidenceCount minus the sum of the three below.
	Port                 int `json:"port"`
	OutcomeSuccessCount  int `json:"outcome_success_count"`
	OutcomeFailureCount  int `json:"outcome_failure_count"`
	OutcomeTimeoutCount  int `json:"outcome_timeout_count"`
	// ADR 0032: to_service's agentify.io/expected-failure annotation value,
	// looked up from cluster_services at read time (never stored on this row)
	// so it's always current with the Service's live annotation state and has
	// exactly one writer. Empty means no annotation — the edge is a normal
	// candidate for the unhealthy banner.
	ExpectedFailureReason string `json:"expected_failure_reason,omitempty"`
}

// UpsertServiceDependency records one piece of evidence for a from->to edge —
// increments evidence_count and bumps last_seen if the edge is already known.
// id must be set by the caller (matches CreateIntegration's convention) —
// only used on first insert; ON CONFLICT is keyed by (namespace, from, to).
// setTenantContext scopes the rest of tx to one tenant for every RLS-enabled
// table it touches — set_config(..., true) is the parameterized equivalent
// of SET LOCAL (avoids ever string-interpolating tenantID into SQL text),
// and "true" (is_local) means it resets automatically at commit/rollback,
// so it can never leak onto a pooled connection reused by a different
// request afterward.
func setTenantContext(ctx context.Context, tx *sql.Tx, tenantID string) error {
	_, err := tx.ExecContext(ctx, `SELECT set_config('app.current_tenant_id', $1, true)`, tenantID)
	return err
}

// UpsertServiceDependency records one piece of evidence for a from->to edge,
// scoped to (tenantID, clusterID) — ADR 0022. Runs inside a transaction so
// the tenant scoping above only ever applies to this one call.
//
// port/outcome (ROADMAP P27 phase 2, ADR 0031): port is 0 when the caller
// never captured one (the unique key includes port, so a row is per-port —
// see the schema migration's comment for why 0, not NULL); outcome is one
// of "success"/"failure"/"timeout"/"" (unknown, e.g. an older collector that
// predates this phase). The CASE expressions below increment at most one
// outcome counter per call, never more than one, since a single observation
// has exactly one outcome or none.
func (c *Client) UpsertServiceDependency(ctx context.Context, id, tenantID, clusterID, namespace, fromService, toService, targetKind string, port int, outcome string) error {
	if targetKind == "" {
		targetKind = "service" // an older collector reports only validated edges
	}
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return fmt.Errorf("set tenant context: %w", err)
	}
	_, err = tx.ExecContext(ctx, `
		INSERT INTO service_dependencies (id, namespace, from_service, to_service, tenant_id, cluster_id, target_kind, port,
		                                   evidence_count, outcome_success_count, outcome_failure_count, outcome_timeout_count,
		                                   first_seen, last_seen)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 1,
		        CASE WHEN $9 = 'success' THEN 1 ELSE 0 END,
		        CASE WHEN $9 = 'failure' THEN 1 ELSE 0 END,
		        CASE WHEN $9 = 'timeout' THEN 1 ELSE 0 END,
		        NOW(), NOW())
		ON CONFLICT (tenant_id, cluster_id, namespace, from_service, to_service, port) DO UPDATE SET
		  evidence_count         = service_dependencies.evidence_count + 1,
		  outcome_success_count  = service_dependencies.outcome_success_count + CASE WHEN $9 = 'success' THEN 1 ELSE 0 END,
		  outcome_failure_count  = service_dependencies.outcome_failure_count + CASE WHEN $9 = 'failure' THEN 1 ELSE 0 END,
		  outcome_timeout_count  = service_dependencies.outcome_timeout_count + CASE WHEN $9 = 'timeout' THEN 1 ELSE 0 END,
		  -- Kind can be corrected on a later sighting (a host first seen as
		  -- external, later resolved as cross-namespace once its namespace is
		  -- tracked) but never downgraded to the default by an older caller.
		  target_kind    = COALESCE(NULLIF(EXCLUDED.target_kind, ''), service_dependencies.target_kind),
		  last_seen      = NOW()`,
		id, namespace, fromService, toService, tenantID, clusterID, targetKind, port, outcome)
	if err != nil {
		return err
	}
	return tx.Commit()
}

// ScanCoverage is one service's scan accounting — the denominator for
// interpreting service_dependencies.evidence_count (ROADMAP P27 phase 1).
type ScanCoverage struct {
	Namespace    string    `json:"namespace"`
	Service      string    `json:"service"`
	ScanCycles   int       `json:"scan_cycles"`
	PodsSeen     int       `json:"pods_seen"`
	PodsSampled  int       `json:"pods_sampled"`
	LogsReadable int       `json:"logs_readable"`
	LogLines     int64     `json:"log_lines"`
	FirstScan    time.Time `json:"first_scan"`
	LastScan     time.Time `json:"last_scan"`
	TenantID     string    `json:"tenant_id"`
	ClusterID    string    `json:"cluster_id,omitempty"`
}

// UpsertScanCoverage adds one scan cycle's counts for one service.
//
// Counters are INCREMENTED rather than replaced, for the same reason
// UpsertServiceDependency increments evidence_count: the collector reports what
// it did this cycle and has no durable memory across restarts, so the Hub is
// the only place a cumulative total can live. scan_cycles is incremented by the
// caller passing 1, not derived, so a namespace scanned but yielding no pods
// still advances the denominator — which is exactly the case that reveals a
// service whose pods are never sampled.
func (c *Client) UpsertScanCoverage(ctx context.Context, tenantID, clusterID, namespace, service string, cycles, podsSeen, podsSampled, logsReadable int, logLines int64) error {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return fmt.Errorf("set tenant context: %w", err)
	}
	_, err = tx.ExecContext(ctx, `
		INSERT INTO scan_coverage (tenant_id, cluster_id, namespace, service,
		                           scan_cycles, pods_seen, pods_sampled, logs_readable, log_lines,
		                           first_scan, last_scan)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW(), NOW())
		ON CONFLICT (tenant_id, cluster_id, namespace, service) DO UPDATE SET
		  scan_cycles   = scan_coverage.scan_cycles   + EXCLUDED.scan_cycles,
		  pods_seen     = scan_coverage.pods_seen     + EXCLUDED.pods_seen,
		  pods_sampled  = scan_coverage.pods_sampled  + EXCLUDED.pods_sampled,
		  logs_readable = scan_coverage.logs_readable + EXCLUDED.logs_readable,
		  log_lines     = scan_coverage.log_lines     + EXCLUDED.log_lines,
		  last_scan     = NOW()`,
		tenantID, clusterID, namespace, service, cycles, podsSeen, podsSampled, logsReadable, logLines)
	if err != nil {
		return err
	}
	return tx.Commit()
}

// ListScanCoverage returns scan accounting for one namespace across every
// cluster belonging to tenantID, same scoping rationale as
// ListServiceDependencies. RLS enforces the tenant boundary.
func (c *Client) ListScanCoverage(ctx context.Context, tenantID, namespace string) ([]ScanCoverage, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	rows, err := tx.QueryContext(ctx, `
		SELECT namespace, service, scan_cycles, pods_seen, pods_sampled, logs_readable, log_lines,
		       first_scan, last_scan, tenant_id, COALESCE(cluster_id, '')
		FROM scan_coverage WHERE namespace = $1 ORDER BY service`, namespace)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var result []ScanCoverage
	for rows.Next() {
		var c ScanCoverage
		if err := rows.Scan(&c.Namespace, &c.Service, &c.ScanCycles, &c.PodsSeen, &c.PodsSampled,
			&c.LogsReadable, &c.LogLines, &c.FirstScan, &c.LastScan, &c.TenantID, &c.ClusterID); err != nil {
			return nil, err
		}
		result = append(result, c)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return result, nil
}

// ListServiceDependencies returns every mined edge for one namespace, across
// every cluster belonging to tenantID (deliberately not filtered by cluster —
// a tenant's clusters' edges surfacing together is what enables cross-cluster
// dependency correlation, P18 use case #4), most-evidenced first. RLS (not
// this query's WHERE clause) is what actually enforces the tenant boundary.
func (c *Client) ListServiceDependencies(ctx context.Context, tenantID, namespace string) ([]ServiceDependency, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	// LEFT JOIN cluster_services (ADR 0032): to_service's expected-failure
	// reason, if the collector's live scan found the annotation on that
	// Service. Joined on cluster_id too (not just namespace+service) so the
	// same service name in two different clusters for this tenant can't
	// fan out this query into duplicate rows.
	rows, err := tx.QueryContext(ctx, `
		SELECT sd.id, sd.namespace, sd.from_service, sd.to_service, sd.evidence_count, sd.first_seen, sd.last_seen,
		       sd.tenant_id, COALESCE(sd.cluster_id, ''), COALESCE(sd.target_kind, 'service'),
		       sd.port, sd.outcome_success_count, sd.outcome_failure_count, sd.outcome_timeout_count,
		       COALESCE(cs.expected_failure_reason, '')
		FROM service_dependencies sd
		LEFT JOIN cluster_services cs
		  ON cs.tenant_id = sd.tenant_id AND cs.cluster_id = sd.cluster_id
		 AND cs.namespace = sd.namespace AND cs.service = sd.to_service
		WHERE sd.namespace = $1 ORDER BY sd.evidence_count DESC`, namespace)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var result []ServiceDependency
	for rows.Next() {
		var d ServiceDependency
		if err := rows.Scan(&d.ID, &d.Namespace, &d.FromService, &d.ToService,
			&d.EvidenceCount, &d.FirstSeen, &d.LastSeen, &d.TenantID, &d.ClusterID, &d.TargetKind,
			&d.Port, &d.OutcomeSuccessCount, &d.OutcomeFailureCount, &d.OutcomeTimeoutCount,
			&d.ExpectedFailureReason); err != nil {
			return nil, err
		}
		result = append(result, d)
	}
	return result, rows.Err()
}

// ── Service->cluster registry (ROADMAP P16 / ADR 0023) ──────────────────────

// ServiceEntry is one Service known to a cluster's Discovery collector: its
// name plus its K8s selector (ADR 0029 — lets a centralized Glue-based
// dependency miner, which has no live cluster access, replicate the same
// selector-to-pod-label matching main.go's _service_for_pod does live).
// Selector is nil/empty for a Service with no selector (e.g. manually-managed
// Endpoints) — never matches any pod, same as live matching's behavior.
type ServiceEntry struct {
	Name     string
	Selector map[string]string

	// Service profile (ROADMAP P22). Zero values mean "the collector did not
	// report it" — an older build, or a Service with no matching workload,
	// which is itself worth surfacing.
	ServiceType     string
	Ports           []ServicePort
	WorkloadKind    string
	ReplicasDesired *int
	ReplicasReady   *int
	Image           string
	Schedule        string

	// ADR 0032: agentify.io/expected-failure annotation value, if the
	// collector's live K8s read found one on this Service. Empty means
	// either no annotation or an older collector that predates this field.
	ExpectedFailureReason string
}

type ServicePort struct {
	Name     string `json:"name,omitempty"`
	Port     int    `json:"port"`
	Protocol string `json:"protocol,omitempty"`
}

// ServiceProfile is one service as the architecture view reads it back.
type ServiceProfile struct {
	Namespace       string        `json:"namespace"`
	Service         string        `json:"service"`
	ServiceType     string        `json:"service_type"`
	Ports           []ServicePort `json:"ports"`
	WorkloadKind    string        `json:"workload_kind"`
	ReplicasDesired *int          `json:"replicas_desired"`
	ReplicasReady   *int          `json:"replicas_ready"`
	Image           string        `json:"image"`
	Schedule        string        `json:"schedule"`
	ClusterID       string        `json:"cluster_id,omitempty"`
	// When the collector last rewrote this row. A profile is DECLARED state, so
	// it only changes on deploy — a profile hours old is normal, and the reason
	// this is exposed is the opposite case: nothing here changes when the
	// collector DIES, so without a timestamp a dead collector and a stable
	// deployment are indistinguishable. Nil = pre-dates the column.
	UpdatedAt *time.Time `json:"updated_at,omitempty"`
}

// UpsertClusterServices replaces the full known-service set for one
// (tenantID, clusterID) — a full delete-then-insert per push, matching
// UpdateIntegrationNamespaces's "reflects live cluster truth" semantics
// rather than an incremental diff: a service that disappeared from the
// collector's scan should disappear from the registry on the next push, not
// linger. byNamespace maps namespace -> that namespace's services.
func (c *Client) UpsertClusterServices(ctx context.Context, tenantID, clusterID string, byNamespace map[string][]ServiceEntry) error {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return fmt.Errorf("set tenant context: %w", err)
	}
	if _, err := tx.ExecContext(ctx,
		`DELETE FROM cluster_services WHERE tenant_id = $1 AND cluster_id = $2`,
		tenantID, clusterID); err != nil {
		return fmt.Errorf("clear stale cluster services: %w", err)
	}
	for namespace, services := range byNamespace {
		for _, service := range services {
			// Ports are stored as JSONB rather than a child table: they are only
			// ever read back with their service, and a Service has a handful.
			ports := service.Ports
			if ports == nil {
				ports = []ServicePort{}
			}
			portsJSON, err := json.Marshal(ports)
			if err != nil {
				return fmt.Errorf("marshal ports for %s/%s: %w", namespace, service.Name, err)
			}
			selectorJSON, err := json.Marshal(service.Selector)
			if err != nil {
				return fmt.Errorf("marshal selector for %s/%s: %w", namespace, service.Name, err)
			}
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO cluster_services (tenant_id, cluster_id, namespace, service, selector,
				                               service_type, ports, workload_kind,
				                               replicas_desired, replicas_ready, image, schedule,
				                               expected_failure_reason, updated_at)
				 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW())`,
				tenantID, clusterID, namespace, service.Name, selectorJSON,
				service.ServiceType, portsJSON, service.WorkloadKind,
				service.ReplicasDesired, service.ReplicasReady, service.Image, service.Schedule,
				service.ExpectedFailureReason); err != nil {
				return fmt.Errorf("insert cluster service %s/%s: %w", namespace, service.Name, err)
			}
		}
	}
	return tx.Commit()
}

// ResolveServiceClusters returns every clusterID (within tenantID) known to
// run (namespace, service) — 0 (unknown), 1 (the common case), or N when the
// same service name exists in more than one of the tenant's clusters. RLS
// (not this query's WHERE clause) is what actually enforces the tenant
// boundary, same convention as ListServiceDependencies.
func (c *Client) ResolveServiceClusters(ctx context.Context, tenantID, namespace, service string) ([]string, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	rows, err := tx.QueryContext(ctx,
		`SELECT DISTINCT cluster_id FROM cluster_services WHERE namespace = $1 AND service = $2`,
		namespace, service)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	clusterIDs := []string{}
	for rows.Next() {
		var clusterID string
		if err := rows.Scan(&clusterID); err != nil {
			return nil, err
		}
		clusterIDs = append(clusterIDs, clusterID)
	}
	return clusterIDs, rows.Err()
}

// ListClusterServices returns every (namespace, service) pair known for
// tenantID as a namespace -> service-names map — the reverse direction of
// ResolveServiceClusters (which pair maps to a cluster; this is every pair a
// tenant has). ADR 0027: this is what replaced the retired k8fy-adapter's
// live DiscoverNamespaces() call for the Hub's own namespace-autocomplete/
// current_state-seeding endpoints — Discovery's existing cluster-inventory
// push already keeps this table fresh, so those endpoints no longer need to
// reach out to a collector live; they just re-read what's already here. RLS
// (not this query's WHERE clause) enforces the tenant boundary, same
// convention as ResolveServiceClusters.
func (c *Client) ListClusterServices(ctx context.Context, tenantID string) (map[string][]string, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	rows, err := tx.QueryContext(ctx, `SELECT DISTINCT namespace, service FROM cluster_services ORDER BY namespace, service`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	result := map[string][]string{}
	for rows.Next() {
		var namespace, service string
		if err := rows.Scan(&namespace, &service); err != nil {
			return nil, err
		}
		result[namespace] = append(result[namespace], service)
	}
	return result, rows.Err()
}

// ListServiceProfiles returns what every service in one namespace IS —
// workload kind, service type, ports, replicas, image — across every cluster
// belonging to tenantID (same scoping rationale as ListServiceDependencies).
//
// This is the read that turns anonymous boxes on the dependency diagram into
// an architecture view; RLS enforces the tenant boundary.
func (c *Client) ListServiceProfiles(ctx context.Context, tenantID, namespace string) ([]ServiceProfile, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	rows, err := tx.QueryContext(ctx, `
		SELECT namespace, service, service_type, ports, workload_kind,
		       replicas_desired, replicas_ready, image, schedule, COALESCE(cluster_id, ''),
		       updated_at
		FROM cluster_services WHERE namespace = $1 ORDER BY service`, namespace)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []ServiceProfile
	for rows.Next() {
		var p ServiceProfile
		var portsJSON []byte
		// updated_at is `TIMESTAMP DEFAULT NOW()` — nullable in the DDL, so it
		// cannot scan into a time.Time. NullTime keeps a NULL as "unknown"
		// rather than as the zero time, which the UI would render as 1 AD.
		var updatedAt sql.NullTime
		if err := rows.Scan(&p.Namespace, &p.Service, &p.ServiceType, &portsJSON, &p.WorkloadKind,
			&p.ReplicasDesired, &p.ReplicasReady, &p.Image, &p.Schedule, &p.ClusterID,
			&updatedAt); err != nil {
			return nil, err
		}
		if updatedAt.Valid {
			t := updatedAt.Time
			p.UpdatedAt = &t
		}
		// A malformed ports blob must not fail the whole namespace — the rest
		// of the profile is still useful.
		if len(portsJSON) > 0 {
			if err := json.Unmarshal(portsJSON, &p.Ports); err != nil {
				c.logger.Warn("bad ports JSON on cluster_services row",
					"namespace", p.Namespace, "service", p.Service, "error", err)
				p.Ports = nil
			}
		}
		if p.Ports == nil {
			p.Ports = []ServicePort{}
		}
		out = append(out, p)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return out, nil
}

// ListClusterServiceSelectors returns one specific cluster's known services
// in one namespace as a service-name -> selector map (ADR 0029). Unlike
// ResolveServiceClusters/ListClusterServices above, this filters by
// clusterID explicitly — the caller (a centralized Glue-based miner) needs
// exactly one cluster's own selector definitions to match against that
// cluster's own log rows, not a cross-cluster merge (a service name could
// mean a different Service, with a different selector, in each cluster).
func (c *Client) ListClusterServiceSelectors(ctx context.Context, tenantID, clusterID, namespace string) (map[string]map[string]string, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	rows, err := tx.QueryContext(ctx,
		`SELECT service, selector FROM cluster_services WHERE cluster_id = $1 AND namespace = $2`,
		clusterID, namespace)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	result := map[string]map[string]string{}
	for rows.Next() {
		var service string
		var selectorJSON []byte
		if err := rows.Scan(&service, &selectorJSON); err != nil {
			return nil, err
		}
		selector := map[string]string{}
		if len(selectorJSON) > 0 {
			if err := json.Unmarshal(selectorJSON, &selector); err != nil {
				return nil, fmt.Errorf("unmarshal selector for service %s: %w", service, err)
			}
		}
		result[service] = selector
	}
	return result, rows.Err()
}

// ── Ingress/entry-point mapping (ROADMAP P18 use case #3) ───────────────────

// IngressEndpoint is one (namespace, kind, name, host, backend_service) row
// from a cluster's Ingress/Gateway+HTTPRoute/OpenShift Route scan. Host and
// BackendService may each be empty when only one side of a mapping is known.
type IngressEndpoint struct {
	Namespace      string
	Kind           string // "ingress" | "httproute" | "route"
	Name           string
	Host           string
	BackendService string
}

// UpsertClusterIngress replaces the full entry-point set for one (tenantID,
// clusterID) — a full delete-then-insert per push, same "reflects live
// cluster truth" semantics as UpsertClusterServices: an Ingress/Route removed
// from the cluster disappears from this table on the next push, not linger.
func (c *Client) UpsertClusterIngress(ctx context.Context, tenantID, clusterID string, entries []IngressEndpoint) error {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return fmt.Errorf("set tenant context: %w", err)
	}
	if _, err := tx.ExecContext(ctx,
		`DELETE FROM cluster_ingress_endpoints WHERE tenant_id = $1 AND cluster_id = $2`,
		tenantID, clusterID); err != nil {
		return fmt.Errorf("clear stale cluster ingress endpoints: %w", err)
	}
	for _, e := range entries {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO cluster_ingress_endpoints (tenant_id, cluster_id, namespace, kind, name, host, backend_service, updated_at)
			 VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
			 ON CONFLICT (tenant_id, cluster_id, namespace, kind, name, host, backend_service) DO NOTHING`,
			tenantID, clusterID, e.Namespace, e.Kind, e.Name, e.Host, e.BackendService); err != nil {
			return fmt.Errorf("insert cluster ingress endpoint %s/%s/%s: %w", e.Namespace, e.Kind, e.Name, err)
		}
	}
	return tx.Commit()
}

// ListClusterIngress returns every entry-point mapping (within tenantID) for
// one namespace — 0..N rows, one per distinct (cluster, kind, name, host,
// backend_service) combination. RLS (not this query's WHERE clause) is what
// actually enforces the tenant boundary, same convention as ResolveServiceClusters.
func (c *Client) ListClusterIngress(ctx context.Context, tenantID, namespace string) ([]IngressEndpoint, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	rows, err := tx.QueryContext(ctx,
		`SELECT namespace, kind, name, host, backend_service FROM cluster_ingress_endpoints WHERE namespace = $1`,
		namespace)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	result := []IngressEndpoint{}
	for rows.Next() {
		var e IngressEndpoint
		if err := rows.Scan(&e.Namespace, &e.Kind, &e.Name, &e.Host, &e.BackendService); err != nil {
			return nil, err
		}
		result = append(result, e)
	}
	return result, rows.Err()
}

// ── Fleet-wide health/version snapshot (ROADMAP P18 use case #5) ────────────

// ClusterHealthSnapshot is one cluster's most recent health/version report.
type ClusterHealthSnapshot struct {
	ClusterID  string
	TenantID   string
	K8sVersion string
	PodsTotal  int
	PodsReady  int
	UpdatedAt  time.Time
}

// ServiceHealth is the live state of one service's pods, aggregated from the
// per-pod rows the collector's watch keeps in current_state.
//
// This is what turns a structural diagram into one worth opening during an
// incident: "Deployment · 0/2" says something is wrong, "0/2 · 47 restarts ·
// CrashLoopBackOff" says what.
type ServiceHealth struct {
	Namespace string   `json:"namespace"`
	Service   string   `json:"service"`
	Pods      int      `json:"pods"`
	Ready     int      `json:"ready"`
	Restarts  int      `json:"restarts"`
	Phases    []string `json:"phases"` // distinct pod phases, Running first excluded when others exist
	// The most recent current_state write across this service's pods — i.e.
	// when the pod watch last said anything at all about it. This is the
	// fastest-moving data the UI draws and was the only layer with no
	// staleness signal, so "0 restarts, all ready" rendered identically
	// whether it was 30 seconds or 3 hours old. Nil = no timestamp on any row.
	ObservedAt *time.Time `json:"observed_at,omitempty"`
}

// ListServiceHealth aggregates live pod state per service for one namespace.
//
// TENANT SCOPING WAS EXPLICIT-ONLY HERE until ROADMAP OPS-10 (ADR 0022
// amendment), unlike every other RLS-backed read in this file.
// current_state now has a real `tenant_isolation` policy — this query's own
// `WHERE tenant_id = $1` predicate is kept anyway, belt-and-suspenders, but
// it is no longer the only thing standing between one tenant's service
// inventory and another's.
//
// Attribution comes from payload->>'service', written at push time by the
// collector (service_index). Pods with no attributable Service are counted
// separately by the caller rather than silently folded in.
func (c *Client) ListServiceHealth(ctx context.Context, tenantID, namespace string) ([]ServiceHealth, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}

	rows, err := tx.QueryContext(ctx, `
		SELECT payload->>'service'                                        AS service,
		       count(*)                                                   AS pods,
		       count(*) FILTER (WHERE payload->>'ready' = 'true')         AS ready,
		       COALESCE(SUM(COALESCE(NULLIF(payload->>'restarts', '')::int, 0)), 0) AS restarts,
		       -- string_agg rather than array_agg: lib/pq is imported blank here
		       -- (driver registration only), and a comma-joined TEXT needs no
		       -- pq.StringArray and no change to that import.
		       COALESCE(string_agg(DISTINCT payload->>'phase', ','), '')   AS phases,
		       -- When the watch last said ANYTHING about this service. Not a
		       -- proxy for pod age: current_state is upserted on every event,
		       -- so this going quiet means the COLLECTOR went quiet, which is
		       -- the failure the UI could not previously distinguish from a
		       -- stable deployment.
		       MAX(updated_at)                                             AS observed_at
		  FROM current_state
		 WHERE tenant_id = $1
		   AND event_namespace = 'k8fy.live-state'
		   AND payload->>'namespace' = $2
		   AND COALESCE(payload->>'service', '') <> ''
		   -- Exclude pods the watch saw deleted — belt-and-suspenders, kept
		   -- even now that ingester.go's storeEvent actually DELETEs the row
		   -- for a "_deleted" event (ROADMAP OPS-12, CurrentState.Delete)
		   -- rather than upserting a tombstone. Two reasons this predicate
		   -- still earns its place instead of being removed as redundant:
		   --
		   --   1. Rows written before that fix shipped still had their
		   --      tombstone upserted, not deleted, and nothing backfills them.
		   --   2. The fix is necessary but NOT sufficient going forward: a pod
		   --      that vanishes while the watch is reconnecting never produces
		   --      a DELETED event at all, so no delete call ever fires and its
		   --      row persists indefinitely. That residual gap is why the
		   --      ready/desired RATIO shown in the UI comes from the
		   --      Deployment status instead of from these counts.
		   AND COALESCE(event_type, '') <> 'pod_deleted'
		 GROUP BY 1
		 ORDER BY 1`, tenantID, namespace)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []ServiceHealth
	for rows.Next() {
		var h ServiceHealth
		var phaseCSV string
		var observedAt sql.NullTime // nullable column; see the profile scan above
		if err := rows.Scan(&h.Service, &h.Pods, &h.Ready, &h.Restarts, &phaseCSV,
			&observedAt); err != nil {
			return nil, err
		}
		if observedAt.Valid {
			t := observedAt.Time
			h.ObservedAt = &t
		}
		h.Namespace = namespace
		phases := strings.Split(phaseCSV, ",")
		// "Running" alongside a failure phase is noise — the failure is the
		// headline. Keep Running only when it is the whole story.
		h.Phases = nil
		for _, ph := range phases {
			if ph != "" && ph != "Running" {
				h.Phases = append(h.Phases, ph)
			}
		}
		if h.Phases == nil {
			for _, ph := range phases {
				if ph != "" {
					h.Phases = append(h.Phases, ph)
				}
			}
		}
		if h.Phases == nil {
			h.Phases = []string{}
		}
		out = append(out, h)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	rows.Close()
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return out, nil
}

// SecurityFinding is one deployment-security check's result for one resource
// (ROADMAP P30 phase 1, ADR 0033). Confidence and VerifiedByEngagementID are
// Phase 2+ columns, inert in phase 1 (always "config-only"/empty).
type SecurityFinding struct {
	Namespace              string    `json:"namespace"`
	CheckID                string    `json:"check_id"`
	ResourceKind           string    `json:"resource_kind"`
	ResourceName           string    `json:"resource_name"`
	Severity               string    `json:"severity"`
	Evidence               string    `json:"evidence"`
	// TargetHost (ROADMAP P30 phase 2): the concrete host an active-
	// verification engagement checks. Empty when the check that produced
	// this finding has no phase-2 technique mapped to it yet.
	TargetHost             string    `json:"target_host,omitempty"`
	Status                 string    `json:"status"`
	Confidence             string    `json:"confidence"`
	VerifiedByEngagementID string    `json:"verified_by_engagement_id,omitempty"`
	FirstSeen              time.Time `json:"first_seen"`
	LastSeen               time.Time `json:"last_seen"`
	TenantID               string    `json:"tenant_id"`
	ClusterID              string    `json:"cluster_id,omitempty"`
}

// UpsertSecurityFindings records this scan cycle's complete finding set for
// one (tenant, cluster, namespace) — findings are config-state, not
// accumulated evidence, so this follows cluster_services' "full replace
// reflects live truth" family rather than scan_coverage's accumulate-forever
// counters, with one addition for auditability: a finding no longer present
// is marked status='resolved', never deleted, so history survives a fixed
// issue. A finding already 'resolved' that reappears flips back to 'open'
// (a regression); one already 'open' or 'acknowledged' is left as-is, so
// acknowledging a still-present issue doesn't get silently undone by the
// very next scan cycle finding it again.
func (c *Client) UpsertSecurityFindings(ctx context.Context, tenantID, clusterID, namespace string, findings []SecurityFinding) error {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return fmt.Errorf("set tenant context: %w", err)
	}

	for _, f := range findings {
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO security_findings (tenant_id, cluster_id, namespace, check_id, resource_kind,
			                                resource_name, severity, evidence, target_host, status, confidence,
			                                first_seen, last_seen)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'open', 'config-only', NOW(), NOW())
			ON CONFLICT (tenant_id, cluster_id, namespace, check_id, resource_kind, resource_name) DO UPDATE SET
			  severity    = EXCLUDED.severity,
			  evidence    = EXCLUDED.evidence,
			  target_host = EXCLUDED.target_host,
			  last_seen   = NOW(),
			  status      = CASE WHEN security_findings.status = 'resolved' THEN 'open' ELSE security_findings.status END`,
			tenantID, clusterID, namespace, f.CheckID, f.ResourceKind, f.ResourceName, f.Severity, f.Evidence, f.TargetHost); err != nil {
			return fmt.Errorf("upsert finding %s/%s/%s: %w", f.CheckID, f.ResourceKind, f.ResourceName, err)
		}
	}

	// Anything still 'open'/'acknowledged' for this namespace that wasn't in
	// this push no longer reproduces — resolve it. Built as a dynamic VALUES
	// list (same dynamic-SQL convention Client.Query already uses for events'
	// optional filters) rather than a Go-side array type, matching this
	// file's existing avoidance of pq's array marshaling elsewhere.
	if len(findings) == 0 {
		if _, err := tx.ExecContext(ctx, `
			UPDATE security_findings SET status = 'resolved'
			WHERE tenant_id = $1 AND cluster_id = $2 AND namespace = $3 AND status != 'resolved'`,
			tenantID, clusterID, namespace); err != nil {
			return fmt.Errorf("resolve stale findings: %w", err)
		}
	} else {
		var b strings.Builder
		args := []interface{}{tenantID, clusterID, namespace}
		b.WriteString(`
			UPDATE security_findings SET status = 'resolved'
			WHERE tenant_id = $1 AND cluster_id = $2 AND namespace = $3 AND status != 'resolved'
			  AND NOT EXISTS (SELECT 1 FROM (VALUES `)
		for i, f := range findings {
			if i > 0 {
				b.WriteString(", ")
			}
			args = append(args, f.CheckID, f.ResourceKind, f.ResourceName)
			n := len(args)
			fmt.Fprintf(&b, "($%d, $%d, $%d)", n-2, n-1, n)
		}
		b.WriteString(`) AS pushed(check_id, resource_kind, resource_name)
			  WHERE pushed.check_id = security_findings.check_id
			    AND pushed.resource_kind = security_findings.resource_kind
			    AND pushed.resource_name = security_findings.resource_name)`)
		if _, err := tx.ExecContext(ctx, b.String(), args...); err != nil {
			return fmt.Errorf("resolve stale findings: %w", err)
		}
	}

	return tx.Commit()
}

// ListSecurityFindings returns every finding for one namespace across every
// cluster belonging to tenantID, same scoping rationale as
// ListServiceDependencies. RLS enforces the tenant boundary.
func (c *Client) ListSecurityFindings(ctx context.Context, tenantID, namespace string) ([]SecurityFinding, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	rows, err := tx.QueryContext(ctx, `
		SELECT namespace, check_id, resource_kind, resource_name, severity, evidence, target_host, status,
		       confidence, COALESCE(verified_by_engagement_id, ''), first_seen, last_seen,
		       tenant_id, COALESCE(cluster_id, '')
		FROM security_findings WHERE namespace = $1
		ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, check_id`,
		namespace)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var result []SecurityFinding
	for rows.Next() {
		var f SecurityFinding
		if err := rows.Scan(&f.Namespace, &f.CheckID, &f.ResourceKind, &f.ResourceName, &f.Severity,
			&f.Evidence, &f.TargetHost, &f.Status, &f.Confidence, &f.VerifiedByEngagementID, &f.FirstSeen, &f.LastSeen,
			&f.TenantID, &f.ClusterID); err != nil {
			return nil, err
		}
		result = append(result, f)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return result, nil
}

// GetSecurityFinding is a point lookup by natural key (ROADMAP P30 phase 2)
// — validates a finding exists (and reads its target_host) before an
// engagement is created against it, so an engagement can never be requested
// for a finding that isn't real.
func (c *Client) GetSecurityFinding(ctx context.Context, tenantID, namespace, checkID, resourceKind, resourceName string) (*SecurityFinding, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	row := tx.QueryRowContext(ctx, `
		SELECT namespace, check_id, resource_kind, resource_name, severity, evidence, target_host, status,
		       confidence, COALESCE(verified_by_engagement_id, ''), first_seen, last_seen,
		       tenant_id, COALESCE(cluster_id, '')
		FROM security_findings
		WHERE namespace = $1 AND check_id = $2 AND resource_kind = $3 AND resource_name = $4`,
		namespace, checkID, resourceKind, resourceName)
	var f SecurityFinding
	if err := row.Scan(&f.Namespace, &f.CheckID, &f.ResourceKind, &f.ResourceName, &f.Severity,
		&f.Evidence, &f.TargetHost, &f.Status, &f.Confidence, &f.VerifiedByEngagementID, &f.FirstSeen, &f.LastSeen,
		&f.TenantID, &f.ClusterID); err != nil {
		return nil, err
	}
	return &f, tx.Commit()
}

// ── Security engagements (ROADMAP P30 phases 2-4, ADR 0033) ─────────────────

// SecurityEngagement is one propose→approve/reject→execute record
// authorizing exactly one active-verification technique against exactly one
// existing SecurityFinding. Producing one makes no network calls; only an
// approved engagement is dispatched. Unlike RemediationProposal (no RLS at
// all — see the migration's comment), every method here runs inside a
// tenant-scoped transaction, so a tenantID is required even for a
// point-lookup by ID.
type SecurityEngagement struct {
	ID                 string
	TenantID           string
	ClusterID          string
	Phase              int
	Technique          string
	TargetNamespace    string
	TargetCheckID      string
	TargetResourceKind string
	TargetResourceName string
	Scope              map[string]interface{}
	Status             string // pending | approved | rejected | active | completed | failed | expired
	RequestedBy        string
	ApprovedBy         string
	CreatedAt          time.Time
	ExpiresAt          time.Time
	DecidedAt          *time.Time
	StartedAt          *time.Time
	CompletedAt        *time.Time
	Result             map[string]interface{}
	Error              string
}

// CreateSecurityEngagement inserts a new pending engagement. e.ID and
// e.TenantID must be set by the caller.
func (c *Client) CreateSecurityEngagement(ctx context.Context, e *SecurityEngagement) error {
	scopeJSON, err := json.Marshal(e.Scope)
	if err != nil {
		return fmt.Errorf("marshal scope: %w", err)
	}
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, e.TenantID); err != nil {
		return fmt.Errorf("set tenant context: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO security_engagements
		  (id, tenant_id, cluster_id, phase, technique, target_namespace,
		   target_check_id, target_resource_kind, target_resource_name,
		   scope, status, requested_by, created_at, expires_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'pending',$11,NOW(),$12)`,
		e.ID, e.TenantID, e.ClusterID, e.Phase, e.Technique, e.TargetNamespace,
		e.TargetCheckID, e.TargetResourceKind, e.TargetResourceName,
		scopeJSON, e.RequestedBy, e.ExpiresAt); err != nil {
		return fmt.Errorf("insert security engagement: %w", err)
	}
	return tx.Commit()
}

const securityEngagementSelectCols = `
	SELECT id, tenant_id, COALESCE(cluster_id, ''), phase, technique, target_namespace,
	       target_check_id, target_resource_kind, target_resource_name, scope,
	       status, requested_by, approved_by, created_at, expires_at,
	       decided_at, started_at, completed_at, result, error
	FROM security_engagements`

func scanSecurityEngagement(row interface{ Scan(...any) error }) (*SecurityEngagement, error) {
	var e SecurityEngagement
	var scopeJSON, resultJSON []byte
	if err := row.Scan(
		&e.ID, &e.TenantID, &e.ClusterID, &e.Phase, &e.Technique, &e.TargetNamespace,
		&e.TargetCheckID, &e.TargetResourceKind, &e.TargetResourceName, &scopeJSON,
		&e.Status, &e.RequestedBy, &e.ApprovedBy, &e.CreatedAt, &e.ExpiresAt,
		&e.DecidedAt, &e.StartedAt, &e.CompletedAt, &resultJSON, &e.Error,
	); err != nil {
		return nil, err
	}
	_ = json.Unmarshal(scopeJSON, &e.Scope)
	_ = json.Unmarshal(resultJSON, &e.Result)
	return &e, nil
}

// GetSecurityEngagement returns one engagement by ID, scoped to tenantID —
// RLS also enforces this, but the WHERE clause keeps the query correct even
// if a future caller forgets to set tenant context.
func (c *Client) GetSecurityEngagement(ctx context.Context, tenantID, id string) (*SecurityEngagement, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	row := tx.QueryRowContext(ctx, securityEngagementSelectCols+` WHERE id = $1 AND tenant_id = $2`, id, tenantID)
	e, err := scanSecurityEngagement(row)
	if err != nil {
		return nil, err
	}
	return e, tx.Commit()
}

// ListSecurityEngagements returns engagements newest-first for tenantID,
// optionally filtered by status. An empty status returns all.
func (c *Client) ListSecurityEngagements(ctx context.Context, tenantID, status string, limit int) ([]SecurityEngagement, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	var rows *sql.Rows
	if status != "" {
		rows, err = tx.QueryContext(ctx,
			securityEngagementSelectCols+` WHERE status = $1 ORDER BY created_at DESC LIMIT $2`, status, limit)
	} else {
		rows, err = tx.QueryContext(ctx,
			securityEngagementSelectCols+` ORDER BY created_at DESC LIMIT $1`, limit)
	}
	if err != nil {
		return nil, fmt.Errorf("list security engagements: %w", err)
	}
	defer rows.Close()

	var result []SecurityEngagement
	for rows.Next() {
		e, err := scanSecurityEngagement(rows)
		if err != nil {
			return nil, fmt.Errorf("scan security engagement: %w", err)
		}
		result = append(result, *e)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return result, tx.Commit()
}

// DecideSecurityEngagement transitions an engagement from pending to
// approved/rejected. The WHERE status='pending' guard makes this idempotent,
// same reasoning as DecideRemediationProposal: a duplicate click or retry
// after the first decision affects zero rows (ok==false), which the caller
// must treat as "already decided," never as a reason to re-dispatch.
func (c *Client) DecideSecurityEngagement(ctx context.Context, tenantID, id, status, approvedBy string) (bool, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return false, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return false, fmt.Errorf("set tenant context: %w", err)
	}
	res, err := tx.ExecContext(ctx, `
		UPDATE security_engagements
		SET status = $1, decided_at = NOW(), approved_by = $2
		WHERE id = $3 AND status = 'pending'`,
		status, approvedBy, id)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return false, tx.Commit()
	}
	return true, tx.Commit()
}

// CompleteSecurityEngagement records the outcome of dispatching an approved
// engagement, and — only when confirmed is non-nil, i.e. the verifier
// reached a definitive answer — updates the TARGET finding's confidence in
// the same transaction: true means the finding is confirmed-live, false
// means the check refuted it. A nil confirmed (the dispatch itself failed,
// inconclusive) leaves the finding's confidence at config-only; an
// inconclusive check must never masquerade as a verified one.
func (c *Client) CompleteSecurityEngagement(ctx context.Context, tenantID, id, status string, result map[string]interface{}, errMsg string, confirmed *bool) error {
	resultJSON, err := json.Marshal(result)
	if err != nil {
		return fmt.Errorf("marshal result: %w", err)
	}
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return fmt.Errorf("set tenant context: %w", err)
	}
	row := tx.QueryRowContext(ctx, securityEngagementSelectCols+` WHERE id = $1`, id)
	eng, err := scanSecurityEngagement(row)
	if err != nil {
		return fmt.Errorf("get engagement for completion: %w", err)
	}

	if _, err := tx.ExecContext(ctx, `
		UPDATE security_engagements
		SET status = $1, completed_at = NOW(), result = $2, error = $3
		WHERE id = $4`,
		status, resultJSON, errMsg, id); err != nil {
		return fmt.Errorf("update security engagement: %w", err)
	}

	if confirmed != nil {
		confidence := "refuted"
		if *confirmed {
			confidence = "confirmed-live"
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE security_findings
			SET confidence = $1, verified_by_engagement_id = $2
			WHERE tenant_id = $3 AND cluster_id = $4 AND namespace = $5
			  AND check_id = $6 AND resource_kind = $7 AND resource_name = $8`,
			confidence, id, tenantID, eng.ClusterID, eng.TargetNamespace,
			eng.TargetCheckID, eng.TargetResourceKind, eng.TargetResourceName); err != nil {
			return fmt.Errorf("update target finding confidence: %w", err)
		}
	}

	return tx.Commit()
}

// UpsertClusterHealthSnapshot replaces one cluster's current snapshot —
// always reflects the most recent scan cycle, not history, so this is a
// single-row overwrite-in-place (ON CONFLICT DO UPDATE), not the
// delete-then-insert-a-row-set shape UpsertClusterServices/
// UpsertClusterIngress use for their multi-row registries.
func (c *Client) UpsertClusterHealthSnapshot(ctx context.Context, tenantID, clusterID, k8sVersion string, podsTotal, podsReady int) error {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after a successful Commit

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return fmt.Errorf("set tenant context: %w", err)
	}
	_, err = tx.ExecContext(ctx,
		`INSERT INTO cluster_health_snapshots (cluster_id, tenant_id, k8s_version, pods_total, pods_ready, updated_at)
		 VALUES ($1, $2, $3, $4, $5, NOW())
		 ON CONFLICT (cluster_id) DO UPDATE SET
		   tenant_id = EXCLUDED.tenant_id, k8s_version = EXCLUDED.k8s_version,
		   pods_total = EXCLUDED.pods_total, pods_ready = EXCLUDED.pods_ready, updated_at = NOW()`,
		clusterID, tenantID, k8sVersion, podsTotal, podsReady)
	if err != nil {
		return fmt.Errorf("upsert cluster health snapshot: %w", err)
	}
	return tx.Commit()
}

// ListClusterHealthSnapshots returns every cluster's current snapshot for
// tenantID — the fleet-wide view, no namespace filter (this is cluster-
// level, not namespace-level). RLS (not this query's WHERE clause) is what
// actually enforces the tenant boundary, same convention as
// ListClusterIngress.
func (c *Client) ListClusterHealthSnapshots(ctx context.Context, tenantID string) ([]ClusterHealthSnapshot, error) {
	tx, err := c.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback() //nolint:errcheck // read-only; always rolled back, never committed

	if err := setTenantContext(ctx, tx, tenantID); err != nil {
		return nil, fmt.Errorf("set tenant context: %w", err)
	}
	rows, err := tx.QueryContext(ctx,
		`SELECT cluster_id, tenant_id, k8s_version, pods_total, pods_ready, updated_at FROM cluster_health_snapshots ORDER BY cluster_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	result := []ClusterHealthSnapshot{}
	for rows.Next() {
		var s ClusterHealthSnapshot
		if err := rows.Scan(&s.ClusterID, &s.TenantID, &s.K8sVersion, &s.PodsTotal, &s.PodsReady, &s.UpdatedAt); err != nil {
			return nil, err
		}
		result = append(result, s)
	}
	return result, rows.Err()
}
