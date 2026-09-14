package ingestion

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/chan/agentify/backend/internal/config"
	"github.com/chan/agentify/backend/internal/models"
	"github.com/chan/agentify/backend/internal/storage"
	"github.com/chan/agentify/backend/internal/storage/registry"
	"github.com/chan/agentify/backend/internal/telemetry"
)

// Ingester handles event ingestion into the context-mesh.
// It classifies events, routes them to pods, creates pods as needed, and emits feedback.
type Ingester struct {
	registry registry.PodStore
	backends *storage.BackendFactory
	logger   *slog.Logger
	// TODO: add refinement loop feedback emitter (SQS, Kafka, etc.)
}

// NewIngester creates a new event ingester.
func NewIngester(reg registry.PodStore, backends *storage.BackendFactory, logger *slog.Logger) *Ingester {
	return &Ingester{
		registry: reg,
		backends: backends,
		logger:   logger,
	}
}

// Ingest accepts an event, classifies it, routes to a pod, stores it, and returns feedback.
//
// tenantID/clusterID (ADR 0024) come from the caller having already resolved
// the pushing collector/adapter's credential (HandleIngestEvent's
// resolveTenantContext) — never from the event body itself, same trust
// boundary as every other tenant-scoped write in this codebase. An empty
// clusterID (no credential presented — every deployment that hasn't
// onboarded a fleet collector) routes and stores exactly as before this ADR;
// this is what keeps existing single-cluster deployments byte-for-byte
// unchanged.
//
// Workflow:
// 1. Classify the event by traits (shape, access pattern, temporality, etc.)
// 2. Route the event to the appropriate pod(s) using storage-strategy
// 3. Create a new pod if no suitable pod exists (pod-formation rules)
// 4. Store the event in the target pod's storage backend
// 5. Update the pod registry (freshness, event count)
// 6. Emit feedback for the refinement loop
func (ing *Ingester) Ingest(ctx context.Context, event *models.Event, tenantID, clusterID string) (*models.EventIngestionResult, error) {
	start := time.Now()

	// Validate event
	if event.ID == "" {
		event.ID = uuid.New().String()
	}
	if event.Timestamp.IsZero() {
		event.Timestamp = time.Now()
	}

	ing.logger.Info("ingesting event",
		"event_id", event.ID,
		"namespace", event.EventNamespace,
		"type", event.Type,
	)

	// Step 1: Determine target pod(s) via storage routing
	targetPod, created, err := ing.routeAndCreatePod(ctx, event, tenantID, clusterID)
	if err != nil {
		ing.logger.Error("failed to route event", "event_id", event.ID, "error", err)
		return nil, err
	}

	// Step 2: Store event in the target pod's storage backend
	if err := ing.storeEvent(ctx, targetPod, event, tenantID, clusterID); err != nil {
		telemetry.IngestTotal.WithLabelValues(targetPod.StoreType, "error").Inc()
		ing.logger.Error("failed to store event", "event_id", event.ID, "pod_id", targetPod.ID, "error", err)
		return nil, err
	}
	telemetry.IngestTotal.WithLabelValues(targetPod.StoreType, "ok").Inc()

	// Step 3: Update pod registry
	if err := ing.registry.UpdateFreshness(ctx, targetPod.ID, targetPod.EventCount+1); err != nil {
		ing.logger.Error("failed to update pod freshness", "pod_id", targetPod.ID, "error", err)
		return nil, err
	}

	// Step 4: Emit feedback for refinement loop
	// TODO: emit to SQS/Kafka
	_ = event

	latency := time.Since(start).Milliseconds()
	result := &models.EventIngestionResult{
		EventID:    event.ID,
		PodID:      targetPod.ID,
		CreatedPod: created,
		StoreType:  targetPod.StoreType,
		Latency:    latency,
		Timestamp:  time.Now(),
	}

	ing.logger.Info("event ingested successfully",
		"event_id", event.ID,
		"pod_id", targetPod.ID,
		"created_pod", created,
		"latency_ms", latency,
	)

	return result, nil
}

