package api

import (
	"context"
	"time"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// TraceStore is the query-history CRUD interface implemented by the Postgres client.
type TraceStore interface {
	InsertTrace(ctx context.Context, t pgstore.TraceRecord) error
	ListTraces(ctx context.Context, limit int) ([]pgstore.TraceRecord, error)
	GetTrace(ctx context.Context, id string) (*pgstore.TraceRecord, error)
	GetTracesSummary(ctx context.Context) (*pgstore.TracesSummary, error)
}

// TraceResponse is the API representation of one query trace row.
type TraceResponse struct {
	ID                       string    `json:"id"`
	TraceID                  string    `json:"trace_id"`
	Question                 string    `json:"question"`
	Intent                   string    `json:"intent"`
	Namespace                string    `json:"namespace"`
	Tier                     string    `json:"tier"`
	Status                   string    `json:"status"`
	Confidence               float64   `json:"confidence"`
	Sources                  []string  `json:"sources"`
	ToolCalls                []string  `json:"tool_calls"`
	LatencyMs                int64     `json:"latency_ms"`
	StartedAt                time.Time `json:"started_at"`
	CreatedAt                time.Time `json:"created_at"`
	InputTokens              int64     `json:"input_tokens"`
	OutputTokens             int64     `json:"output_tokens"`
	CacheCreationInputTokens int64     `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     int64     `json:"cache_read_input_tokens"`
	EstimatedCostUSD         float64   `json:"estimated_cost_usd"`
	PromptName               string    `json:"prompt_name"`
	PromptVersion            *int      `json:"prompt_version"`
	SessionID                string    `json:"session_id"`
	IsEval                   bool      `json:"is_eval"`
}

// MetricsSummaryResponse is returned by GET /admin/metrics/summary.
type MetricsSummaryResponse struct {
	TotalQueries      int64            `json:"total_queries"`
	Last24hCount      int64            `json:"last_24h_count"`
	QueriesByTier     map[string]int64 `json:"queries_by_tier"`
	QueriesByStatus   map[string]int64 `json:"queries_by_status"`
	QueriesByIntent   map[string]int64 `json:"queries_by_intent"`
	AvgAgentLatencyMs float64          `json:"avg_agent_latency_ms"`
	P95AgentLatencyMs float64          `json:"p95_agent_latency_ms"`
	CollectedAt       time.Time        `json:"collected_at"`
}

func traceToResponse(t pgstore.TraceRecord) TraceResponse {
	src := t.Sources
	if src == nil {
		src = []string{}
	}
	tc := t.ToolCalls
	if tc == nil {
		tc = []string{}
	}
	return TraceResponse{
		ID: t.ID, TraceID: t.TraceID, Question: t.Question, Intent: t.Intent,
		Namespace: t.Namespace, Tier: t.Tier, Status: t.Status,
		Confidence: t.Confidence, Sources: src, ToolCalls: tc,
		LatencyMs: t.LatencyMs, StartedAt: t.StartedAt, CreatedAt: t.CreatedAt,
		InputTokens:              t.InputTokens,
		OutputTokens:             t.OutputTokens,
		CacheCreationInputTokens: t.CacheCreationInputTokens,
		CacheReadInputTokens:     t.CacheReadInputTokens,
		EstimatedCostUSD:         t.EstimatedCostUSD,
		PromptName:               t.PromptName,
		PromptVersion:            t.PromptVersion,
		SessionID:                t.SessionID,
		IsEval:                   t.IsEval,
	}
}

// ChatStore is the multi-turn conversation interface implemented by the Postgres client.
type ChatStore interface {
	CreateChatSession(ctx context.Context, s *pgstore.ChatSession) error
	GetChatSession(ctx context.Context, id string) (*pgstore.ChatSession, error)
	UpdateChatSession(ctx context.Context, s *pgstore.ChatSession) error
	ListChatSessions(ctx context.Context, limit int) ([]pgstore.ChatSession, error)
	DeleteChatSession(ctx context.Context, id string) error
}

// ChatSessionResponse is the API shape of a chat session.
type ChatSessionResponse struct {
	ID         string                `json:"id"`
	Title      string                `json:"title"`
	Namespace  string                `json:"namespace"`
	Service    string                `json:"service"`
	Messages   []pgstore.ChatMessage `json:"messages"`
	CreatedAt  time.Time             `json:"created_at"`
	LastActive time.Time             `json:"last_active"`
}

func chatSessionToResponse(s pgstore.ChatSession) ChatSessionResponse {
	return ChatSessionResponse{
		ID: s.ID, Title: s.Title, Namespace: s.Namespace, Service: s.Service,
		Messages: s.Messages, CreatedAt: s.CreatedAt, LastActive: s.LastActive,
	}
}

// PricingStore is the model-pricing CRUD interface implemented by the Postgres client.
type PricingStore interface {
	ListModelPricing(ctx context.Context) ([]pgstore.ModelPricing, error)
	UpsertModelPricing(ctx context.Context, p *pgstore.ModelPricing) error
}

// ServiceDependencyStore is the mined-service-graph interface implemented by
// the Postgres client — see k8fy/service_topology.py for how edges are mined.
// tenantID always comes from Handler.resolveTenantContext, never client
// input (ADR 0022 phase 2). clusterID does too, EXCEPT when
// resolveTenantContext resolved no clusterID of its own (no CollectorToken
// presented) — HandleServiceDependencyUpsert then honors an explicit
// cluster_id from the request body instead (ADR 0029's trusted-internal-
// caller override, for the Glue-based dependency miner).
type ServiceDependencyStore interface {
	UpsertServiceDependency(ctx context.Context, id, tenantID, clusterID, namespace, fromService, toService string) error
	ListServiceDependencies(ctx context.Context, tenantID, namespace string) ([]pgstore.ServiceDependency, error)

	// Scan coverage lives on this interface rather than its own because it is
	// the DENOMINATOR for the edges above — evidence_count is uninterpretable
	// without it (ROADMAP P27 phase 1). Splitting them would mean a 17th
	// parameter on NewHandler and two nil checks that are always equal.
	UpsertScanCoverage(ctx context.Context, tenantID, clusterID, namespace, service string, cycles, podsSeen, podsSampled, logsReadable int, logLines int64) error
	ListScanCoverage(ctx context.Context, tenantID, namespace string) ([]pgstore.ScanCoverage, error)
}

// ClusterServiceStore is the service->cluster registry interface (ROADMAP
// P16 / ADR 0023), populated by agentify-discovery's inventory push and
// consulted by the resolver the agent uses to auto-route live-fetch and
// DiagnoseSkill requests to the right fleet cluster.
type ClusterServiceStore interface {
	UpsertClusterServices(ctx context.Context, tenantID, clusterID string, byNamespace map[string][]pgstore.ServiceEntry) error
	ResolveServiceClusters(ctx context.Context, tenantID, namespace, service string) ([]string, error)
	ListClusterServices(ctx context.Context, tenantID string) (map[string][]string, error)
	// ListClusterServiceSelectors returns one specific cluster's known
	// services in one namespace as a service-name -> selector map (ADR
	// 0029) — consulted by the Glue-based dependency miner, which has no
	// live cluster access of its own.
	ListClusterServiceSelectors(ctx context.Context, tenantID, clusterID, namespace string) (map[string]map[string]string, error)

	// Service profile read for the architecture view (ROADMAP P22).
	ListServiceProfiles(ctx context.Context, tenantID, namespace string) ([]pgstore.ServiceProfile, error)
}

// NamespaceEntry is one discovered namespace, returned by the namespace-sync
// endpoints (HandleSyncNamespaces, HandleTrackedEntities's live-seed
// fallback). Was populated by the retired k8fy adapter's live
// DiscoverNamespaces() call (ADR 0027); now built from
// ClusterServiceStore.ListClusterServices — same JSON shape, so the
// frontend's SyncResult type (src/frontend/src/api.ts) needed no change.
type NamespaceEntry struct {
	Namespace    string   `json:"namespace"`
	Services     []string `json:"services"`
	ServiceCount int      `json:"service_count"`
}

// ClusterIngressStore is the entry-point-mapping registry interface (ROADMAP
// P18 use case #3), populated by agentify-discovery's Ingress/Gateway+
// HTTPRoute/OpenShift Route scan. Store-only in this pass — no agent tool
// consumes ListClusterIngress yet; it exists for admin/future use, same
// deliberate scope boundary as the plan that added this interface.
type ClusterIngressStore interface {
	UpsertClusterIngress(ctx context.Context, tenantID, clusterID string, entries []pgstore.IngressEndpoint) error
	ListClusterIngress(ctx context.Context, tenantID, namespace string) ([]pgstore.IngressEndpoint, error)
}

// ClusterHealthStore is the fleet-wide health/version snapshot interface
// (ROADMAP P18 use case #5), populated by agentify-discovery's per-cycle
// pod-readiness + K8s-version report. Store-only in this pass — no agent
// tool or frontend fleet dashboard consumes ListClusterHealthSnapshots yet,
// same deliberate scope boundary as ClusterIngressStore.
type ClusterHealthStore interface {
	UpsertClusterHealthSnapshot(ctx context.Context, tenantID, clusterID, k8sVersion string, podsTotal, podsReady int) error
	ListClusterHealthSnapshots(ctx context.Context, tenantID string) ([]pgstore.ClusterHealthSnapshot, error)
}

// IntegrationStore is the integration CRUD interface implemented by the Postgres
// client. Using an interface keeps the handler decoupled from the storage package
// and makes the nil-safe "not configured" path cheap.
type IntegrationStore interface {
	ListIntegrations(ctx context.Context) ([]pgstore.Integration, error)
	GetIntegration(ctx context.Context, id string) (*pgstore.Integration, error)
	GetIntegrationByCollectorToken(ctx context.Context, token string) (*pgstore.Integration, error)
	CreateIntegration(ctx context.Context, in *pgstore.Integration) error
	UpdateIntegration(ctx context.Context, in *pgstore.Integration) error
	UpdateIntegrationNamespaces(ctx context.Context, id string, namespaces []string) error
	DeleteIntegration(ctx context.Context, id string) error
}

// IntegrationResponse is what the API returns — identical to pgstore.Integration
// but with HasToken/HasCollectorToken instead of the raw credentials so
// neither is ever leaked.
type IntegrationResponse struct {
	ID                string    `json:"id"`
	Name              string    `json:"name"`
	Namespaces        []string  `json:"namespaces"`
	Status            string    `json:"status"`
	HasToken          bool      `json:"has_token"`
	HasCollectorToken bool      `json:"has_collector_token"`
	CreatedAt         time.Time `json:"created_at"`
	UpdatedAt         time.Time `json:"updated_at"`
}

// integrationToResponse converts a storage integration to an API response,
// replacing the plaintext credentials with booleans.
func integrationToResponse(in pgstore.Integration) IntegrationResponse {
	return IntegrationResponse{
		ID:                in.ID,
		Name:              in.Name,
		Namespaces:        in.Namespaces,
		Status:            in.Status,
		HasToken:          in.Token != "" || in.TokenSecretARN != "",
		HasCollectorToken: in.CollectorToken != "",
		CreatedAt:         in.CreatedAt,
		UpdatedAt:         in.UpdatedAt,
	}
}

// QueryRequest represents a user query.
type QueryRequest struct {
	Question string                 `json:"question"`
	Context  map[string]interface{} `json:"context"`
}

// EvalQueryRequest is the body of POST /admin/eval/query (ADR 0030): a normal
// query plus a pin naming which prompt version should answer it.
//
// Deliberately a separate type from QueryRequest so the public, unauthenticated
// /api/query cannot carry a pin — "make the agent answer with arbitrary prompt
// text" is a behaviour-substitution lever, and ADR 0020 rule 5 keeps that class
// of capability off unauthenticated surfaces.
//
// PromptLabel and PromptVersion are mutually exclusive; version wins if both are
// set. Empty/zero means "resolve normally", which makes this endpoint a
// bearer-authenticated mirror of /api/query when no pin is given.
type EvalQueryRequest struct {
	Question string                 `json:"question"`
	Context  map[string]interface{} `json:"context"`
	// PromptName scopes the pin to ONE prompt (e.g. "k8fy/diagnose"). Required
	// when pinning: the dataset spans several intents and therefore several
	// prompts, so an unscoped pin would resolve every skill's prompt at the
	// candidate label. Those that have no such version fall back to their local
	// string — meaning the run would score a mixture of one candidate and
	// several fallbacks, not the production baseline. Empty name + a pin is
	// rejected.
	PromptName    string `json:"prompt_name,omitempty"`
	PromptLabel   string `json:"prompt_label,omitempty"`
	PromptVersion int    `json:"prompt_version,omitempty"`
}

// ToolCallInfo describes one tool call made by Claude during reasoning.
type ToolCallInfo struct {
	Name      string                 `json:"name"`
	Arguments map[string]interface{} `json:"arguments,omitempty"`
}

// QueryResponse represents the answer to a query.
type QueryResponse struct {
	Answer     string                 `json:"answer"`
	Status     string                 `json:"status"`
	Confidence float64                `json:"confidence"`
	Sources    []string               `json:"sources"`
	TraceID    string                 `json:"trace_id,omitempty"`
	ToolCalls  []ToolCallInfo         `json:"tool_calls,omitempty"`
	Details    map[string]interface{} `json:"details,omitempty"`
	// Eval-visible metadata: intent classification and tier used to answer.
	// Included so eval harness can score routing decisions without a DB lookup.
	Intent string `json:"intent,omitempty"`
	Tier   string `json:"tier,omitempty"`
}

// RemediationStore is the remediation-proposal CRUD interface implemented by
// the Postgres client (ADR 0020 / spec 011 Use Cases 1+2).
type RemediationStore interface {
	CreateRemediationProposal(ctx context.Context, p *pgstore.RemediationProposal) error
	GetRemediationProposal(ctx context.Context, id string) (*pgstore.RemediationProposal, error)
	ListRemediationProposals(ctx context.Context, status string, limit int) ([]pgstore.RemediationProposal, error)
	DecideRemediationProposal(ctx context.Context, id, status, decidedBy string) (bool, error)
	CompleteRemediationProposal(ctx context.Context, id, status string, result map[string]interface{}, errMsg string) error
	ProposalExistsForEvent(ctx context.Context, sourceEventID string) (bool, error)
}

// RemediationProposalResponse is the API shape of a remediation proposal.
type RemediationProposalResponse struct {
	ID             string                 `json:"id"`
	TraceID        string                 `json:"trace_id,omitempty"`
	UseCase        string                 `json:"use_case"`
	Namespace      string                 `json:"namespace"`
	Service        string                 `json:"service"`
	ProposedAction string                 `json:"proposed_action"`
	ActionParams   map[string]interface{} `json:"action_params"`
	Analysis       map[string]interface{} `json:"analysis"`
	Status         string                 `json:"status"`
	CreatedAt      time.Time              `json:"created_at"`
	ExpiresAt      time.Time              `json:"expires_at"`
	DecidedAt      *time.Time             `json:"decided_at,omitempty"`
	DecidedBy      string                 `json:"decided_by,omitempty"`
	ExecutedAt     *time.Time             `json:"executed_at,omitempty"`
	Result         map[string]interface{} `json:"result,omitempty"`
	Error          string                 `json:"error,omitempty"`
}

func remediationToResponse(p pgstore.RemediationProposal) RemediationProposalResponse {
	params := p.ActionParams
	if params == nil {
		params = map[string]interface{}{}
	}
	analysis := p.Analysis
	if analysis == nil {
		analysis = map[string]interface{}{}
	}
	return RemediationProposalResponse{
		ID: p.ID, TraceID: p.TraceID, UseCase: p.UseCase,
		Namespace: p.Namespace, Service: p.Service, ProposedAction: p.ProposedAction,
		ActionParams: params, Analysis: analysis, Status: p.Status,
		CreatedAt: p.CreatedAt, ExpiresAt: p.ExpiresAt,
		DecidedAt: p.DecidedAt, DecidedBy: p.DecidedBy, ExecutedAt: p.ExecutedAt,
		Result: p.Result, Error: p.Error,
	}
}

// CertRenewRequest is the payload for POST /admin/certs/renew.
type CertRenewRequest struct {
	Namespace string `json:"namespace"` // K8s namespace (e.g. "payments")
	Service   string `json:"service"`   // service name (e.g. "payment")
}

// CertRenewResponse is returned by POST /admin/certs/renew.
type CertRenewResponse struct {
	Status           string `json:"status"` // "ok" | "error"
	Message          string `json:"message"`
	Serial           string `json:"serial,omitempty"`
	TTL              string `json:"ttl,omitempty"`
	K8sSecretUpdated bool   `json:"k8s_secret_updated,omitempty"`
	K8sSecret        string `json:"k8s_secret,omitempty"`
	// New cert metadata so the UI can update immediately without waiting for
	// the next adapter scrape cycle (default 5 minutes).
	ExpiresAt       string   `json:"expires_at,omitempty"`
	DaysUntilExpiry int      `json:"days_until_expiry,omitempty"`
	DnsNames        []string `json:"dns_names,omitempty"`
	TraceID         string   `json:"trace_id,omitempty"`
}
