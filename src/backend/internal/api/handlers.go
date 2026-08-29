package api

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/chan/agentify/backend/internal/governance"
	"github.com/chan/agentify/backend/internal/ingestion"
	"github.com/chan/agentify/backend/internal/models"
	"github.com/chan/agentify/backend/internal/orchestrator"
	"github.com/chan/agentify/backend/internal/secrets"
	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
	"github.com/chan/agentify/backend/internal/telemetry"
)

// Handler holds dependencies for HTTP handlers.
type Handler struct {
	orch                     *orchestrator.Router
	ingester                 *ingestion.Ingester
	queryExec                *orchestrator.QueryExecutor
	agentClient              *AgentClient
	redactor                 *governance.Redactor
	integrationStore         IntegrationStore // nil when postgres is not provisioned
	traceStore               TraceStore       // nil when postgres is not provisioned
	pricingStore             PricingStore     // nil when postgres is not provisioned
	chatStore                ChatStore        // nil when postgres is not provisioned
	remediationStore         RemediationStore // nil when postgres is not provisioned
	remediationConfig        RemediationConfig
	serviceDepsStore         ServiceDependencyStore // nil when postgres is not provisioned
	collectorHub             *CollectorHub          // fleet collectors' persistent connections (ADR 0022 Decision #7 / ROADMAP P18 use case #9)
	clusterServiceStore      ClusterServiceStore    // service->cluster registry (ROADMAP P16 / ADR 0023); nil when postgres is not provisioned
	clusterIngressStore      ClusterIngressStore    // entry-point-mapping registry (ROADMAP P18 use case #3); nil when postgres is not provisioned
	clusterHealthStore       ClusterHealthStore     // fleet-wide health/version snapshot (ROADMAP P18 use case #5); nil when postgres is not provisioned
	secretsManager           secrets.Manager        // Integration.Token storage (ADR 0025); nil = plaintext mode (today's default)
	integrationSecretsPrefix string                 // secret-name prefix; only meaningful when secretsManager != nil
	logger                   *slog.Logger
}

// integrationSecretName builds the deterministic Secrets Manager secret name
// for one Integration row's outbound token — stable across edits so an
// update reuses (rather than accumulates) the same secret.
func (h *Handler) integrationSecretName(id string) string {
	return h.integrationSecretsPrefix + "/" + id
}

// NewHandler creates a new handler.
func NewHandler(orch *orchestrator.Router, agentServiceURL string, redactor *governance.Redactor, integrations IntegrationStore, traces TraceStore, pricing PricingStore, chat ChatStore, remediation RemediationStore, remediationCfg RemediationConfig, serviceDeps ServiceDependencyStore, clusterServices ClusterServiceStore, clusterIngress ClusterIngressStore, clusterHealth ClusterHealthStore, secretsManager secrets.Manager, integrationSecretsPrefix string, logger *slog.Logger) *Handler {
	ingester := ingestion.NewIngester(orch.GetPodRegistry(), orch.GetBackendFactory(), logger)
	queryExec := orchestrator.NewQueryExecutor(orch.GetPodRegistry(), orch.GetBackendFactory(), logger)

	return &Handler{
		orch:                     orch,
		ingester:                 ingester,
		queryExec:                queryExec,
		agentClient:              NewAgentClient(agentServiceURL),
		redactor:                 redactor,
		integrationStore:         integrations,
		traceStore:               traces,
		pricingStore:             pricing,
		chatStore:                chat,
		remediationStore:         remediation,
		remediationConfig:        remediationCfg,
		serviceDepsStore:         serviceDeps,
		collectorHub:             NewCollectorHub(),
		clusterServiceStore:      clusterServices,
		clusterIngressStore:      clusterIngress,
		clusterHealthStore:       clusterHealth,
		secretsManager:           secretsManager,
		integrationSecretsPrefix: integrationSecretsPrefix,
		logger:                   logger,
	}
}