// storeEvent persists an event into the storage backend selected for the pod's
// store type. Both families are Postgres-backed (ADR 0010): "kv" → the
// current_state table, "relational" → the events table. The backend's Store
// contract expects a flat map with id/event_namespace/type/timestamp and a
// nested payload (see storage/postgres).
//
// The record id depends on temporality:
//   - current-state stores (kv) key by EntityKey so the latest event for an
//     entity overwrites the previous one (latest-wins). See ADR 0005.
//   - history stores (relational) key by the unique event ID so every event is
//     retained as its own row.
func (ing *Ingester) storeEvent(ctx context.Context, pod *models.Pod, event *models.Event, tenantID, clusterID string) error {
	backend, err := ing.backends.GetBackend(pod.StoreType)
	if err != nil {
		return fmt.Errorf("no backend for store type %q: %w", pod.StoreType, err)
	}

	// A watch DELETED event is normalised to an event_type ending "_deleted"
	// (pod_deleted, service_deleted — discovery/normalize.py) meaning the
	// underlying K8s object no longer exists. On the kv (current_state)
	// backend that must remove the row, not upsert it with the object's
	// last-known state: before this fix current_state never forgot a dead
	// pod, so ten rollouts left ~10 rows per service and readiness ratios
	// like "9/1 · 1 not ready" for a healthy 1-replica Deployment (ROADMAP
	// OPS-12). The relational/vector event history is unaffected — it is
	// append-only and correctly keeps this event like any other.
	if pod.StoreType == "kv" && strings.HasSuffix(event.Type, "_deleted") && event.EntityKey != "" {
		type kvDeleter interface {
			Delete(ctx context.Context, tenantID, podID, entityKey string) error
		}
		if deleter, ok := backend.(kvDeleter); ok {
			if err := deleter.Delete(ctx, tenantID, pod.ID, event.EntityKey); err != nil {
				return err
			}
			ing.logger.Debug("current-state entity deleted", "event_id", event.ID, "pod_id", pod.ID, "entity_key", event.EntityKey)
			return nil
		}
	}

	recordID := event.ID
	if pod.StoreType == "kv" && event.EntityKey != "" {
		recordID = event.EntityKey
	}

	data := map[string]interface{}{
		"id":              recordID,
		"event_id":        event.ID,
		"entity_key":      event.EntityKey,
		"event_namespace": event.EventNamespace,
		"type":            event.Type,
		"timestamp":       event.Timestamp.Format(time.RFC3339),
		"source":          event.Source,
		"payload":         event.Payload,
		"tenant_id":       tenantID,
		"cluster_id":      clusterID,
	}

	storedKey, err := backend.Store(ctx, pod.ID, data)
	if err != nil {
		return err
	}

	ing.logger.Debug("event stored", "event_id", event.ID, "pod_id", pod.ID, "store_type", pod.StoreType, "key", storedKey)
	return nil
}

// routeAndCreatePod determines the target leaf pod for an event and creates it
// (and, for sharded families, its parent index pod) on demand.
//
// It implements pod-formation by consulting the event profile registry
// (context-mesh/policies/storage-strategy.md Step 3) keyed on event_namespace.
// For a "by-namespace" family the event lands in a per-partition shard
// (e.g. k8fy.live-state.prod) under an index pod (k8fy.live-state); see ADR 0002
// and ADR 0005. Unknown namespaces fall back to trait-based classification.
//
// clusterID (ADR 0024) is folded into the leaf pod ID via models.PodID —
// empty clusterID reproduces today's plain "{family}.{partition}" shape
// exactly; a non-empty one inserts a cluster segment so two clusters'
// identically-named partitions (e.g. both running a "payments" namespace)
// never collide in the same shard. The parent index pod's own ID
// (profile.EventNamespace) is unchanged either way — it covers every
// cluster's shards in one shard map.
func (ing *Ingester) routeAndCreatePod(ctx context.Context, event *models.Event, tenantID, clusterID string) (*models.Pod, bool, error) {
	profile, ok := config.LookupProfile(event.EventNamespace)
	if !ok {
		// Classify-don't-enumerate fallback: derive the store from traits and
		// treat the namespace as its own unsharded family.
		profile = config.EventProfile{
			EventNamespace: event.EventNamespace,
			Integration:    config.IntegrationOf(event.EventNamespace),
			StoreType:      deriveStoreType(event.Traits),
			Sharded:        false,
		}
	}

	// Resolve the target leaf pod ID (and partition for sharded families).
	partition := ""
	if profile.Sharded {
		partition = partitionValue(event, profile.PartitionField)
	}
	podID := models.PodID(profile.EventNamespace, clusterID, partition)
	if profile.Sharded {
		// Maintain the parent index pod's shard map. Non-fatal: leaf storage
		// must still succeed even if the index update races or fails.
		if err := ing.ensureIndexPod(ctx, profile, podID, partition); err != nil {
			ing.logger.Warn("failed to maintain index pod",
				"family", profile.EventNamespace, "shard", podID, "error", err)
		}
	}

	// Reuse the leaf pod if it already exists.
	if existing, err := ing.registry.GetPod(ctx, podID); err == nil {
		return existing, false, nil
	}

	ing.logger.Info("creating new pod", "pod_id", podID, "integration", profile.Integration, "store_type", profile.StoreType, "cluster_id", clusterID)

	leaf := &models.Pod{
		ID:           podID,
		Kind:         "leaf",
		Summary:      podSummary(profile, partition),
		Namespace:    profile.Integration,
		Tags:         podTags(profile, partition),
		StoreType:    profile.StoreType,
		Authority:    event.Traits.Authority,
		Lifecycle:    "active",
		PartitionKey: profile.PartitionField,
		Freshness:    time.Now(),
		TenantID:     tenantID,
		ClusterID:    clusterID,
	}
	if err := ing.registry.UpsertPod(ctx, leaf); err != nil {
		return nil, false, fmt.Errorf("failed to create pod: %w", err)
	}
	return leaf, true, nil
}

