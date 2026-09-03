package api

import (
	"log/slog"
	"net/http"

	"github.com/chan/agentify/backend/internal/telemetry"
)

// NewRouter creates a new HTTP router from a prebuilt handler.
func NewRouter(h *Handler, logger *slog.Logger) http.Handler {
	mux := http.NewServeMux()

	// Health check
	mux.HandleFunc("GET /health", h.HandleHealth)

	// Event ingestion (for adapters to send data)
	mux.HandleFunc("POST /api/ingest", h.HandleIngestEvent)

	// Query endpoint (ops dashboard)
	mux.HandleFunc("POST /api/query", h.HandleQuery)

	// Agent tool callback: lets the Python agent fetch pod data during its loop
	mux.HandleFunc("POST /api/agent/fetch", h.HandleAgentFetch)

	// Admin: query history + metrics summary
	// Version-pinned evaluation (ADR 0030). Bearer-authenticated in-handler; the
	// /admin/ prefix grants nothing on its own.
	mux.HandleFunc("POST /admin/eval/query", h.HandleEvalQuery)

	mux.HandleFunc("GET /admin/traces", h.HandleTraceList)
	mux.HandleFunc("GET /admin/traces/{id}", h.HandleTraceGet)
	mux.HandleFunc("GET /admin/metrics/summary", h.HandleMetricsSummary)

	// Admin: integrations management
	mux.HandleFunc("GET /admin/integrations", h.HandleIntegrationList)
	mux.HandleFunc("POST /admin/integrations", h.HandleIntegrationCreate)
	mux.HandleFunc("GET /admin/integrations/{id}", h.HandleIntegrationGet)
	mux.HandleFunc("PUT /admin/integrations/{id}", h.HandleIntegrationUpdate)
	mux.HandleFunc("DELETE /admin/integrations/{id}", h.HandleIntegrationDelete)

	// Admin: pod registry (observability)
	mux.HandleFunc("GET /admin/pods", h.HandlePodRegistryList)
	mux.HandleFunc("GET /admin/pods/get", h.HandlePodRegistryGet)

	// Admin: tracked namespace/service pairs (powers frontend autocomplete)
	mux.HandleFunc("GET /admin/tracked", h.HandleTrackedEntities)

	// Admin: sync — discover namespaces/services from the adapter (live K8s list)
	mux.HandleFunc("POST /admin/sync", h.HandleSyncNamespaces)
	mux.HandleFunc("GET /admin/sync", h.HandleSyncNamespaces) // also allow GET for CronJob curl

	// Chat: multi-turn conversational debugging
	mux.HandleFunc("POST /api/chat/sessions", h.HandleCreateChatSession)
	mux.HandleFunc("GET /api/chat/sessions", h.HandleListChatSessions)
	mux.HandleFunc("GET /api/chat/sessions/{id}", h.HandleGetChatSession)
	mux.HandleFunc("POST /api/chat/sessions/{id}/messages", h.HandleSendChatMessage)
	mux.HandleFunc("DELETE /api/chat/sessions/{id}", h.HandleDeleteChatSession)
	mux.HandleFunc("POST /api/live-query", h.HandleLiveToolCall)

	// Admin: model pricing — read/edit $/MTok rates shown in the UI and used for trace cost estimates
	mux.HandleFunc("GET /admin/pricing", h.HandleListPricing)
	mux.HandleFunc("PUT /admin/pricing", h.HandleUpsertPricing)
	mux.HandleFunc("POST /admin/pricing", h.HandleUpsertPricing)

	// Semantic memory: similar past incidents retrieval (P8 — called by the Python agent's get_similar_incidents tool)
	mux.HandleFunc("GET /api/incidents/similar", h.HandleSimilarIncidents)

	// Service dependency graph mined from log text (see k8fy/service_topology.py):
	// upsert one piece of evidence, or list a namespace's known edges. Called by
	// DiagnoseSkill's prefetch and the get_service_dependencies chat tool.
	mux.HandleFunc("POST /api/service-dependencies", h.HandleServiceDependencyUpsert)
	mux.HandleFunc("GET /api/service-dependencies", h.HandleServiceDependencyList)

	// Scan-coverage accounting — the denominator for evidence_count
	// (ROADMAP P27 phase 1). Same credential shape as the dependency upsert:
	// a collector's CollectorToken derives (tenant, cluster).
	mux.HandleFunc("POST /api/scan-coverage", h.HandleScanCoverageUpsert)
	mux.HandleFunc("GET /api/scan-coverage", h.HandleScanCoverageList)

	// Fleet collector's namespace/service/deployment inventory push (ADR 0022 /
	// ROADMAP P18 use case #1) — auto-populates Integration.Namespaces.
	mux.HandleFunc("POST /api/cluster-inventory", h.HandleClusterInventoryUpsert)

	// Fleet collector's entry-point mapping push (ROADMAP P18 use case #3) —
	// Ingress/Gateway+HTTPRoute/OpenShift Route, store-only for now (no agent
	// tool reads the GET endpoint yet).
	mux.HandleFunc("POST /api/cluster-ingress", h.HandleClusterIngressUpsert)
	mux.HandleFunc("GET /api/cluster-ingress", h.HandleClusterIngressList)

	// Fleet collector's health/version snapshot push (ROADMAP P18 use case
	// #5), store-only for now (no agent tool or frontend fleet dashboard
	// reads the GET endpoint yet).
	mux.HandleFunc("POST /api/cluster-health", h.HandleClusterHealthUpsert)
	mux.HandleFunc("GET /api/cluster-health", h.HandleClusterHealthList)

	// Fleet collector's persistent outbound connection + the agent's on-demand
	// live-diagnostic relay over it (ADR 0022 Decision #7 / ROADMAP P18 use case #9).
	mux.HandleFunc("GET /api/collector/connect", h.HandleCollectorConnect)
	mux.HandleFunc("POST /api/live-fetch", h.HandleLiveFetch)

	// Service->cluster resolver (ROADMAP P16 / ADR 0023) — "which fleet
	// cluster(s) run this (namespace, service)?", read from the
	// cluster_services registry POST /api/cluster-inventory populates.
	mux.HandleFunc("GET /api/resolve-cluster", h.HandleResolveCluster)

	// One specific cluster's Service->selector map (ADR 0029, P18 use case
	// #2's Glue extension) — consulted by the Glue-based dependency miner,
	// which has no live cluster access of its own.
	mux.HandleFunc("GET /api/cluster-service-selectors", h.HandleClusterServiceSelectors)

	// Admin: on-demand cert renewal — issues from Vault PKI + updates K8s Secret
	mux.HandleFunc("POST /admin/certs/renew", h.HandleCertRenew)

	// Remediation proposals (ADR 0020 / spec 011 Use Cases 1+2): propose-only
	// endpoint + admin approve/reject. Nothing executes without an explicit
	// approve call — see remediation.go.
	mux.HandleFunc("POST /api/incidents/respond", h.HandleIncidentRespond)
	mux.HandleFunc("GET /admin/remediation", h.HandleRemediationList)
	mux.HandleFunc("GET /admin/remediation/{id}", h.HandleRemediationGet)
	mux.HandleFunc("POST /admin/remediation/{id}/approve", h.HandleRemediationApprove)
	mux.HandleFunc("POST /admin/remediation/{id}/reject", h.HandleRemediationReject)

	// TODO: add WebSocket handler for chat
	// mux.HandleFunc("/ws/chat", h.HandleChatWebSocket)

	// Outer mux: /metrics bypasses the logging+metrics middleware so scrapes
	// don't pollute request logs or the HTTP counters (ADR 0011). Everything else
	// goes through the middleware-wrapped API mux.
	root := http.NewServeMux()
	root.Handle("GET /metrics", telemetry.MetricsHandler())
	root.Handle("/", NewMiddleware(mux, logger))
	return root
}