// HandleHealth responds with service health status.
// Returns 503 when the Postgres backend is configured but not yet reachable so
// that the K8s readiness probe holds traffic until the DB connection is healthy.
// This prevents the "empty namespace autocomplete + empty Query History" symptom
// caused by the pod receiving traffic before the RDS connection succeeds after a
// scale-up resume cycle.
func (h *Handler) HandleHealth(w http.ResponseWriter, r *http.Request) {
	// If a trace store is wired, verify the DB connection is live.
	if checker, ok := h.traceStore.(interface {
		HealthCheck(ctx context.Context) error
	}); ok {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := checker.HealthCheck(ctx); err != nil {
			h.logger.Warn("health check: postgres not reachable", "error", err)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusServiceUnavailable)
			json.NewEncoder(w).Encode(map[string]string{
				"status": "degraded",
				"reason": "postgres unavailable",
			})
			return
		}
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

// HandleIngestEvent accepts a canonical event and ingests it into the mesh.
//
// tenantID/clusterID (ADR 0024) come from resolveTenantContext, same
// CollectorToken credential the fleet-collector push endpoints already use
// (src/adapters/discovery/normalize.py's push_event sends it as a Bearer
// token with every ingest POST — COLLECTOR_TOKEN, the single unified
// collector credential since ADR 0027 merged the old k8fy-adapter's separate
// BACKEND_AUTH_TOKEN into it). Unlike the collector endpoints, an ABSENT
// credential is not rejected: it defaults to (DefaultTenantID, "") so every
// single-cluster agentify-discovery deployment that hasn't been given a
// CollectorToken keeps ingesting exactly as before. An unrecognized
// (invalid) token is still rejected — same as every other
// resolveTenantContext consumer.
func (h *Handler) HandleIngestEvent(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	tenantID, clusterID, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	var event models.Event
	if err := json.NewDecoder(r.Body).Decode(&event); err != nil {
		h.logger.Error("failed to decode event", "error", err)
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}

	// Ingest the event
	result, err := h.ingester.Ingest(r.Context(), &event, tenantID, clusterID)
	if err != nil {
		h.logger.Error("ingestion failed", "event_id", event.ID, "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusAccepted)
	json.NewEncoder(w).Encode(result)
}

// HandleQuery processes a user query and returns an answer.
func (h *Handler) HandleQuery(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	start := time.Now()
	traceID := uuid.New().String() // provenance correlation id (spec 004)

	var req QueryRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		h.logger.Error("failed to decode query request", "error", err)
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}

	// Extract namespace from context (default to "prod")
	namespace := "prod"
	if ns, ok := req.Context["namespace"]; ok {
		namespace = ns.(string)
	}

	// Extract intent or infer from question
	// For MVP: use simple heuristics to determine intent
	intent := inferIntent(req.Question)

	// Route to pods and fetch data. Not cluster-scoped: this is the initial
	// /api/query routing (ADR 0024 scopes the agent's per-tool
	// /api/agent/fetch path, not this one — see HandleAgentFetch).
	pods, err := h.queryExec.RouteToPods(r.Context(), intent, namespace, "")
	if err != nil {
		h.logger.Error("failed to route query", "error", err)
		telemetry.QueriesTotal.WithLabelValues(intent, "none", "error").Inc()
		h.logTrace(traceID, req.Question, intent, namespace, "none", "error", nil, 0, nil, start, nil)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}

	if len(pods) == 0 {
		resp := QueryResponse{
			Answer:     "No data available for this query",
			Status:     "no_data",
			Confidence: 0.0,
			Sources:    []string{},
			TraceID:    traceID,
		}
		telemetry.QueriesTotal.WithLabelValues(intent, "no_data", "no_data").Inc()
		h.logTrace(traceID, req.Question, intent, namespace, "no_data", "no_data", nil, 0, nil, start, nil)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(resp)
		return
	}

	// Build the backend query. By default it carries no lookup key, so KV pods
	// return every entity in the shard for the agent to reason over. A caller may
	// target a single entity by putting its exact stored key under "entity"/"key"
	// in the request context (we don't guess a key from fuzzy service names).
	podQuery := buildPodQuery(req.Context)

	// Fetch from pods
	podData := make(map[string]interface{})
	for _, pod := range pods {
		data, err := h.queryExec.FetchFromPod(r.Context(), pod, podQuery)
		if err != nil {
			h.logger.Warn("failed to fetch from pod", "pod_id", pod.ID, "error", err)
			continue
		}
		podData[pod.ID] = map[string]interface{}{
			"data": data,
			"type": pod.StoreType,
			"tags": pod.Tags,
		}
	}

	// Tier 1 — deterministic fast-path (ADR 0006): answer structured intents
	// (health/cert) directly from the data with no LLM call. Falls through to the
	// agent when the intent needs synthesis or there's no data to evaluate.
	if resp, handled := tryDeterministic(intent, podData, req.Context); handled {
		resp.TraceID = traceID
		resp.Intent = intent
		resp.Tier = "tier1"
		h.logger.Info("answered via deterministic fast-path", "intent", intent, "pods", len(pods))
		telemetry.QueriesTotal.WithLabelValues(intent, "tier1", "ok").Inc()
		telemetry.QueryDuration.WithLabelValues("tier1").Observe(time.Since(start).Seconds())
		h.logTrace(traceID, req.Question, intent, namespace, "tier1", resp.Status, resp.Sources, resp.Confidence, nil, start, nil)
		writeJSON(w, http.StatusOK, resp)
		return
	}

	// Tier 2 — agentic synthesis path. Redact at the egress boundary (ADR 0007):
	// the agent (and the model it calls) only ever sees allowlisted data.
	// trimAgentPayload then reduces token footprint: dedup by entity_key,
	// drop completed-rollout noise, hard-cap at maxEventsPerPod per pod.
	agentData := trimAgentPayload(h.redactor.RedactPodData(podData))
	agentStart := time.Now()
	agentResp, err := h.agentClient.Reason(req.Question, intent, agentData, req.Context, traceID)
	telemetry.AgentCallDuration.Observe(time.Since(agentStart).Seconds())
	if err != nil {
		h.logger.Warn("agent service error", "error", err)
		telemetry.AgentCallsTotal.WithLabelValues("error").Inc()
		telemetry.QueriesTotal.WithLabelValues(intent, "tier2", "partial").Inc()
		telemetry.QueryDuration.WithLabelValues("tier2").Observe(time.Since(start).Seconds())
		// Fallback: return raw data
		resp := QueryResponse{
			Answer:     formatPodData(podData),
			Status:     "partial",
			Confidence: 0.5,
			Sources:    extractPodIDs(pods),
			TraceID:    traceID,
		}
		h.logTrace(traceID, req.Question, intent, namespace, "tier2", "partial", resp.Sources, 0.5, nil, start, nil)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(resp)
		return
	}

	telemetry.AgentCallsTotal.WithLabelValues("ok").Inc()
	telemetry.QueriesTotal.WithLabelValues(intent, "tier2", "ok").Inc()
	telemetry.QueryDuration.WithLabelValues("tier2").Observe(time.Since(start).Seconds())

	// Return agent response. Pass the agent's status through so the frontend
	// can render error/degraded cards correctly. Fall back to "ok" only when
	// the agent omits the field (older image compatibility).
	agentStatus := agentResp.Status
	if agentStatus == "" {
		agentStatus = "ok"
	}

	var toolCalls []ToolCallInfo
	for _, tc := range agentResp.ToolCalls {
		toolCalls = append(toolCalls, ToolCallInfo{Name: tc.Name, Arguments: tc.Arguments})
	}

	resp := QueryResponse{
		Answer:     agentResp.Answer,
		Status:     agentStatus,
		Confidence: agentResp.Confidence,
		Sources:    agentResp.Sources,
		TraceID:    traceID,
		ToolCalls:  toolCalls,
		Details:    agentResp.Details,
		Intent:     intent,
		Tier:       "tier2",
	}
	h.logTrace(traceID, req.Question, intent, namespace, "tier2", agentStatus, agentResp.Sources, agentResp.Confidence, toolCallNames(agentResp.ToolCalls), start, agentResp)

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(resp)
}

// HandleAgentFetch lets the Python agent fetch pod data on demand during its
// tool-calling loop. The agent's tools (get_service_health, query_pod,
// get_certificates, get_pod_events) map to a pod query here. This endpoint only
// reads data — it never re-invokes the agent, so there is no recursion.
func (h *Handler) HandleAgentFetch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Tool string                 `json:"tool"`
		Args map[string]interface{} `json:"args"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		h.logger.Error("failed to decode agent fetch", "error", err)
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}

	intent, namespace, key := mapToolToQuery(req.Tool, req.Args)

	// cluster_id (ADR 0024) is an optional explicit arg — same shape as
	// HandleLiveFetch's, not a bearer credential (the agent presents none).
	// Resolved by the agent via resolve_service_clusters before it calls
	// here; empty (every call site not yet passing one) routes exactly as
	// before this ADR.
	clusterID := stringArg(req.Args, "cluster_id")

	pods, err := h.queryExec.RouteToPods(r.Context(), intent, namespace, clusterID)
	if err != nil {
		h.logger.Warn("agent fetch routing failed", "tool", req.Tool, "error", err)
		// Degrade to an empty result rather than failing the agent's loop.
		writeJSON(w, http.StatusOK, map[string]interface{}{"tool": req.Tool, "data": map[string]interface{}{}})
		return
	}

	query := map[string]interface{}{}
	if key != "" {
		query["key"] = key
	}
	// Forward time-window / entity / order filters so history tools (spec 006) can
	// read a windowed series; the events store ignores any it doesn't recognize.
	for _, p := range []string{"since", "until", "order", "type", "entity", "pod_id", "service", "deployment"} {
		if v := stringArg(req.Args, p); v != "" {
			query[p] = v
		}
	}
	// get_service_health passes "service_name" (not "service") — map it explicitly
	// so CurrentState.Query can do the service-prefix scan for Deployment-only
	// workloads whose pods are stored as "{service}-{rs-hash}-{pod-hash}".
	if req.Tool == "get_service_health" {
		if svcName := stringArg(req.Args, "service_name"); svcName != "" {
			query["service"] = svcName
		}
	}
	if v, ok := req.Args["limit"]; ok {
		query["limit"] = v
	}

	data := make(map[string]interface{})
	for _, pod := range pods {
		rows, err := h.queryExec.FetchFromPod(r.Context(), pod, query)
		if err != nil {
			h.logger.Warn("agent fetch from pod failed", "pod_id", pod.ID, "error", err)
			continue
		}
		data[pod.ID] = rows
	}

	// Redact at the egress boundary (ADR 0007) before the agent (and its model) sees it.
	writeJSON(w, http.StatusOK, map[string]interface{}{"tool": req.Tool, "data": h.redactor.RedactFetch(data)})
}

// mapToolToQuery translates an agent tool name + args into a (intent, namespace,
// entity-key) query triple aligned with the K8fy pod taxonomy (ADR 0005).
func mapToolToQuery(tool string, args map[string]interface{}) (intent, namespace, key string) {
	namespace = stringArg(args, "namespace")
	if namespace == "" {
		namespace = "prod"
	}

	switch tool {
	case "get_certificates":
		return "cert_check", namespace, ""
	case "get_service_health":
		// Don't use an exact key lookup — for Deployment-only workloads (queue
		// workers, consumers) there is no service_* row keyed by the plain service
		// name; only pod_* rows keyed by the full pod name exist. An exact match
		// returns nothing. Return "" so HandleAgentFetch will use the service-prefix
		// LIKE scan path in CurrentState.Query instead.
		return "health_check", namespace, ""
	case "query_pod", "get_pod_events":
		return "health_check", namespace, stringArg(args, "pod_id")
	case "get_metrics_history":
		// Time-series of restart samples; the entity is forwarded as a filter
		// (not a point-lookup key) by HandleAgentFetch (spec 006).
		return "metrics_history", namespace, ""
	case "get_change_history":
		// Deploy/change events; entity (deployment/service) forwarded as a filter (spec 007).
		return "change_history", namespace, ""
	default:
		return "general_query", namespace, ""
	}
}

// stringArg safely extracts a string argument from a decoded JSON map.
func stringArg(args map[string]interface{}, name string) string {
	if v, ok := args[name].(string); ok {
		return v
	}
	return ""
}

// writeJSON writes a JSON response with the given status code.
func writeJSON(w http.ResponseWriter, status int, body interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(body)
}

// HandleIntegrationList returns all configured integrations (tokens redacted).
func (h *Handler) HandleIntegrationList(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if h.integrationStore == nil {
		json.NewEncoder(w).Encode([]IntegrationResponse{})
		return
	}
	rows, err := h.integrationStore.ListIntegrations(r.Context())
	if err != nil {
		h.logger.Error("list integrations failed", "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	resp := make([]IntegrationResponse, len(rows))
	for i, row := range rows {
		resp[i] = integrationToResponse(row)
	}
	json.NewEncoder(w).Encode(resp)
}

// HandleIntegrationGet returns one integration by ID.
func (h *Handler) HandleIntegrationGet(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		http.Error(w, "missing id", http.StatusBadRequest)
		return
	}
	if h.integrationStore == nil {
		http.Error(w, "integration store not configured", http.StatusServiceUnavailable)
		return
	}
	row, err := h.integrationStore.GetIntegration(r.Context(), id)
	if err != nil {
		h.logger.Error("get integration failed", "id", id, "error", err)
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(integrationToResponse(*row))
}

// integrationCreateRequest is the body accepted by POST /admin/integrations.
type integrationCreateRequest struct {
	Name           string   `json:"name"`
	Namespaces     []string `json:"namespaces"`
	Token          string   `json:"token"`
	CollectorToken string   `json:"collector_token"` // ADR 0022 — a collector's inbound push credential
}

// HandleIntegrationCreate adds a new integration.
func (h *Handler) HandleIntegrationCreate(w http.ResponseWriter, r *http.Request) {
	if h.integrationStore == nil {
		http.Error(w, "integration store not configured", http.StatusServiceUnavailable)
		return
	}
	var req integrationCreateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	if req.Name == "" {
		http.Error(w, "name is required", http.StatusBadRequest)
		return
	}
	if req.Namespaces == nil {
		req.Namespaces = []string{}
	}

	id := uuid.New().String()
	row := &pgstore.Integration{
		ID:             id,
		Name:           req.Name,
		Namespaces:     req.Namespaces,
		Status:         "inactive",
		Token:          req.Token,
		CollectorToken: req.CollectorToken,
	}
	if h.secretsManager != nil && req.Token != "" {
		arn, err := h.secretsManager.Store(r.Context(), h.integrationSecretName(id), req.Token)
		if err != nil {
			h.logger.Error("store integration token in secrets manager failed", "id", id, "error", err)
			http.Error(w, "internal server error", http.StatusInternalServerError)
			return
		}
		row.Token = ""
		row.TokenSecretARN = arn
	}
	if err := h.integrationStore.CreateIntegration(r.Context(), row); err != nil {
		h.logger.Error("create integration failed", "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	created, err := h.integrationStore.GetIntegration(r.Context(), id)
	if err != nil {
		h.logger.Error("fetch created integration failed", "id", id, "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(integrationToResponse(*created))
}

// integrationUpdateRequest is the body accepted by PUT /admin/integrations/{id}.
type integrationUpdateRequest struct {
	Name           string   `json:"name"`
	Namespaces     []string `json:"namespaces"`
	Status         string   `json:"status"`
	Token          string   `json:"token"`           // empty = keep existing token
	CollectorToken string   `json:"collector_token"` // empty = keep existing collector token
}

// HandleIntegrationUpdate replaces mutable fields for an existing integration.
func (h *Handler) HandleIntegrationUpdate(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		http.Error(w, "missing id", http.StatusBadRequest)
		return
	}
	if h.integrationStore == nil {
		http.Error(w, "integration store not configured", http.StatusServiceUnavailable)
		return
	}
	var req integrationUpdateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	if req.Name == "" {
		http.Error(w, "name is required", http.StatusBadRequest)
		return
	}
	if req.Namespaces == nil {
		req.Namespaces = []string{}
	}
	status := req.Status
	if status == "" {
		status = "inactive"
	}

	row := &pgstore.Integration{
		ID:             id,
		Name:           req.Name,
		Namespaces:     req.Namespaces,
		Status:         status,
		Token:          req.Token,
		CollectorToken: req.CollectorToken,
	}
	if h.secretsManager != nil && req.Token != "" {
		arn, err := h.secretsManager.Store(r.Context(), h.integrationSecretName(id), req.Token)
		if err != nil {
			h.logger.Error("store integration token in secrets manager failed", "id", id, "error", err)
			http.Error(w, "internal server error", http.StatusInternalServerError)
			return
		}
		row.Token = ""
		row.TokenSecretARN = arn
	}
	if err := h.integrationStore.UpdateIntegration(r.Context(), row); err != nil {
		h.logger.Error("update integration failed", "id", id, "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	updated, err := h.integrationStore.GetIntegration(r.Context(), id)
	if err != nil {
		h.logger.Error("fetch updated integration failed", "id", id, "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(integrationToResponse(*updated))
}

// HandleIntegrationDelete removes an integration by ID.
func (h *Handler) HandleIntegrationDelete(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		http.Error(w, "missing id", http.StatusBadRequest)
		return
	}
	if h.integrationStore == nil {
		http.Error(w, "integration store not configured", http.StatusServiceUnavailable)
		return
	}
	// Look up the row first (best-effort) so a live Secrets Manager entry can
	// be cleaned up after the DB delete succeeds — never blocks the delete
	// itself on a Secrets Manager error.
	var secretARN string
	if h.secretsManager != nil {
		if existing, err := h.integrationStore.GetIntegration(r.Context(), id); err == nil {
			secretARN = existing.TokenSecretARN
		}
	}
	if err := h.integrationStore.DeleteIntegration(r.Context(), id); err != nil {
		h.logger.Error("delete integration failed", "id", id, "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	if secretARN != "" {
		if err := h.secretsManager.Delete(r.Context(), secretARN); err != nil {
			h.logger.Warn("delete integration secret failed (row already removed)", "id", id, "arn", secretARN, "error", err)
		}
	}
	w.WriteHeader(http.StatusNoContent)
}

// HandlePodRegistryList returns all pods in the registry.
func (h *Handler) HandlePodRegistryList(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	pods, err := h.orch.GetPodRegistry().ListActivePods(r.Context())
	if err != nil {
		h.logger.Error("failed to list pods", "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(pods)
}

// inferIntent determines the query intent from natural language.
func inferIntent(question string) string {
	lower := strings.ToLower(question)
	// Diagnostic phrasing must win BEFORE health/cert so "why is payment unhealthy?"
	// fans out to a multi-signal Tier-2 diagnosis (spec 005) rather than dropping
	// into the single-signal Tier-1 health fast-path (resolves spec 003 limitation #3).
	for _, kw := range []string{"why", "what's wrong", "whats wrong", "what is wrong", "root cause", "root-cause", "diagnose", "diagnos", "investigate", "going on", "going wrong", "broken"} {
		if strings.Contains(lower, kw) {
			return "diagnose"
		}
	}
	if strings.Contains(lower, "health") || strings.Contains(lower, "healthy") {
		return "health_check"
	}
	// Vault cert intent: questions about Vault-managed certs or cert rotation via Vault.
	for _, kw := range []string{"vault", "rotate cert", "rotate tls", "pki", "cert rotat", "renew cert"} {
		if strings.Contains(lower, kw) {
			return "vault_cert"
		}
	}
	if strings.Contains(lower, "certificate") || strings.Contains(lower, "cert") || strings.Contains(lower, "expir") {
		return "cert_check"
	}
	if strings.Contains(lower, "metric") || strings.Contains(lower, "cpu") || strings.Contains(lower, "memory") {
		return "metrics_query"
	}
	// Change / deploy history: captures "what changed", "recent deploy", "rollout history"
	for _, kw := range []string{"what changed", "what deploy", "recent deploy", "deploy history", "rollout history", "change history", "recently deployed", "recently changed"} {
		if strings.Contains(lower, kw) {
			return "change_history"
		}
	}
	// Restart / metrics history: captures "restart trend", "restart history", "restart count"
	for _, kw := range []string{"restart trend", "restart history", "restart count", "restart metric", "restart spike"} {
		if strings.Contains(lower, kw) {
			return "metrics_history"
		}
	}
	return "general_query"
}

// buildPodQuery derives the backend query map from the request context.
// It forwards an explicit entity lookup key (from "key" or "entity") so KV pods
// can do a point lookup; absent that, the empty key makes them scan the whole
// shard. It deliberately does not derive a key from "service"/"namespace", which
// are filters the agent applies, not exact storage keys.
func buildPodQuery(reqContext map[string]interface{}) map[string]interface{} {
	query := make(map[string]interface{})
	for k, v := range reqContext {
		query[k] = v
	}
	if _, ok := query["key"]; !ok {
		if entity, ok := query["entity"].(string); ok && entity != "" {
			query["key"] = entity
		}
	}
	return query
}

// logTrace emits the per-query provenance record (spec 004): structured log +
// async Postgres insert so the HTTP response is never blocked.
// agentResp may be nil for Tier-1 / error paths (no LLM call was made).
func (h *Handler) logTrace(traceID, question, intent, namespace, tier, status string, sources []string, confidence float64, toolCalls []string, start time.Time, agentResp *AgentResponse) {
	latencyMs := time.Since(start).Milliseconds()
	var inTok, outTok, cacheWriteTok, cacheReadTok int64
	var cost float64
	var promptName string
	var promptVersion *int
	if agentResp != nil {
		inTok = agentResp.InputTokens
		outTok = agentResp.OutputTokens
		cacheWriteTok = agentResp.CacheCreationInputTokens
		cacheReadTok = agentResp.CacheReadInputTokens
		cost = agentResp.EstimatedCostUSD
		promptName = agentResp.PromptName
		promptVersion = agentResp.PromptVersion
	}
	h.logger.Info("query.trace",
		"trace_id", traceID,
		"question", question,
		"intent", intent,
		"namespace", namespace,
		"tier", tier,
		"status", status,
		"sources", sources,
		"confidence", confidence,
		"tool_calls", toolCalls,
		"latency_ms", latencyMs,
		"input_tokens", inTok,
		"output_tokens", outTok,
		"cache_creation_input_tokens", cacheWriteTok,
		"cache_read_input_tokens", cacheReadTok,
		"estimated_cost_usd", cost,
		"prompt_name", promptName,
		"prompt_version", promptVersion,
	)
	if h.traceStore != nil {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			rowID := uuid.New().String()
			if err := h.traceStore.InsertTrace(ctx, pgstore.TraceRecord{
				ID:                       rowID,
				TraceID:                  traceID,
				Question:                 question,
				Intent:                   intent,
				Namespace:                namespace,
				Tier:                     tier,
				Status:                   status,
				Confidence:               confidence,
				Sources:                  sources,
				ToolCalls:                toolCalls,
				LatencyMs:                latencyMs,
				StartedAt:                start,
				InputTokens:              inTok,
				OutputTokens:             outTok,
				CacheCreationInputTokens: cacheWriteTok,
				CacheReadInputTokens:     cacheReadTok,
				EstimatedCostUSD:         cost,
				PromptName:               promptName,
				PromptVersion:            promptVersion,
			}); err != nil {
				h.logger.Warn("trace persist failed", "error", err)
				return
			}

			// P8 — Semantic memory: embed Tier-2 diagnose traces so DiagnoseSkill
			// can retrieve similar past incidents as few-shot context.
			if tier == "tier2" && intent == "diagnose" && agentResp != nil {
				go h.embedAndStoreIncident(rowID, traceID, namespace, intent, agentResp)
			}
		}()
	}
}

// embedAndStoreIncident calls the agent's /embed endpoint and stores the
// resulting vector in incident_embeddings. Never blocks the query response —
// called from a goroutine. Silently skips if the embed service is unavailable.
func (h *Handler) embedAndStoreIncident(rowID, traceID, namespace, intent string, agentResp *AgentResponse) {
	// Build a compact summary from the structured diagnosis fields.
	details := agentResp.Details
	headline, _ := details["headline"].(string)
	cause, _ := details["likely_cause"].(string)
	if headline == "" {
		headline = agentResp.Answer
	}
	summary := headline
	if cause != "" {
		summary += " | " + cause
	}
	if summary == "" {
		return
	}

	// Derive the service name from the sources list (e.g. "k8fy.live-state.payments" → "payments").
	service := ""
	for _, src := range agentResp.Sources {
		if strings.HasPrefix(src, "k8fy.live-state.") {
			service = strings.TrimPrefix(src, "k8fy.live-state.")
			break
		}
	}

	// Call the agent's /embed endpoint with a 10 s deadline.
	type embedResp struct {
		Embedding []float32 `json:"embedding"`
		Available bool      `json:"available"`
	}
	body, _ := json.Marshal(map[string]string{"text": summary})
	req, err := http.NewRequest("POST", h.agentClient.baseURL+"/embed",
		bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	embedCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	req = req.WithContext(embedCtx)

	resp, err := h.agentClient.client.Do(req)
	if err != nil {
		h.logger.Info("embed unavailable, storing summary only", "error", err)
	}
	var vec []float32
	if err == nil {
		defer resp.Body.Close()
		var er embedResp
		if json.NewDecoder(resp.Body).Decode(&er) == nil && er.Available {
			vec = er.Embedding
		}
	}

	// Persist the embedding (or just the summary when vec is nil).
	if relational, rerr := h.orch.GetBackendFactory().GetBackend("relational"); rerr == nil {
		type embStorer interface {
			InsertIncidentEmbedding(ctx context.Context, e pgstore.IncidentEmbedding) error
		}
		if es, ok := relational.(embStorer); ok {
			storeCtx, storeCancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer storeCancel()
			if serr := es.InsertIncidentEmbedding(storeCtx, pgstore.IncidentEmbedding{
				ID:        rowID,
				TraceID:   traceID,
				Namespace: namespace,
				Service:   service,
				Summary:   summary,
				Embedding: vec,
			}); serr != nil {
				h.logger.Warn("incident embedding store failed", "error", serr)
			}
		}
	}
}

// HandleTraceList returns recent query traces for the admin history view.
func (h *Handler) HandleTraceList(w http.ResponseWriter, r *http.Request) {
	if h.traceStore == nil {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode([]TraceResponse{})
		return
	}
	rows, err := h.traceStore.ListTraces(r.Context(), 200)
	if err != nil {
		h.logger.Error("list traces failed", "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	resp := make([]TraceResponse, len(rows))
	for i, row := range rows {
		resp[i] = traceToResponse(row)
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// HandleTraceGet returns a single trace by its primary-key ID.
func (h *Handler) HandleTraceGet(w http.ResponseWriter, r *http.Request) {
	if h.traceStore == nil {
		http.Error(w, "trace store not available", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	if id == "" {
		http.Error(w, "missing id", http.StatusBadRequest)
		return
	}
	rec, err := h.traceStore.GetTrace(r.Context(), id)
	if err != nil {
		h.logger.Warn("get trace failed", "id", id, "error", err)
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	writeJSON(w, http.StatusOK, traceToResponse(*rec))
}

// HandleMetricsSummary returns aggregated query statistics for the metrics dashboard.
func (h *Handler) HandleMetricsSummary(w http.ResponseWriter, r *http.Request) {
	if h.traceStore == nil {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(MetricsSummaryResponse{
			QueriesByTier:   map[string]int64{},
			QueriesByStatus: map[string]int64{},
			QueriesByIntent: map[string]int64{},
			CollectedAt:     time.Now(),
		})
		return
	}
	s, err := h.traceStore.GetTracesSummary(r.Context())
	if err != nil {
		h.logger.Error("metrics summary failed", "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(MetricsSummaryResponse{
		TotalQueries:      s.TotalQueries,
		Last24hCount:      s.Last24hCount,
		QueriesByTier:     s.QueriesByTier,
		QueriesByStatus:   s.QueriesByStatus,
		QueriesByIntent:   s.QueriesByIntent,
		AvgAgentLatencyMs: s.AvgAgentLatencyMs,
		P95AgentLatencyMs: s.P95AgentLatencyMs,
		CollectedAt:       time.Now(),
	})
}

// toolCallNames extracts the tool names the agent invoked, for the trace.
func toolCallNames(calls []AgentToolCall) []string {
	if len(calls) == 0 {
		return nil
	}
	names := make([]string, 0, len(calls))
	for _, c := range calls {
		names = append(names, c.Name)
	}
	return names
}

// formatPodData returns a brief human-readable summary of the fetched pod data.
// Called only when the agent service is unavailable; output is shown as the
// fallback answer so the user knows what data was fetched but not analysed.
func formatPodData(podData map[string]interface{}) string {
	var sb strings.Builder

	// Count total events across all pods to distinguish "no data found" from
	// "data found but Claude couldn't process it".
	totalEvents := 0
	for _, raw := range podData {
		m, ok := raw.(map[string]interface{})
		if !ok {
			continue
		}
		events, _ := m["data"].([]interface{})
		totalEvents += len(events)
	}

	if totalEvents == 0 {
		sb.WriteString("No data found for this service. Check that the namespace and service name are correct, " +
			"and that the adapter is syncing this namespace.")
		return sb.String()
	}

	sb.WriteString("Agent service unavailable — data was collected but not analysed by Claude.\n\n")
	for podID, raw := range podData {
		m, ok := raw.(map[string]interface{})
		if !ok {
			continue
		}
		events, _ := m["data"].([]interface{})
		if len(events) == 0 {
			continue
		}
		sb.WriteString(fmt.Sprintf("• %s  (%d events)\n", podID, len(events)))
		for _, e := range events {
			ev, ok := e.(map[string]interface{})
			if !ok {
				continue
			}
			payload, _ := ev["payload"].(map[string]interface{})
			evType, _ := ev["type"].(string)
			entityKey, _ := ev["entity_key"].(string)

			switch evType {
			case "pod_modified", "pod_added", "pod_deleted":
				restarts, _ := payload["restarts"].(float64)
				ready, _ := payload["ready"].(bool)
				phase, _ := payload["phase"].(string)
				sb.WriteString(fmt.Sprintf(
					"  – %s  %-12s  phase=%-10s  ready=%-5v  restarts=%.0f\n",
					evType, entityKey, phase, ready, restarts,
				))
			case "service_added":
				clusterIP, _ := payload["cluster_ip"].(string)
				sb.WriteString(fmt.Sprintf("  – %s  %s  ip=%s\n", evType, entityKey, clusterIP))
			}
		}
		sb.WriteByte('\n')
	}
	return sb.String()
}

// extractPodIDs extracts pod IDs from a slice of pods.
func extractPodIDs(pods []*models.Pod) []string {
	ids := make([]string, len(pods))
	for i, pod := range pods {
		ids[i] = pod.ID
	}
	return ids
}

// HandlePodRegistryGet returns a single pod.
func (h *Handler) HandlePodRegistryGet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	podID := r.URL.Query().Get("id")
	if podID == "" {
		http.Error(w, "missing pod id", http.StatusBadRequest)
		return
	}

	pod, err := h.orch.GetPodRegistry().GetPod(r.Context(), podID)
	if err != nil {
		h.logger.Error("failed to get pod", "pod_id", podID, "error", err)
		http.Error(w, "pod not found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(pod)
}

// seedNamespaceCache writes the given namespace->services map into
// current_state so TrackedEntities (and therefore the frontend autocomplete)
// reflects it immediately, without waiting for a real ingestion event to
// arrive. Returns the number of services seeded.
func (h *Handler) seedNamespaceCache(ctx context.Context, byNamespace map[string][]string) int {
	kv, err := h.orch.GetBackendFactory().GetBackend("kv")
	if err != nil {
		return 0
	}
	seeder, ok := kv.(syncSeeder)
	if !ok {
		return 0
	}
	seeded := 0
	for namespace, services := range byNamespace {
		podID := "k8fy.live-state." + namespace
		for _, svc := range services {
			if _, serr := seeder.Store(ctx, podID, map[string]interface{}{
				"entity_key":      svc,
				"event_namespace": namespace,
				"type":            "service_synced",
				"source":          "sync",
				"payload":         map[string]interface{}{"name": svc},
			}); serr == nil {
				seeded++
			}
		}
	}
	return seeded
}

// HandleSyncNamespaces re-derives the namespace/service list from
// cluster_services (populated by Discovery's periodic inventory push, ADR
// 0022 / ROADMAP P18 use case #1) and seeds current_state from it, so the
// frontend search autocomplete can be updated immediately. The CronJob
// (infra/kubernetes/namespace-sync-cronjob.yaml) calls the same endpoint on
// a schedule; a "Sync New Namespaces" UI button calls it on demand.
//
// Prior to ADR 0027 this queried the k8fy adapter live; now it's a plain
// Postgres read — Discovery already keeps cluster_services fresh on its own
// schedule, so there's no live cluster call to make here at all.
func (h *Handler) HandleSyncNamespaces(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost && r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.clusterServiceStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]interface{}{
			"error": "cluster service store not configured",
		})
		return
	}
	byNamespace, err := h.clusterServiceStore.ListClusterServices(r.Context(), pgstore.DefaultTenantID)
	if err != nil {
		h.logger.Warn("namespace discovery failed", "error", err)
		writeJSON(w, http.StatusServiceUnavailable, map[string]interface{}{
			"error": "namespace discovery failed",
		})
		return
	}

	namespaces := make([]NamespaceEntry, 0, len(byNamespace))
	suggestions := make([]string, 0)
	for namespace, services := range byNamespace {
		namespaces = append(namespaces, NamespaceEntry{Namespace: namespace, Services: services, ServiceCount: len(services)})
		for _, svc := range services {
			suggestions = append(suggestions, namespace+"/"+svc)
		}
		if len(services) == 0 {
			// Namespace exists but no services yet — still useful to surface.
			suggestions = append(suggestions, namespace+"/")
		}
	}

	seeded := h.seedNamespaceCache(r.Context(), byNamespace)
	h.logger.Info("seeded current_state from sync", "namespaces", len(namespaces), "services", seeded)

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"namespaces":  namespaces,
		"suggestions": suggestions,
		"total":       len(namespaces),
	})
}

// trackedEntitiesProvider is satisfied by *postgres.CurrentState.
type trackedEntitiesProvider interface {
	TrackedEntities(ctx context.Context) ([]string, error)
}

// syncSeeder is satisfied by *postgres.CurrentState — it lets HandleSyncNamespaces
// write discovered entities directly into current_state so the frontend autocomplete
// reflects live adapter data without waiting for ingestion events to re-arrive.
type syncSeeder interface {
	Store(ctx context.Context, podID string, data map[string]interface{}) (string, error)
}

// HandleTrackedEntities returns all known namespace/service pairs from the
// live-state current_state table. Powers the frontend search autocomplete.
//
// After a scale-up the table may be empty until Discovery's watch stream
// (ADR 0027) re-populates it — which happens within seconds via the watch
// API's initial LIST-then-WATCH semantics, not the 5-minute adapter-polling
// workaround this fallback used to need. The fallback here now just re-reads
// cluster_services (already fresh, no live call) so the very first request
// after a scale-up doesn't have to wait even those few seconds.
func (h *Handler) HandleTrackedEntities(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// The kv backend wraps *postgres.CurrentState which implements TrackedEntities.
	kv, err := h.orch.GetBackendFactory().GetBackend("kv")
	if err != nil {
		writeJSON(w, http.StatusOK, []string{})
		return
	}
	provider, ok := kv.(trackedEntitiesProvider)
	if !ok {
		writeJSON(w, http.StatusOK, []string{})
		return
	}

	entities, err := provider.TrackedEntities(r.Context())
	if err != nil {
		h.logger.Warn("failed to list tracked entities", "error", err)
		writeJSON(w, http.StatusOK, []string{})
		return
	}

	// Empty current_state — fall back to cluster_services (already fresh via
	// Discovery's own push schedule) so the first request after a scale-up
	// returns real data without the user having to wait.
	if len(entities) == 0 && h.clusterServiceStore != nil {
		byNamespace, cerr := h.clusterServiceStore.ListClusterServices(r.Context(), pgstore.DefaultTenantID)
		if cerr == nil && len(byNamespace) > 0 {
			h.seedNamespaceCache(r.Context(), byNamespace)
			entities, _ = provider.TrackedEntities(r.Context())
			h.logger.Info("tracked entities: live-seeded from cluster_services", "count", len(entities))
		}
	}

	if entities == nil {
		entities = []string{}
	}
	writeJSON(w, http.StatusOK, entities)
}

// HandleSimilarIncidents returns past Tier-2 diagnose incidents whose embedding
// is most similar to the query vector provided by the Python agent (P8).
// Query params: namespace, service, limit (default 3), and optionally
// vec=<comma-separated floats> for vector search (falls back to recency).
func (h *Handler) HandleSimilarIncidents(w http.ResponseWriter, r *http.Request) {
	namespace := r.URL.Query().Get("namespace")
	service := r.URL.Query().Get("service")
	limit := 3
	if l := r.URL.Query().Get("limit"); l != "" {
		fmt.Sscanf(l, "%d", &limit)
	}

	// Parse optional query vector from the vec= param.
	var queryVec []float32
	if vecStr := r.URL.Query().Get("vec"); vecStr != "" {
		for _, part := range strings.Split(vecStr, ",") {
			var f float32
			if _, err := fmt.Sscanf(strings.TrimSpace(part), "%f", &f); err == nil {
				queryVec = append(queryVec, f)
			}
		}
	}

	relational, err := h.orch.GetBackendFactory().GetBackend("relational")
	if err != nil {
		writeJSON(w, http.StatusOK, []interface{}{})
		return
	}
	type similarFinder interface {
		FindSimilarIncidents(ctx context.Context, namespace, service string, queryVec []float32, limit int) ([]pgstore.IncidentEmbedding, error)
	}
	finder, ok := relational.(similarFinder)
	if !ok {
		writeJSON(w, http.StatusOK, []interface{}{})
		return
	}

	incidents, err := finder.FindSimilarIncidents(r.Context(), namespace, service, queryVec, limit)
	if err != nil {
		h.logger.Warn("find similar incidents failed", "error", err)
		writeJSON(w, http.StatusOK, []interface{}{})
		return
	}

	type result struct {
		TraceID   string `json:"trace_id"`
		Namespace string `json:"namespace"`
		Service   string `json:"service"`
		Summary   string `json:"summary"`
	}
	out := make([]result, len(incidents))
	for i, inc := range incidents {
		out[i] = result{TraceID: inc.TraceID, Namespace: inc.Namespace, Service: inc.Service, Summary: inc.Summary}
	}
	writeJSON(w, http.StatusOK, out)
}

// HandleListPricing returns all model pricing rows from the database.
// The Python agent and the Admin UI both consume this endpoint.
func (h *Handler) HandleListPricing(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.pricingStore == nil {
		writeJSON(w, http.StatusOK, []interface{}{})
		return
	}
	rows, err := h.pricingStore.ListModelPricing(r.Context())
	if err != nil {
		h.logger.Warn("failed to list model pricing", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	if rows == nil {
		rows = []pgstore.ModelPricing{}
	}
	writeJSON(w, http.StatusOK, rows)
}

// ── Chat handlers ─────────────────────────────────────────────────────────────

// HandleCreateChatSession creates a new conversation session.
// Optional body: { "namespace": "...", "service": "..." }
func (h *Handler) HandleCreateChatSession(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.chatStore == nil {
		http.Error(w, "chat store not available", http.StatusServiceUnavailable)
		return
	}
	var init struct {
		Namespace string `json:"namespace"`
		Service   string `json:"service"`
	}
	_ = json.NewDecoder(r.Body).Decode(&init)

	s := pgstore.ChatSession{
		ID:        uuid.New().String(),
		Namespace: init.Namespace,
		Service:   init.Service,
		Messages:  []pgstore.ChatMessage{},
		ExpiresAt: time.Now().Add(24 * time.Hour),
	}
	if err := h.chatStore.CreateChatSession(r.Context(), &s); err != nil {
		h.logger.Error("create chat session failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusCreated, chatSessionToResponse(s))
}

// HandleListChatSessions returns the 20 most recently active sessions (no messages).
func (h *Handler) HandleListChatSessions(w http.ResponseWriter, r *http.Request) {
	if h.chatStore == nil {
		writeJSON(w, http.StatusOK, []ChatSessionResponse{})
		return
	}
	sessions, err := h.chatStore.ListChatSessions(r.Context(), 20)
	if err != nil {
		h.logger.Error("list chat sessions failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	resp := make([]ChatSessionResponse, len(sessions))
	for i, s := range sessions {
		r2 := chatSessionToResponse(s)
		r2.Messages = nil // omit from list to keep payload small
		resp[i] = r2
	}
	writeJSON(w, http.StatusOK, resp)
}

// HandleGetChatSession returns one session with its full message history.
func (h *Handler) HandleGetChatSession(w http.ResponseWriter, r *http.Request) {
	if h.chatStore == nil {
		http.Error(w, "chat store not available", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	s, err := h.chatStore.GetChatSession(r.Context(), id)
	if err != nil {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	writeJSON(w, http.StatusOK, chatSessionToResponse(*s))
}

// HandleSendChatMessage appends a user turn, calls the agent, and returns the
// assistant reply.  Stage 2: synchronous (no streaming).
func (h *Handler) HandleSendChatMessage(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.chatStore == nil {
		http.Error(w, "chat store not available", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	var req struct {
		Content string `json:"content"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || strings.TrimSpace(req.Content) == "" {
		http.Error(w, "content is required", http.StatusBadRequest)
		return
	}

	s, err := h.chatStore.GetChatSession(r.Context(), id)
	if err != nil {
		http.Error(w, "session not found", http.StatusNotFound)
		return
	}

	start := time.Now()

	// Auto-title from first user message.
	if s.Title == "" {
		t := req.Content
		if len(t) > 60 {
			t = t[:60] + "…"
		}
		s.Title = t
	}

	userMsg := pgstore.ChatMessage{Role: "user", Content: req.Content, CreatedAt: time.Now()}
	s.Messages = append(s.Messages, userMsg)

	// Build message history for the agent in {role, content} format.
	history := make([]map[string]string, len(s.Messages))
	for i, m := range s.Messages {
		history[i] = map[string]string{"role": m.Role, "content": m.Content}
	}

	traceID := uuid.New().String()
	agentResp, agentErr := h.agentClient.Chat(
		history,
		map[string]interface{}{"namespace": s.Namespace, "service": s.Service},
		traceID,
	)

	assistantContent := "I'm sorry, I encountered an error. Please try again."
	if agentErr != nil {
		h.logger.Warn("chat agent call failed", "session_id", id, "error", agentErr)
	} else {
		assistantContent = agentResp.Answer
	}

	var assistantDetails map[string]interface{}
	if agentErr == nil && agentResp != nil {
		assistantDetails = agentResp.Details
	}
	assistantMsg := pgstore.ChatMessage{Role: "assistant", Content: assistantContent, CreatedAt: time.Now(), Details: assistantDetails}
	s.Messages = append(s.Messages, assistantMsg)
	s.LastActive = time.Now()
	s.ExpiresAt = time.Now().Add(24 * time.Hour)

	if err := h.chatStore.UpdateChatSession(r.Context(), s); err != nil {
		h.logger.Warn("chat session update failed", "error", err)
	}

	if agentErr == nil && agentResp != nil {
		h.logTrace(traceID, req.Content, "chat", s.Namespace, "tier2", "ok",
			agentResp.Sources, agentResp.Confidence, toolCallNames(agentResp.ToolCalls),
			start, agentResp)
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"message": assistantMsg,
		"session": chatSessionToResponse(*s),
	})
}

