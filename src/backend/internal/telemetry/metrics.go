// Package telemetry: metrics.go defines the Prometheus metrics for the pipeline
// (ADR 0011). All labels are bounded sets — never label by pod/entity/namespace
// id, which would blow up cardinality.
package telemetry

import (
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	// QueriesTotal counts answered queries by intent, tier (tier1|tier2|no_data),
	// and status (ok|partial|error|no_data) — the Tier-1 vs Tier-2 split.
	QueriesTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "agentify_queries_total",
		Help: "Queries answered, by intent, tier, and status.",
	}, []string{"intent", "tier", "status"})

	// QueryDuration is end-to-end query latency, by tier (deterministic vs agentic).
	QueryDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "agentify_query_duration_seconds",
		Help:    "Query handling latency, by tier.",
		Buckets: prometheus.DefBuckets,
	}, []string{"tier"})

	// AgentCallsTotal counts Tier-2 calls to the agent/LLM, by status (ok|error).
	AgentCallsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "agentify_agent_calls_total",
		Help: "Tier-2 agent (LLM) calls, by status.",
	}, []string{"status"})

	// AgentCallDuration is the agent/LLM round-trip latency seen by the backend.
	AgentCallDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "agentify_agent_call_duration_seconds",
		Help:    "Agent (LLM) round-trip latency.",
		Buckets: []float64{0.5, 1, 2, 4, 8, 16, 32, 64},
	})

	// IngestTotal counts ingested events by store type and result (ok|error).
	IngestTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "agentify_ingest_total",
		Help: "Events ingested, by store type and result.",
	}, []string{"store_type", "result"})

	// RegistryCacheTotal counts pod-registry snapshot reads by result
	// (hit | miss | stale | error) — surfaces registry health/SPOF (ADR 0012).
	RegistryCacheTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "agentify_registry_cache_total",
		Help: "Pod-registry cache reads, by result (hit|miss|stale|error).",
	}, []string{"result"})

	// EventsPurgedTotal counts events deleted by the retention janitor (ADR 0015).
	EventsPurgedTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "agentify_events_purged_total",
		Help: "Events deleted by the retention janitor.",
	})

	// ServiceDependencyCallersPurgedTotal counts caller-pod sightings deleted
	// by their own retention janitor (ROADMAP P27 phase 4, caller cardinality).
	ServiceDependencyCallersPurgedTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "agentify_service_dependency_callers_purged_total",
		Help: "service_dependency_callers rows deleted by the retention janitor.",
	})

	// HTTPRequestsTotal / HTTPRequestDuration are generic HTTP metrics. `path`
	// is safe to label on because our routes carry no path-param ids.
	HTTPRequestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "agentify_http_requests_total",
		Help: "HTTP requests, by method, path, and status code.",
	}, []string{"method", "path", "code"})

	HTTPRequestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "agentify_http_request_duration_seconds",
		Help:    "HTTP request latency, by method and path.",
		Buckets: prometheus.DefBuckets,
	}, []string{"method", "path"})
)

// MetricsHandler serves the Prometheus exposition endpoint (default registry,
// which also includes Go runtime + process metrics).
func MetricsHandler() http.Handler {
	return promhttp.Handler()
}
