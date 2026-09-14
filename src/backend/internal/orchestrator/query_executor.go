package orchestrator

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	"github.com/chan/agentify/backend/internal/models"
	"github.com/chan/agentify/backend/internal/storage"
	"github.com/chan/agentify/backend/internal/storage/postgres"
	"github.com/chan/agentify/backend/internal/storage/registry"
)

// QueryExecutor executes queries by routing to pods and fetching results.
type QueryExecutor struct {
	registry        registry.PodStore
	backendFactory  *storage.BackendFactory
	logger          *slog.Logger
}

// NewQueryExecutor creates a new query executor.
func NewQueryExecutor(reg registry.PodStore, bf *storage.BackendFactory, logger *slog.Logger) *QueryExecutor {
	return &QueryExecutor{
		registry:       reg,
		backendFactory: bf,
		logger:         logger,
	}
}

// Execute routes a query to pod(s), fetches results, and correlates them.
// Confirmed dead code (ROADMAP OPS-10 audit, 2026-09-13) — no caller anywhere
// in the backend; HandleQuery uses RouteToPods/FetchFromPod directly. Kept
// compiling with DefaultTenantID rather than deleted, since removing unused-
// but-plausibly-reachable exported methods is its own separate call.
func (qe *QueryExecutor) Execute(ctx context.Context, intent string, query map[string]interface{}, namespace string) (map[string]interface{}, error) {
	// Step 1: Parse intent and determine target pods
	pods, err := qe.RouteToPods(ctx, intent, namespace, "")
	if err != nil {
		return nil, fmt.Errorf("failed to route query: %w", err)
	}

	if len(pods) == 0 {
		return nil, fmt.Errorf("no pods found for intent: %s", intent)
	}

	// Step 2: Fetch from pod(s)
	results := make(map[string]interface{})
	for _, pod := range pods {
		data, err := qe.FetchFromPod(ctx, postgres.DefaultTenantID, pod, query)
		if err != nil {
			qe.logger.Warn("failed to fetch from pod", "pod_id", pod.ID, "error", err)
			continue
		}
		results[pod.ID] = data
	}

	// Step 3: Correlate results if multiple pods
	if len(pods) > 1 {
		return qe.correlateResults(results), nil
	}

	// Single pod: return its results
	for _, data := range results {
		return map[string]interface{}{"data": data}, nil
	}

	return results, nil
}

// RouteToPods determines which leaf pods should answer the query, aligned with
// the pod taxonomy created by ingestion (see ADR 0005 and pod-formation.md).
//
// `namespace` is the K8s namespace the query is scoped to (the live-state
// partition key), not the integration grouping. Index pods are never returned —
// they hold only a shard map and have no backend data to fetch.
//
// clusterID (ADR 0024) targets a specific fleet cluster's shard, mirroring
// how ingestion builds cluster-scoped pod IDs (models.PodID) — empty
// clusterID (every call site not yet passing one, including the "diagnose"/
// default fan-out below, which isn't touched by this ADR) routes exactly as
// before.
func (qe *QueryExecutor) RouteToPods(ctx context.Context, intent string, namespace string, clusterID string) ([]*models.Pod, error) {
	var pods []*models.Pod
	var err error

	switch intent {
	case "health_check":
		// Current health lives in the per-namespace live-state shard.
		pods, err = qe.routeLiveState(ctx, namespace, clusterID)

	case "metrics_query", "metrics_history":
		// Restart/metric samples are an append-only time-series in their own pod
		// (spec 006), no longer folded into live-state.
		pods, err = qe.getLeafPod(ctx, models.PodID("k8fy.metrics", clusterID))

	case "cert_check":
		pods, err = qe.getLeafPod(ctx, models.PodID("k8fy.certificates", clusterID))

	case "change_history":
		// Deploy/change events live in the append-only events pod (spec 007).
		pods, err = qe.getLeafPod(ctx, models.PodID("k8fy.events", clusterID))

	case "diagnose":
		// Multi-signal diagnosis (spec 005): fan out to every k8fy leaf so the agent
		// sees health + certs + events together and can correlate a likely cause.
		fallthrough

	default:
		// General/diagnostic query: every active k8fy leaf pod (skip index pods).
		// Not cluster-scoped — this path is reached via HandleQuery's initial
		// routing, not the agent's per-tool cluster_id-aware fetches (ADR 0024
		// scopes HandleAgentFetch's tool routing, not this one).
		integration := "k8fy"
		var all []*models.Pod
		all, err = qe.registry.ListPods(ctx, &models.PodFilter{Namespace: &integration})
		pods = leavesOnly(all)
	}

	if err != nil {
		return nil, err
	}

	qe.logger.Info("routed query to pods", "intent", intent, "namespace", namespace, "cluster_id", clusterID, "pod_count", len(pods))
	return pods, nil
}