// ensureIndexPod creates the parent index pod for a sharded family if needed and
// records the shard in its shard map (see ADR 0002 — recursive pods). The index
// pod holds only the map, not data, so its store type is "passthrough".
func (ing *Ingester) ensureIndexPod(ctx context.Context, profile config.EventProfile, shardID, partition string) error {
	idx, err := ing.registry.GetPod(ctx, profile.EventNamespace)
	if err != nil {
		idx = &models.Pod{
			ID:           profile.EventNamespace,
			Kind:         "index",
			Summary:      fmt.Sprintf("%s index, sharded by %s", profile.EventNamespace, profile.PartitionField),
			Namespace:    profile.Integration,
			Tags:         []string{profile.Integration, "index", familyTag(profile.EventNamespace)},
			StoreType:    "passthrough",
			Authority:    "derived",
			Lifecycle:    "active",
			PartitionKey: profile.PartitionField,
			Shards:       []models.ShardRef{},
			Freshness:    time.Now(),
		}
	}

	for _, s := range idx.Shards {
		if s.ChildID == shardID {
			return nil // shard already mapped
		}
	}
	idx.Shards = append(idx.Shards, models.ShardRef{ChildID: shardID, Partition: partition})
	return ing.registry.UpsertPod(ctx, idx)
}

// partitionValue reads the shard partition (e.g. the K8s namespace) from the
// event payload field declared by the profile.
func partitionValue(event *models.Event, field string) string {
	if field == "" {
		return ""
	}
	if v, ok := event.Payload[field].(string); ok {
		return v
	}
	return ""
}

// familyTag returns the last segment of an event namespace (e.g.
// "k8fy.live-state" → "live-state"), used as a routing tag on pods.
func familyTag(eventNamespace string) string {
	if i := strings.LastIndexByte(eventNamespace, '.'); i >= 0 {
		return eventNamespace[i+1:]
	}
	return eventNamespace
}

// podTags builds the routing tags for a leaf pod: integration, family, and
// (for shards) the partition value.
func podTags(profile config.EventProfile, partition string) []string {
	tags := []string{profile.Integration, familyTag(profile.EventNamespace)}
	if partition != "" {
		tags = append(tags, partition)
	}
	return tags
}

// podSummary builds a human-readable one-line description for a leaf pod.
func podSummary(profile config.EventProfile, partition string) string {
	if partition != "" {
		return fmt.Sprintf("%s data for %s=%s", profile.EventNamespace, profile.PartitionField, partition)
	}
	return fmt.Sprintf("%s data", profile.EventNamespace)
}

// deriveStoreType determines which storage backend to use based on event traits.
// Implements context-mesh/policies/storage-strategy.md decision function.
func deriveStoreType(traits models.EventTraits) string {
	// Decision matrix from storage-strategy.md
	switch traits.AccessPattern {
	case "similarity", "semantic":
		return "vector"
	case "point-lookup":
		if traits.Temporality == "current-state" {
			return "kv"
		}
	case "filter+aggregate":
		return "relational"
	case "relationship-traversal":
		return "graph"
	case "time-range-scan":
		if traits.Shape == "numeric/metric" {
			return "timeseries"
		}
		if traits.Temporality == "append-only" {
			return "logs"
		}
	}

	// Default to relational for structured data
	if traits.Shape == "structured" {
		return "relational"
	}

	// Default to logs for unstructured
	return "logs"
}