// HandleDeleteChatSession permanently removes a session.
func (h *Handler) HandleDeleteChatSession(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.chatStore == nil {
		http.Error(w, "chat store not available", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	if err := h.chatStore.DeleteChatSession(r.Context(), id); err != nil {
		h.logger.Warn("delete chat session failed", "id", id, "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// liveDiagnosticTools mirrors the agent's LIVE_DIAGNOSTIC_TOOLS allow-list
// (src/agent/k8fy/live_diagnostics.py) — validated here too so obviously bad
// input never leaves the backend, even though the agent enforces the same
// allow-list authoritatively.
var liveDiagnosticTools = map[string]bool{
	"live_list_pods":    true,
	"live_get_pod_logs": true,
	"live_get_events":   true,
	"live_describe_pod": true,
}

// HandleLiveToolCall handles POST /api/live-query — the Chat UI's "Run"
// buttons on a recommended action. Forwards directly to the agent's
// /live-tool-call endpoint (no LLM call involved) and returns its output.
func (h *Handler) HandleLiveToolCall(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Tool      string                 `json:"tool"`
		Arguments map[string]interface{} `json:"arguments"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid request body", http.StatusBadRequest)
		return
	}
	if !liveDiagnosticTools[req.Tool] {
		http.Error(w, "unknown or disallowed tool", http.StatusBadRequest)
		return
	}
	resp, err := h.agentClient.LiveToolCall(req.Tool, req.Arguments)
	if err != nil {
		h.logger.Warn("live tool call failed", "tool", req.Tool, "error", err)
		http.Error(w, "live tool call failed", http.StatusBadGateway)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// HandleUpsertPricing inserts or updates a single model pricing row.
// Body: { model_id, display_name, input_per_mtok, output_per_mtok, cache_write_per_mtok, cache_read_per_mtok }
// HandleCertRenew handles POST /admin/certs/renew.
// It calls the agent with intent "renew_cert" and action "renew" in the context,
// which triggers VaultCertSkill._renew() — a deterministic path that issues a
// new cert from Vault PKI and updates the K8s TLS Secret without an LLM call.
func (h *Handler) HandleCertRenew(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	start := time.Now()
	traceID := uuid.New().String()
	var req CertRenewRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if req.Namespace == "" || req.Service == "" {
		http.Error(w, "namespace and service are required", http.StatusBadRequest)
		return
	}

	ctx := map[string]interface{}{
		"namespace": req.Namespace,
		"service":   req.Service,
		"action":    "renew",
		"ttl":       "24h",
	}
	agentResp, err := h.agentClient.Reason(
		fmt.Sprintf("renew TLS cert for %s/%s", req.Namespace, req.Service),
		"renew_cert",
		map[string]interface{}{},
		ctx,
		"cert-renew-"+req.Namespace+"-"+req.Service,
	)
	if err != nil {
		h.logger.Warn("cert renewal agent call failed", "error", err)
		writeJSON(w, http.StatusInternalServerError, CertRenewResponse{
			Status:  "error",
			Message: "Agent call failed: " + err.Error(),
		})
		return
	}

	details := agentResp.Details
	resp := CertRenewResponse{
		Status:  agentResp.Status,
		Message: agentResp.Answer,
	}
	if details != nil {
		if v, ok := details["serial"].(string); ok {
			resp.Serial = v
		}
		if v, ok := details["ttl"].(string); ok {
			resp.TTL = v
		}
		if v, ok := details["k8s_secret_updated"].(bool); ok {
			resp.K8sSecretUpdated = v
		}
		if v, ok := details["k8s_secret"].(string); ok {
			resp.K8sSecret = v
		}
		if v, ok := details["expires_at"].(string); ok {
			resp.ExpiresAt = v
		}
		if v, ok := details["days_until_expiry"].(float64); ok {
			resp.DaysUntilExpiry = int(v)
		}
		if raw, ok := details["dns_names"].([]interface{}); ok {
			for _, item := range raw {
				if s, ok := item.(string); ok {
					resp.DnsNames = append(resp.DnsNames, s)
				}
			}
		}
	}

	// Write the new cert event directly to the events table so the next
	// /api/query (triggered by onRenewed()) reads fresh expiry data immediately
	// without waiting for the next adapter scrape cycle (default 5 minutes).
	if resp.Status == "ok" && resp.ExpiresAt != "" {
		secretName, _ := details["secret_name"].(string)
		namespace, _ := details["namespace"].(string)
		if secretName == "" {
			secretName = req.Service + "-tls" // best-effort fallback
		}
		if namespace == "" {
			namespace = req.Namespace
		}
		if relational, rerr := h.orch.GetBackendFactory().GetBackend("relational"); rerr == nil {
			type certStorer interface {
				Store(ctx context.Context, podID string, data map[string]interface{}) (string, error)
			}
			if cs, ok := relational.(certStorer); ok {
				dnsNames := make([]interface{}, len(resp.DnsNames))
				for i, d := range resp.DnsNames {
					dnsNames[i] = d
				}
				certCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
				defer cancel()
				eventData := map[string]interface{}{
					// id and timestamp are required by Client.Store (events table PK/column).
					"id":              uuid.New().String(),
					"timestamp":       time.Now().UTC().Format(time.RFC3339),
					"entity_key":      secretName,
					"event_namespace": "k8fy.certificates",
					"type":            "cert_check",
					"source":          "renew",
					"payload": map[string]interface{}{
						"secret":            secretName,
						"namespace":         namespace,
						"expires_at":        resp.ExpiresAt,
						"days_until_expiry": resp.DaysUntilExpiry,
						"should_renew":      false,
						"dns_names":         dnsNames,
					},
				}
				if _, err := cs.Store(certCtx, "k8fy.certificates", eventData); err != nil {
					h.logger.Warn("cert event seed after renewal failed", "error", err)
				} else {
					h.logger.Info("cert event seeded after renewal",
						"secret", secretName, "expires_at", resp.ExpiresAt)
				}
			}
		}
	}

	// Surface the trace ID in the response so the UI can link to Query History.
	resp.TraceID = traceID

	// Log the renewal as a trace so it appears in Query History alongside
	// regular cert_check and diagnose queries.
	h.logTrace(
		traceID,
		fmt.Sprintf("renew TLS cert for %s/%s", req.Namespace, req.Service),
		"renew_cert",
		req.Namespace,
		"tier2",
		resp.Status,
		[]string{"k8fy.certificates"},
		1.0,
		nil,
		start,
		agentResp,
	)

	writeJSON(w, http.StatusOK, resp)
}

func (h *Handler) HandleUpsertPricing(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut && r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.pricingStore == nil {
		http.Error(w, "pricing store not available", http.StatusServiceUnavailable)
		return
	}
	var p pgstore.ModelPricing
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if p.ModelID == "" {
		http.Error(w, "model_id is required", http.StatusBadRequest)
		return
	}
	if err := h.pricingStore.UpsertModelPricing(r.Context(), &p); err != nil {
		h.logger.Warn("failed to upsert model pricing", "model_id", p.ModelID, "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, p)
}

// errInvalidCredential means a Bearer credential was presented but didn't
// match any Integration's collector_token — distinct from "no credential
// presented at all" (which defaults to pgstore.DefaultTenantID, not an error).
var errInvalidCredential = errors.New("invalid credential")

// resolveTenantContext reads an optional Bearer credential and resolves it to
// (tenantID, clusterID) via Integration.CollectorToken (ADR 0022 phase 2).
// Absent header -> (DefaultTenantID, "", nil), today's behavior unchanged --
// this is what keeps src/agent's existing, credential-less calls working
// exactly as before. Presented but unrecognized -> ("", "", errInvalidCredential),
// a 401, never silently defaulted. clusterID is the matched Integration's own
// ID -- Integration rows already ARE the cluster registry, no separate
// cluster identifier is needed.
func (h *Handler) resolveTenantContext(r *http.Request) (tenantID, clusterID string, err error) {
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if token == "" {
		return pgstore.DefaultTenantID, "", nil
	}
	if h.integrationStore == nil {
		return "", "", errInvalidCredential
	}
	integ, err := h.integrationStore.GetIntegrationByCollectorToken(r.Context(), token)
	if errors.Is(err, sql.ErrNoRows) {
		return "", "", errInvalidCredential
	}
	if err != nil {
		return "", "", err
	}
	return integ.TenantID, integ.ID, nil
}

// serviceDependencyUpsertRequest is the body accepted by POST /api/service-dependencies.
// ClusterID is ADR 0029's trusted-internal-caller override — see below.
type serviceDependencyUpsertRequest struct {
	Namespace   string `json:"namespace"`
	FromService string `json:"from_service"`
	ToService   string `json:"to_service"`
	ClusterID   string `json:"cluster_id,omitempty"`
}

// HandleServiceDependencyUpsert records one piece of mined evidence for a
// from->to service call edge (see k8fy/service_topology.py). Best-effort from
// the agent's side — a failure here just means one piece of evidence is lost,
// never surfaces as a diagnosis error.
//
// ADR 0029: req.ClusterID is honored only as a fallback, when
// resolveTenantContext resolved no clusterID of its own (i.e. the caller
// presented no CollectorToken) — the Glue-based dependency miner runs
// centrally in the Agent process, over the same trusted, unauthenticated
// boundary the Agent already calls every other Hub endpoint over, and has no
// per-cluster credential to authenticate as. A real Discovery collector's own
// CollectorToken-derived clusterID always wins over anything in the body, so
// a stray cluster_id field in a genuine collector's push is never honored.
func (h *Handler) HandleServiceDependencyUpsert(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.serviceDepsStore == nil {
		http.Error(w, "service dependency store not available", http.StatusServiceUnavailable)
		return
	}
	tenantID, clusterID, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	var req serviceDependencyUpsertRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if req.Namespace == "" || req.FromService == "" || req.ToService == "" {
		http.Error(w, "namespace, from_service, and to_service are required", http.StatusBadRequest)
		return
	}
	if clusterID == "" && req.ClusterID != "" {
		clusterID = req.ClusterID
	}
	id := uuid.New().String()
	if err := h.serviceDepsStore.UpsertServiceDependency(r.Context(), id, tenantID, clusterID, req.Namespace, req.FromService, req.ToService); err != nil {
		h.logger.Warn("failed to upsert service dependency", "namespace", req.Namespace, "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// namespaceInventory is one namespace's entry in the fleet collector's
// inventory push — the namespace name plus its known services (ROADMAP
// P16 / ADR 0023: the collector already fetches these to decide "active",
// this just stops discarding them after the check).
type namespaceInventory struct {
	Name     string                  `json:"name"`
	Services []serviceInventoryEntry `json:"services"`
}

// serviceInventoryEntry is one Service in a namespaceInventory entry: its
// name plus its K8s selector (ADR 0029 — Discovery already fetches this on
// every scan for its own live from_service matching; carrying it here lets
// a centralized Glue-based dependency miner replicate the same matching
// against stored data instead of a live K8s read). Accepts the legacy
// bare-string wire shape too (Selector omitted/empty) via UnmarshalJSON,
// so an older Discovery build pushing plain service names doesn't break.
type serviceInventoryEntry struct {
	Name     string            `json:"name"`
	Selector map[string]string `json:"selector,omitempty"`
}

func (s *serviceInventoryEntry) UnmarshalJSON(data []byte) error {
	var name string
	if err := json.Unmarshal(data, &name); err == nil {
		s.Name = name
		s.Selector = nil
		return nil
	}
	type alias serviceInventoryEntry
	var a alias
	if err := json.Unmarshal(data, &a); err != nil {
		return err
	}
	*s = serviceInventoryEntry(a)
	return nil
}

// clusterInventoryUpsertRequest is the body accepted by POST /api/cluster-inventory.
type clusterInventoryUpsertRequest struct {
	Namespaces []namespaceInventory `json:"namespaces"`
}

// HandleClusterInventoryUpsert records the fleet collector's live namespace
// + service inventory for its own cluster (ADR 0022 / ROADMAP P18 use case
// #1, extended by ROADMAP P16 / ADR 0023 to also carry service names) —
// auto-populates Integration.Namespaces (unchanged) instead of the
// IntegrationsPanel's manual checkbox entry, and populates the
// cluster_services registry the P16 resolver reads from. Unlike
// HandleServiceDependencyUpsert, an absent or unrecognized credential is
// always rejected here: there is no Integration row to attach namespaces to
// without one, so the usual "no credential -> DefaultTenantID" default
// doesn't apply.
func (h *Handler) HandleClusterInventoryUpsert(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.integrationStore == nil {
		http.Error(w, "integration store not configured", http.StatusServiceUnavailable)
		return
	}
	tenantID, clusterID, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	if clusterID == "" {
		http.Error(w, "a collector credential is required", http.StatusUnauthorized)
		return
	}
	var req clusterInventoryUpsertRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if req.Namespaces == nil {
		req.Namespaces = []namespaceInventory{}
	}

	namespaces := make([]string, 0, len(req.Namespaces))
	byNamespace := make(map[string][]pgstore.ServiceEntry, len(req.Namespaces))
	for _, ns := range req.Namespaces {
		namespaces = append(namespaces, ns.Name)
		entries := make([]pgstore.ServiceEntry, 0, len(ns.Services))
		for _, svc := range ns.Services {
			entries = append(entries, pgstore.ServiceEntry{Name: svc.Name, Selector: svc.Selector})
		}
		byNamespace[ns.Name] = entries
	}

	if err := h.integrationStore.UpdateIntegrationNamespaces(r.Context(), clusterID, namespaces); err != nil {
		h.logger.Warn("failed to update integration namespaces", "cluster_id", clusterID, "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	if h.clusterServiceStore != nil {
		if err := h.clusterServiceStore.UpsertClusterServices(r.Context(), tenantID, clusterID, byNamespace); err != nil {
			h.logger.Warn("failed to update cluster services", "cluster_id", clusterID, "error", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
	}
	w.WriteHeader(http.StatusNoContent)
}

// clusterIngressUpsertRequest is the body accepted by POST /api/cluster-ingress.
type clusterIngressUpsertRequest struct {
	Entries []ingressEndpointEntry `json:"entries"`
}

// ingressEndpointEntry mirrors pgstore.IngressEndpoint's JSON wire shape —
// kept as its own type (rather than reusing pgstore.IngressEndpoint
// directly) so the storage struct's field names/tags can change without
// coupling to the wire contract, same separation every other *Request type
// in this file already keeps from its pgstore counterpart.
type ingressEndpointEntry struct {
	Namespace      string `json:"namespace"`
	Kind           string `json:"kind"`
	Name           string `json:"name"`
	Host           string `json:"host"`
	BackendService string `json:"backend_service"`
}

// HandleClusterIngressUpsert records the fleet collector's entry-point
// mapping (Ingress / Gateway+HTTPRoute / OpenShift Route — ROADMAP P18 use
// case #3) for its own cluster. Same auth shape as
// HandleClusterInventoryUpsert: an absent or unrecognized credential is
// always rejected, since there's no cluster identity to attach entries to
// otherwise.
func (h *Handler) HandleClusterIngressUpsert(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.clusterIngressStore == nil {
		http.Error(w, "cluster ingress store not configured", http.StatusServiceUnavailable)
		return
	}
	tenantID, clusterID, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	if clusterID == "" {
		http.Error(w, "a collector credential is required", http.StatusUnauthorized)
		return
	}
	var req clusterIngressUpsertRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	entries := make([]pgstore.IngressEndpoint, 0, len(req.Entries))
	for _, e := range req.Entries {
		entries = append(entries, pgstore.IngressEndpoint{
			Namespace: e.Namespace, Kind: e.Kind, Name: e.Name, Host: e.Host, BackendService: e.BackendService,
		})
	}
	if err := h.clusterIngressStore.UpsertClusterIngress(r.Context(), tenantID, clusterID, entries); err != nil {
		h.logger.Warn("failed to update cluster ingress endpoints", "cluster_id", clusterID, "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// clusterIngressListResponse is the body returned by GET /api/cluster-ingress.
type clusterIngressListResponse struct {
	Entries []ingressEndpointEntry `json:"entries"`
}

// HandleClusterIngressList answers "what entry points map to this
// namespace?" (ROADMAP P18 use case #3), read from the
// cluster_ingress_endpoints table HandleClusterIngressUpsert populates.
// Store-only surface for now — no agent tool consumes this yet, same
// deliberate scope boundary noted where ClusterIngressStore is declared.
// Same unauthenticated-agent-facing shape as HandleResolveCluster: resolves
// to DefaultTenantID via resolveTenantContext, returns an empty list (200),
// never an error, when nothing matches or the store isn't configured.
func (h *Handler) HandleClusterIngressList(w http.ResponseWriter, r *http.Request) {
	namespace := r.URL.Query().Get("namespace")
	if namespace == "" {
		http.Error(w, "namespace is required", http.StatusBadRequest)
		return
	}
	if h.clusterIngressStore == nil {
		writeJSON(w, http.StatusOK, clusterIngressListResponse{Entries: []ingressEndpointEntry{}})
		return
	}
	tenantID, _, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	rows, err := h.clusterIngressStore.ListClusterIngress(r.Context(), tenantID, namespace)
	if err != nil {
		h.logger.Warn("failed to list cluster ingress endpoints", "namespace", namespace, "error", err)
		writeJSON(w, http.StatusOK, clusterIngressListResponse{Entries: []ingressEndpointEntry{}})
		return
	}
	entries := make([]ingressEndpointEntry, 0, len(rows))
	for _, row := range rows {
		entries = append(entries, ingressEndpointEntry{
			Namespace: row.Namespace, Kind: row.Kind, Name: row.Name, Host: row.Host, BackendService: row.BackendService,
		})
	}
	writeJSON(w, http.StatusOK, clusterIngressListResponse{Entries: entries})
}

// clusterHealthUpsertRequest is the body accepted by POST /api/cluster-health.
type clusterHealthUpsertRequest struct {
	K8sVersion string `json:"k8s_version"`
	PodsTotal  int    `json:"pods_total"`
	PodsReady  int    `json:"pods_ready"`
}

// HandleClusterHealthUpsert records the fleet collector's health/version
// snapshot (ROADMAP P18 use case #5) for its own cluster. Same auth shape as
// HandleClusterIngressUpsert: an absent or unrecognized credential is always
// rejected, since there's no cluster identity to attach a snapshot to
// otherwise.
func (h *Handler) HandleClusterHealthUpsert(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.clusterHealthStore == nil {
		http.Error(w, "cluster health store not configured", http.StatusServiceUnavailable)
		return
	}
	tenantID, clusterID, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	if clusterID == "" {
		http.Error(w, "a collector credential is required", http.StatusUnauthorized)
		return
	}
	var req clusterHealthUpsertRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if err := h.clusterHealthStore.UpsertClusterHealthSnapshot(r.Context(), tenantID, clusterID, req.K8sVersion, req.PodsTotal, req.PodsReady); err != nil {
		h.logger.Warn("failed to upsert cluster health snapshot", "cluster_id", clusterID, "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// clusterHealthSnapshotEntry mirrors pgstore.ClusterHealthSnapshot's JSON
// wire shape — kept as its own type for the same reason
// ingressEndpointEntry is: decouples the wire contract from the storage
// struct's field names/tags.
type clusterHealthSnapshotEntry struct {
	ClusterID  string    `json:"cluster_id"`
	K8sVersion string    `json:"k8s_version"`
	PodsTotal  int       `json:"pods_total"`
	PodsReady  int       `json:"pods_ready"`
	UpdatedAt  time.Time `json:"updated_at"`
}

// clusterHealthListResponse is the body returned by GET /api/cluster-health.
type clusterHealthListResponse struct {
	Snapshots []clusterHealthSnapshotEntry `json:"snapshots"`
}

// HandleClusterHealthList answers "what's the fleet's current health/version
// picture?" (ROADMAP P18 use case #5), read from the
// cluster_health_snapshots table HandleClusterHealthUpsert populates.
// Store-only surface for now — no agent tool or frontend fleet dashboard
// consumes this yet, same deliberate scope boundary noted where
// ClusterHealthStore is declared. Same unauthenticated-agent-facing shape as
// HandleClusterIngressList: resolves to DefaultTenantID via
// resolveTenantContext, returns an empty list (200), never an error, when
// nothing matches or the store isn't configured.
func (h *Handler) HandleClusterHealthList(w http.ResponseWriter, r *http.Request) {
	if h.clusterHealthStore == nil {
		writeJSON(w, http.StatusOK, clusterHealthListResponse{Snapshots: []clusterHealthSnapshotEntry{}})
		return
	}
	tenantID, _, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	rows, err := h.clusterHealthStore.ListClusterHealthSnapshots(r.Context(), tenantID)
	if err != nil {
		h.logger.Warn("failed to list cluster health snapshots", "error", err)
		writeJSON(w, http.StatusOK, clusterHealthListResponse{Snapshots: []clusterHealthSnapshotEntry{}})
		return
	}
	snapshots := make([]clusterHealthSnapshotEntry, 0, len(rows))
	for _, row := range rows {
		snapshots = append(snapshots, clusterHealthSnapshotEntry{
			ClusterID: row.ClusterID, K8sVersion: row.K8sVersion,
			PodsTotal: row.PodsTotal, PodsReady: row.PodsReady, UpdatedAt: row.UpdatedAt,
		})
	}
	writeJSON(w, http.StatusOK, clusterHealthListResponse{Snapshots: snapshots})
}

// resolveClusterResponse is the body returned by GET /api/resolve-cluster.
type resolveClusterResponse struct {
	ClusterIDs []string `json:"cluster_ids"`
}

// HandleResolveCluster answers "which fleet cluster(s) run this
// (namespace, service)?" (ROADMAP P16 / ADR 0023), read from the
// cluster_services registry HandleClusterInventoryUpsert populates. Called
// by the agent over the same unauthenticated trust boundary as
// GET /api/service-dependencies (the agent doesn't present a credential
// today — ADR 0022 Decision #8 flags agent tenant-awareness as a separate,
// unresolved follow-up) — resolves to DefaultTenantID via
// resolveTenantContext the same way. Returns an empty list (200), never an
// error, when nothing matches — callers are expected to degrade to today's
// single-cluster behavior, not treat "unknown" as a failure.
func (h *Handler) HandleResolveCluster(w http.ResponseWriter, r *http.Request) {
	namespace := r.URL.Query().Get("namespace")
	service := r.URL.Query().Get("service")
	if namespace == "" || service == "" {
		http.Error(w, "namespace and service are required", http.StatusBadRequest)
		return
	}
	if h.clusterServiceStore == nil {
		writeJSON(w, http.StatusOK, resolveClusterResponse{ClusterIDs: []string{}})
		return
	}
	tenantID, _, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	clusterIDs, err := h.clusterServiceStore.ResolveServiceClusters(r.Context(), tenantID, namespace, service)
	if err != nil {
		h.logger.Warn("failed to resolve service clusters", "namespace", namespace, "service", service, "error", err)
		writeJSON(w, http.StatusOK, resolveClusterResponse{ClusterIDs: []string{}})
		return
	}
	writeJSON(w, http.StatusOK, resolveClusterResponse{ClusterIDs: clusterIDs})
}

// clusterServiceSelectorsResponse is the body returned by
// GET /api/cluster-service-selectors.
type clusterServiceSelectorsResponse struct {
	Selectors map[string]map[string]string `json:"selectors"` // service name -> selector
}

// HandleClusterServiceSelectors answers "what's this specific cluster's
// namespace's Service->selector map?" (ADR 0029, P18 use case #2's Glue
// extension) — read from the cluster_services registry's selector column,
// which HandleClusterInventoryUpsert now also populates. Unlike
// HandleResolveCluster/GET /api/service-dependencies, this requires an
// explicit cluster_id — a service name can mean a different Service, with a
// different selector, in each cluster, so there is no cross-cluster merge
// here that would make sense. Same unauthenticated trust boundary as every
// other Agent-to-Hub call (the Glue-based miner, this endpoint's only
// caller, has no per-cluster credential of its own). Returns an empty map
// (200), never an error, when nothing matches.
func (h *Handler) HandleClusterServiceSelectors(w http.ResponseWriter, r *http.Request) {
	clusterID := r.URL.Query().Get("cluster_id")
	namespace := r.URL.Query().Get("namespace")
	if clusterID == "" || namespace == "" {
		http.Error(w, "cluster_id and namespace are required", http.StatusBadRequest)
		return
	}
	if h.clusterServiceStore == nil {
		writeJSON(w, http.StatusOK, clusterServiceSelectorsResponse{Selectors: map[string]map[string]string{}})
		return
	}
	tenantID, _, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	selectors, err := h.clusterServiceStore.ListClusterServiceSelectors(r.Context(), tenantID, clusterID, namespace)
	if err != nil {
		h.logger.Warn("failed to list cluster service selectors", "cluster_id", clusterID, "namespace", namespace, "error", err)
		writeJSON(w, http.StatusOK, clusterServiceSelectorsResponse{Selectors: map[string]map[string]string{}})
		return
	}
	writeJSON(w, http.StatusOK, clusterServiceSelectorsResponse{Selectors: selectors})
}

// HandleCollectorConnect upgrades to a persistent outbound WebSocket
// connection used for on-demand live-diagnostic drill-down (ADR 0022
// Decision #7 / ROADMAP P18 use case #9). Same CollectorToken handshake as
// the push endpoints (resolveTenantContext) — an absent/invalid credential
// is rejected exactly like HandleClusterInventoryUpsert, since there's no
// cluster identity to register a connection under otherwise. Blocks for the
// connection's lifetime (CollectorHub.Register only returns on disconnect).
func (h *Handler) HandleCollectorConnect(w http.ResponseWriter, r *http.Request) {
	if h.integrationStore == nil {
		http.Error(w, "integration store not configured", http.StatusServiceUnavailable)
		return
	}
	_, clusterID, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	if clusterID == "" {
		http.Error(w, "a collector credential is required", http.StatusUnauthorized)
		return
	}
	if h.collectorHub == nil {
		http.Error(w, "collector hub not configured", http.StatusServiceUnavailable)
		return
	}
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		h.logger.Error("collector websocket upgrade failed", "cluster_id", clusterID, "error", err)
		return
	}
	h.logger.Info("collector connected", "cluster_id", clusterID)
	disconnectErr := h.collectorHub.Register(clusterID, conn) // blocks until disconnect
	h.logger.Info("collector disconnected", "cluster_id", clusterID, "error", disconnectErr)
}

// liveFetchAllowedTools mirrors LIVE_DIAGNOSTIC_TOOLS in
// src/agent/k8fy/live_diagnostics.py — kept explicit here too so this
// passthrough can never become an arbitrary RPC surface into a cluster.
var liveFetchAllowedTools = map[string]bool{
	"live_list_pods":        true,
	"live_get_pod_logs":     true,
	"live_get_events":       true,
	"live_describe_pod":     true,
	"live_get_certificates": true,
}

// liveFetchRequest is the body accepted by POST /api/live-fetch.
type liveFetchRequest struct {
	ClusterID string         `json:"cluster_id"`
	Tool      string         `json:"tool"`
	Args      map[string]any `json:"args"`
}

// HandleLiveFetch relays one on-demand live-diagnostic call to a specific
// fleet cluster's already-connected collector (ROADMAP P18 use case #9).
// Called by the Python agent over the same trusted, unauthenticated
// boundary as its other backend calls (e.g. GET /api/service-dependencies,
// GET /api/incidents/similar) — no new auth layer invented here. cluster_id
// must be supplied by the caller; this endpoint does not resolve "which
// cluster is service X in" — that's P16 (multi-cluster connector), not
// built.
func (h *Handler) HandleLiveFetch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.collectorHub == nil {
		http.Error(w, "collector hub not configured", http.StatusServiceUnavailable)
		return
	}
	var req liveFetchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if req.ClusterID == "" {
		http.Error(w, "cluster_id is required", http.StatusBadRequest)
		return
	}
	if !liveFetchAllowedTools[req.Tool] {
		http.Error(w, "unsupported tool", http.StatusBadRequest)
		return
	}
	result, err := h.collectorHub.RequestLive(r.Context(), req.ClusterID, req.Tool, req.Args)
	if errors.Is(err, ErrClusterNotConnected) {
		http.Error(w, "cluster not connected", http.StatusBadGateway)
		return
	}
	if errors.Is(err, ErrLiveRequestTimeout) {
		http.Error(w, "live request timed out", http.StatusGatewayTimeout)
		return
	}
	if err != nil {
		h.logger.Warn("live fetch failed", "cluster_id", req.ClusterID, "tool", req.Tool, "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(result)
}

// HandleServiceDependencyList returns every mined edge for one namespace —
// read by DiagnoseSkill's prefetch and the get_service_dependencies chat tool.
func (h *Handler) HandleServiceDependencyList(w http.ResponseWriter, r *http.Request) {
	namespace := r.URL.Query().Get("namespace")
	if namespace == "" {
		http.Error(w, "namespace is required", http.StatusBadRequest)
		return
	}
	if h.serviceDepsStore == nil {
		writeJSON(w, http.StatusOK, []pgstore.ServiceDependency{})
		return
	}
	tenantID, _, err := h.resolveTenantContext(r)
	if errors.Is(err, errInvalidCredential) {
		http.Error(w, "invalid credential", http.StatusUnauthorized)
		return
	}
	if err != nil {
		h.logger.Warn("tenant resolution failed", "error", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	deps, err := h.serviceDepsStore.ListServiceDependencies(r.Context(), tenantID, namespace)
	if err != nil {
		h.logger.Warn("failed to list service dependencies", "namespace", namespace, "error", err)
		writeJSON(w, http.StatusOK, []pgstore.ServiceDependency{})
		return
	}
	writeJSON(w, http.StatusOK, deps)
}