// routeLiveState targets the live-state shard for the requested K8s namespace
// (and, per ADR 0024, cluster), falling back to a fan-out across all
// live-state shards when the namespace is unspecified or its shard doesn't
// exist yet (cross-shard correlation).
func (qe *QueryExecutor) routeLiveState(ctx context.Context, namespace, clusterID string) ([]*models.Pod, error) {
	if namespace != "" {
		shardID := models.PodID("k8fy.live-state", clusterID, namespace)
		if pod, err := qe.registry.GetPod(ctx, shardID); err == nil && pod.Kind == "leaf" {
			return []*models.Pod{pod}, nil
		}
	}

	// Fan out: all live-state leaf shards (ID prefixed with the family name).
	integration := "k8fy"
	all, err := qe.registry.ListPods(ctx, &models.PodFilter{Namespace: &integration})
	if err != nil {
		return nil, err
	}
	var shards []*models.Pod
	for _, p := range leavesOnly(all) {
		if strings.HasPrefix(p.ID, "k8fy.live-state.") {
			shards = append(shards, p)
		}
	}
	return shards, nil
}

// getLeafPod returns a single leaf pod by ID, or an empty slice if it's missing
// or is an index pod (so the query degrades to "no data" rather than erroring).
func (qe *QueryExecutor) getLeafPod(ctx context.Context, podID string) ([]*models.Pod, error) {
	pod, err := qe.registry.GetPod(ctx, podID)
	if err != nil {
		return nil, nil
	}
	if pod.Kind == "index" {
		return nil, nil
	}
	return []*models.Pod{pod}, nil
}

// leavesOnly filters out index pods (which hold a shard map, not data).
func leavesOnly(pods []*models.Pod) []*models.Pod {
	var leaves []*models.Pod
	for _, p := range pods {
		if p.Kind != "index" {
			leaves = append(leaves, p)
		}
	}
	return leaves
}

// FetchFromPod retrieves data from a single pod. tenantID is server-resolved
// by the caller (ROADMAP OPS-10 / ADR 0022 amendment) and passed straight
// through to the backend, which sets the RLS session variable on the kv
// (current_state) store.
func (qe *QueryExecutor) FetchFromPod(ctx context.Context, tenantID string, pod *models.Pod, query map[string]interface{}) ([]map[string]interface{}, error) {
	backend, err := qe.backendFactory.GetBackend(pod.StoreType)
	if err != nil {
		return nil, err
	}

	results, err := backend.Query(ctx, tenantID, pod.ID, query)
	if err != nil {
		return nil, err
	}

	// Update pod query stats
	if err := qe.registry.UpdateQueryStats(ctx, pod.ID, len(results) > 0, 0); err != nil {
		qe.logger.Warn("failed to update pod stats", "pod_id", pod.ID, "error", err)
	}

	return results, nil
}

// correlateResults combines results from multiple pods.
// Implements context-mesh/policies/correlation.md
func (qe *QueryExecutor) correlateResults(results map[string]interface{}) map[string]interface{} {
	// TODO: implement complex correlation logic
	// For now, just merge and return
	return map[string]interface{}{
		"data":   results,
		"merged": true,
	}
}
