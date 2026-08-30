package postgres

import (
	"context"
	"database/sql"
	"log/slog"
	"strings"
	"testing"
	"time"

	embeddedpostgres "github.com/fergusstrange/embedded-postgres"
	"github.com/google/uuid"
)

// startEmbedded boots a throwaway Postgres for the test, or skips if the binary
// can't be downloaded/started (e.g. no network / unsupported platform).
func startEmbedded(t *testing.T) *Client {
	t.Helper()
	pg := embeddedpostgres.NewDatabase(
		embeddedpostgres.DefaultConfig().
			Port(54329).
			Database("agentify_test"),
	)
	if err := pg.Start(); err != nil {
		t.Skipf("embedded postgres unavailable: %v", err)
	}
	t.Cleanup(func() { _ = pg.Stop() })

	// Embedded-postgres is already running on port 54329 at this point, so a
	// short context is fine — no retry delay expected.
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	client, err := NewClient(
		ctx,
		"host=localhost port=54329 user=postgres password=postgres dbname=agentify_test sslmode=disable",
		slog.New(slog.NewTextHandler(nopWriter{}, nil)),
	)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return client
}

type nopWriter struct{}

func (nopWriter) Write(p []byte) (int, error) { return len(p), nil }

func TestPostgresStores(t *testing.T) {
	client := startEmbedded(t)
	ctx := context.Background()

	t.Run("current_state upsert latest-wins + scan + point-lookup", func(t *testing.T) {
		cs := client.CurrentStateStore()
		pod := "k8fy.live-state.prod"

		// pod-a: first crashing, then overwritten healthy (latest-wins).
		mustStore(t, cs, pod, "pod-a", map[string]interface{}{"pod_id": "pod-a", "ready": false, "restarts": float64(7), "reason": "CrashLoopBackOff"})
		mustStore(t, cs, pod, "pod-a", map[string]interface{}{"pod_id": "pod-a", "ready": true, "restarts": float64(0)})
		mustStore(t, cs, pod, "pod-b", map[string]interface{}{"pod_id": "pod-b", "ready": true, "restarts": float64(0)})

		// scan: 2 distinct entities (pod-a deduped by upsert).
		rows, err := cs.Query(ctx, pod, map[string]interface{}{})
		if err != nil {
			t.Fatalf("scan: %v", err)
		}
		if len(rows) != 2 {
			t.Fatalf("scan: want 2 entities, got %d", len(rows))
		}

		// point lookup pod-a: payload must be a map and reflect the latest write.
		one, err := cs.Query(ctx, pod, map[string]interface{}{"key": "pod-a"})
		if err != nil || len(one) != 1 {
			t.Fatalf("point lookup: err=%v rows=%d", err, len(one))
		}
		payload, ok := one[0]["payload"].(map[string]interface{})
		if !ok {
			t.Fatalf("payload should decode to a map, got %T", one[0]["payload"])
		}
		if payload["ready"] != true {
			t.Errorf("latest-wins failed: want ready=true, got %v", payload["ready"])
		}
	})

	t.Run("events append + payload decodes to map", func(t *testing.T) {
		pod := "k8fy.events"
		for i := 0; i < 2; i++ {
			_, err := client.Store(ctx, pod, map[string]interface{}{
				"id":              uuid.New().String(),
				"event_namespace": "k8fy.events",
				"type":            "pod_restart",
				"timestamp":       "2026-06-02T00:00:00Z",
				"payload":         map[string]interface{}{"reason": "CrashLoopBackOff"},
			})
			if err != nil {
				t.Fatalf("event store: %v", err)
			}
		}
		rows, err := client.Query(ctx, pod, nil)
		if err != nil || len(rows) != 2 {
			t.Fatalf("event query: err=%v rows=%d", err, len(rows))
		}
		if _, ok := rows[0]["payload"].(map[string]interface{}); !ok {
			t.Errorf("event payload should decode to a map, got %T", rows[0]["payload"])
		}
	})

	t.Run("ADR 0024: Store persists tenant_id/cluster_id on events and current_state", func(t *testing.T) {
		eventID := uuid.New().String()
		if _, err := client.Store(ctx, "k8fy.events.cluster-42", map[string]interface{}{
			"id":              eventID,
			"event_namespace": "k8fy.events",
			"type":            "pod_restart",
			"timestamp":       "2026-08-03T00:00:00Z",
			"payload":         map[string]interface{}{"reason": "OOMKilled"},
			"tenant_id":       "tenant-z",
			"cluster_id":      "cluster-42",
		}); err != nil {
			t.Fatalf("events store: %v", err)
		}
		var eventTenant, eventCluster string
		if err := client.db.QueryRowContext(ctx,
			`SELECT tenant_id, cluster_id FROM events WHERE id = $1`, eventID,
		).Scan(&eventTenant, &eventCluster); err != nil {
			t.Fatalf("query event tenant/cluster: %v", err)
		}
		if eventTenant != "tenant-z" || eventCluster != "cluster-42" {
			t.Errorf("events row: want tenant-z/cluster-42, got %s/%s", eventTenant, eventCluster)
		}

		cs := client.CurrentStateStore()
		if _, err := cs.Store(ctx, "k8fy.live-state.cluster-42.payments", map[string]interface{}{
			"entity_key":      "payment-api-abc",
			"event_namespace": "k8fy.live-state",
			"type":            "pod_modified",
			"source":          "kubernetes-api",
			"payload":         map[string]interface{}{"ready": true},
			"tenant_id":       "tenant-z",
			"cluster_id":      "cluster-42",
		}); err != nil {
			t.Fatalf("current_state store: %v", err)
		}
		var csTenant, csCluster string
		if err := client.db.QueryRowContext(ctx,
			`SELECT tenant_id, cluster_id FROM current_state WHERE pod_id = $1 AND entity_key = $2`,
			"k8fy.live-state.cluster-42.payments", "payment-api-abc",
		).Scan(&csTenant, &csCluster); err != nil {
			t.Fatalf("query current_state tenant/cluster: %v", err)
		}
		if csTenant != "tenant-z" || csCluster != "cluster-42" {
			t.Errorf("current_state row: want tenant-z/cluster-42, got %s/%s", csTenant, csCluster)
		}

		// Omitting tenant_id/cluster_id (every call site not yet passing them)
		// must still succeed and default sensibly — the byte-for-byte-unchanged
		// guarantee for existing deployments.
		if _, err := client.Store(ctx, "k8fy.events", map[string]interface{}{
			"id":              uuid.New().String(),
			"event_namespace": "k8fy.events",
			"type":            "pod_restart",
			"timestamp":       "2026-08-03T00:00:00Z",
			"payload":         map[string]interface{}{"reason": "OOMKilled"},
		}); err != nil {
			t.Fatalf("events store without tenant/cluster: %v", err)
		}
	})

	t.Run("multi-tenancy migration: existing insert paths default tenant_id, leave cluster_id empty", func(t *testing.T) {
		// ADR 0022, phase 1 (schema only): CreateIntegration doesn't reference
		// tenant_id/cluster_id at all, so Postgres must apply the column
		// default/NULL on its own — this is the guarantee the whole
		// schema-only phase rests on (no existing INSERT path changed).
		in := &Integration{
			ID:         uuid.New().String(),
			Name:       "tenancy-test",
			Namespaces: []string{},
			Status:     "inactive",
		}
		if err := client.CreateIntegration(ctx, in); err != nil {
			t.Fatalf("create integration: %v", err)
		}
		got, err := client.GetIntegration(ctx, in.ID)
		if err != nil {
			t.Fatalf("get integration: %v", err)
		}
		if got.TenantID != DefaultTenantID {
			t.Errorf("tenant_id: want default %q, got %q", DefaultTenantID, got.TenantID)
		}
		if got.ClusterID != "" {
			t.Errorf("cluster_id: want empty (NULL), got %q", got.ClusterID)
		}
	})

	t.Run("UpdateIntegrationNamespaces overwrites only namespaces, leaving other fields untouched", func(t *testing.T) {
		id := uuid.New().String()
		in := &Integration{
			ID: id, Name: "cluster-a", Namespaces: []string{"old-ns"},
			Status: "active", CollectorToken: "collector-secret",
		}
		if err := client.CreateIntegration(ctx, in); err != nil {
			t.Fatalf("create integration: %v", err)
		}

		if err := client.UpdateIntegrationNamespaces(ctx, id, []string{"payments", "checkout"}); err != nil {
			t.Fatalf("update namespaces: %v", err)
		}

		got, err := client.GetIntegration(ctx, id)
		if err != nil {
			t.Fatalf("get integration: %v", err)
		}
		if len(got.Namespaces) != 2 || got.Namespaces[0] != "payments" || got.Namespaces[1] != "checkout" {
			t.Errorf("namespaces: want [payments checkout], got %v", got.Namespaces)
		}
		if got.Name != "cluster-a" {
			t.Errorf("unrelated fields should be untouched: got name=%q", got.Name)
		}
	})

	t.Run("ADR 0025: Token and TokenSecretARN are mutually exclusive across updates", func(t *testing.T) {
		id := uuid.New().String()
		in := &Integration{
			ID: id, Name: "secrets-test", Namespaces: []string{},
			Status: "active", Token: "plaintext-token",
		}
		if err := client.CreateIntegration(ctx, in); err != nil {
			t.Fatalf("create integration: %v", err)
		}
		got, err := client.GetIntegration(ctx, id)
		if err != nil {
			t.Fatalf("get integration: %v", err)
		}
		if got.Token != "plaintext-token" || got.TokenSecretARN != "" {
			t.Fatalf("after create: want plaintext token, empty ARN; got token=%q arn=%q", got.Token, got.TokenSecretARN)
		}

		// Simulate the handler switching this row to Secrets-Manager mode:
		// it supplies a new ARN and leaves Token empty. The plaintext token
		// must be cleared, not just left stale alongside the new ARN.
		in.TokenSecretARN = "arn:aws:secretsmanager:test:000000000000:secret:agentify/dev/integrations/" + id
		in.Token = ""
		if err := client.UpdateIntegration(ctx, in); err != nil {
			t.Fatalf("update to secrets-manager mode: %v", err)
		}
		got, err = client.GetIntegration(ctx, id)
		if err != nil {
			t.Fatalf("get integration: %v", err)
		}
		if got.Token != "" {
			t.Errorf("token should be cleared once TokenSecretARN is set, got %q", got.Token)
		}
		if got.TokenSecretARN != in.TokenSecretARN {
			t.Errorf("token_secret_arn: want %q, got %q", in.TokenSecretARN, got.TokenSecretARN)
		}

		// An update with both empty must preserve the current (ARN) state.
		unrelated := &Integration{ID: id, Name: "secrets-test", Namespaces: []string{}, Status: "active"}
		if err := client.UpdateIntegration(ctx, unrelated); err != nil {
			t.Fatalf("no-op credential update: %v", err)
		}
		got, err = client.GetIntegration(ctx, id)
		if err != nil {
			t.Fatalf("get integration: %v", err)
		}
		if got.TokenSecretARN != in.TokenSecretARN || got.Token != "" {
			t.Errorf("empty-credential update should preserve existing ARN state; got token=%q arn=%q", got.Token, got.TokenSecretARN)
		}

		// Switching back to plaintext must clear the stale ARN.
		unrelated.Token = "new-plaintext-token"
		if err := client.UpdateIntegration(ctx, unrelated); err != nil {
			t.Fatalf("update back to plaintext: %v", err)
		}
		got, err = client.GetIntegration(ctx, id)
		if err != nil {
			t.Fatalf("get integration: %v", err)
		}
		if got.Token != "new-plaintext-token" || got.TokenSecretARN != "" {
			t.Errorf("switching back to plaintext should clear ARN; got token=%q arn=%q", got.Token, got.TokenSecretARN)
		}
	})

	t.Run("ADR 0024: TrackedEntities extracts the real namespace from a cluster-scoped pod_id", func(t *testing.T) {
		cs := client.CurrentStateStore()
		mustStore(t, cs, "k8fy.live-state.orders", "order-worker-abc123-xz9y2", map[string]interface{}{
			"pod_id": "order-worker-abc123-xz9y2", "ready": true,
		})
		mustStore(t, cs, "k8fy.live-state.cluster-77.orders", "order-worker-def456-ab1c3", map[string]interface{}{
			"pod_id": "order-worker-def456-ab1c3", "ready": true,
		})

		entities, err := cs.TrackedEntities(ctx)
		if err != nil {
			t.Fatalf("TrackedEntities: %v", err)
		}
		found := map[string]bool{}
		for _, e := range entities {
			found[e] = true
		}
		if !found["orders/order-worker"] {
			t.Errorf("want orders/order-worker (unscoped) in %v", entities)
		}
		// The cluster-scoped row must resolve to the same clean namespace, not
		// "cluster-77.orders/...".
		hasCleanClusterEntry := false
		for e := range found {
			if strings.HasPrefix(e, "orders/order-worker") {
				hasCleanClusterEntry = true
			}
			if strings.Contains(e, "cluster-77") {
				t.Errorf("cluster segment leaked into TrackedEntities output: %q", e)
			}
		}
		if !hasCleanClusterEntry {
			t.Errorf("expected at least one clean orders/order-worker entry, got %v", entities)
		}
	})

	t.Run("cluster_services registry: resolve by (namespace, service), including ambiguous multi-cluster matches", func(t *testing.T) {
		tenantID := uuid.New().String()

		if err := client.UpsertClusterServices(ctx, tenantID, "cluster-a", map[string][]ServiceEntry{
			"payments": {
				{Name: "payment-api", Selector: map[string]string{"app": "payment-api"}},
				{Name: "payment-worker", Selector: map[string]string{"app": "payment-worker"}},
			},
		}); err != nil {
			t.Fatalf("upsert cluster-a: %v", err)
		}
		if err := client.UpsertClusterServices(ctx, tenantID, "cluster-b", map[string][]ServiceEntry{
			// same service name, a different cluster -> ambiguous; deliberately a
			// DIFFERENT selector than cluster-a's payment-api, so a test relying
			// on cluster-a's selector by name alone (rather than by cluster_id)
			// would get the wrong answer.
			"payments": {{Name: "payment-api", Selector: map[string]string{"app": "payment-api-b"}}},
		}); err != nil {
			t.Fatalf("upsert cluster-b: %v", err)
		}

		// payment-worker only exists in cluster-a -> single unambiguous match.
		clusters, err := client.ResolveServiceClusters(ctx, tenantID, "payments", "payment-worker")
		if err != nil {
			t.Fatalf("resolve payment-worker: %v", err)
		}
		if len(clusters) != 1 || clusters[0] != "cluster-a" {
			t.Errorf("payment-worker clusters: want [cluster-a], got %v", clusters)
		}

		// payment-api exists in both -> ambiguous, both surfaced (correlation.md:
		// surface disagreement, don't silently pick a winner).
		clusters, err = client.ResolveServiceClusters(ctx, tenantID, "payments", "payment-api")
		if err != nil {
			t.Fatalf("resolve payment-api: %v", err)
		}
		if len(clusters) != 2 {
			t.Errorf("payment-api clusters: want 2 matches, got %v", clusters)
		}

		// ListClusterServices (ADR 0027) — the reverse direction: every
		// (namespace, service) pair for the tenant, deduped across clusters.
		// This is what replaced the retired k8fy adapter's live
		// DiscoverNamespaces() call for the Hub's own namespace-sync endpoints.
		byNamespace, err := client.ListClusterServices(ctx, tenantID)
		if err != nil {
			t.Fatalf("list cluster services: %v", err)
		}
		if len(byNamespace["payments"]) != 2 {
			t.Errorf("payments services: want [payment-api payment-worker], got %v", byNamespace["payments"])
		}

		// Unknown service -> empty, not an error.
		clusters, err = client.ResolveServiceClusters(ctx, tenantID, "payments", "nonexistent")
		if err != nil {
			t.Fatalf("resolve nonexistent: %v", err)
		}
		if len(clusters) != 0 {
			t.Errorf("nonexistent clusters: want empty, got %v", clusters)
		}

		// ListClusterServiceSelectors (ADR 0029) — one specific cluster's own
		// selectors, not merged across clusters: cluster-a and cluster-b both
		// have a "payment-api" service, but with different selectors.
		selectorsA, err := client.ListClusterServiceSelectors(ctx, tenantID, "cluster-a", "payments")
		if err != nil {
			t.Fatalf("list selectors cluster-a: %v", err)
		}
		if got := selectorsA["payment-api"]["app"]; got != "payment-api" {
			t.Errorf("cluster-a payment-api selector: want app=payment-api, got %v", selectorsA["payment-api"])
		}
		selectorsB, err := client.ListClusterServiceSelectors(ctx, tenantID, "cluster-b", "payments")
		if err != nil {
			t.Fatalf("list selectors cluster-b: %v", err)
		}
		if got := selectorsB["payment-api"]["app"]; got != "payment-api-b" {
			t.Errorf("cluster-b payment-api selector: want app=payment-api-b, got %v", selectorsB["payment-api"])
		}
		if _, ok := selectorsB["payment-worker"]; ok {
			t.Errorf("cluster-b should not have payment-worker (only cluster-a does), got %v", selectorsB)
		}

		// A second push to cluster-a fully replaces its prior service set —
		// payment-worker should disappear once cluster-a stops reporting it.
		if err := client.UpsertClusterServices(ctx, tenantID, "cluster-a", map[string][]ServiceEntry{
			"payments": {{Name: "payment-api", Selector: map[string]string{"app": "payment-api"}}},
		}); err != nil {
			t.Fatalf("re-upsert cluster-a: %v", err)
		}
		clusters, err = client.ResolveServiceClusters(ctx, tenantID, "payments", "payment-worker")
		if err != nil {
			t.Fatalf("resolve payment-worker after replace: %v", err)
		}
		if len(clusters) != 0 {
			t.Errorf("payment-worker should be gone after cluster-a's full replace, got %v", clusters)
		}
	})

	t.Run("cluster_ingress_endpoints: upsert + list by namespace, full replace on re-push", func(t *testing.T) {
		tenantID := uuid.New().String()

		if err := client.UpsertClusterIngress(ctx, tenantID, "cluster-a", []IngressEndpoint{
			{Namespace: "payments", Kind: "ingress", Name: "shop-ingress", Host: "shop.example.com", BackendService: "storefront"},
			{Namespace: "payments", Kind: "httproute", Name: "shop-route", Host: "shop.example.com", BackendService: "storefront"},
			{Namespace: "checkout", Kind: "route", Name: "checkout-route", Host: "checkout.apps.example.com", BackendService: "checkout-api"},
		}); err != nil {
			t.Fatalf("upsert cluster-a: %v", err)
		}

		entries, err := client.ListClusterIngress(ctx, tenantID, "payments")
		if err != nil {
			t.Fatalf("list payments: %v", err)
		}
		if len(entries) != 2 {
			t.Fatalf("payments entries: want 2, got %v", entries)
		}

		// Different namespace's entries don't leak into this listing.
		entries, err = client.ListClusterIngress(ctx, tenantID, "checkout")
		if err != nil {
			t.Fatalf("list checkout: %v", err)
		}
		if len(entries) != 1 || entries[0].Name != "checkout-route" {
			t.Errorf("checkout entries: want [checkout-route], got %v", entries)
		}

		// Unknown namespace -> empty, not an error.
		entries, err = client.ListClusterIngress(ctx, tenantID, "nonexistent")
		if err != nil {
			t.Fatalf("list nonexistent: %v", err)
		}
		if len(entries) != 0 {
			t.Errorf("nonexistent entries: want empty, got %v", entries)
		}

		// A second push to cluster-a fully replaces its prior entry set — the
		// httproute entry should disappear once cluster-a stops reporting it.
		if err := client.UpsertClusterIngress(ctx, tenantID, "cluster-a", []IngressEndpoint{
			{Namespace: "payments", Kind: "ingress", Name: "shop-ingress", Host: "shop.example.com", BackendService: "storefront"},
		}); err != nil {
			t.Fatalf("re-upsert cluster-a: %v", err)
		}
		entries, err = client.ListClusterIngress(ctx, tenantID, "payments")
		if err != nil {
			t.Fatalf("list payments after replace: %v", err)
		}
		if len(entries) != 1 || entries[0].Kind != "ingress" {
			t.Errorf("payments entries after replace: want just the ingress entry, got %v", entries)
		}
	})

	t.Run("ROADMAP P18 use case #4: two of one tenant's clusters' service_dependencies edges surface together, each tagged with its own cluster_id", func(t *testing.T) {
		// Verifies the ROADMAP's "no code change needed" claim for cross-
		// cluster dependency edges: ListServiceDependencies is scoped by
		// (tenant, namespace) only, never by cluster, so once more than one
		// of a tenant's clusters has pushed evidence for the same namespace,
		// both edges appear in the same query — this is what makes them
		// "just start appearing" in get_service_dependencies/DiagnoseSkill's
		// prefetch without any Python-side change.
		tenantID := uuid.New().String()

		if err := client.UpsertServiceDependency(ctx, uuid.New().String(), tenantID, "cluster-a", "payments", "checkout-ui", "checkout-api"); err != nil {
			t.Fatalf("upsert cluster-a dependency: %v", err)
		}
		// Same namespace, same from/to service *names* but a different
		// cluster — the realistic "downstream service lives in a different
		// cluster" scenario use case #4 names explicitly.
		if err := client.UpsertServiceDependency(ctx, uuid.New().String(), tenantID, "cluster-b", "payments", "checkout-ui", "checkout-api"); err != nil {
			t.Fatalf("upsert cluster-b dependency: %v", err)
		}

		deps, err := client.ListServiceDependencies(ctx, tenantID, "payments")
		if err != nil {
			t.Fatalf("list dependencies: %v", err)
		}
		if len(deps) != 2 {
			t.Fatalf("want both clusters' edges surfaced together, got %d: %v", len(deps), deps)
		}
		gotClusters := map[string]bool{deps[0].ClusterID: true, deps[1].ClusterID: true}
		if !gotClusters["cluster-a"] || !gotClusters["cluster-b"] {
			t.Errorf("want edges tagged cluster-a and cluster-b, got %v", gotClusters)
		}
	})

	t.Run("ROADMAP P18 use case #5: cluster_health_snapshots overwrites in place, fleet-wide listing surfaces every cluster", func(t *testing.T) {
		tenantID := uuid.New().String()

		if err := client.UpsertClusterHealthSnapshot(ctx, tenantID, "cluster-a", "v1.29.0", 10, 8); err != nil {
			t.Fatalf("upsert cluster-a: %v", err)
		}
		if err := client.UpsertClusterHealthSnapshot(ctx, tenantID, "cluster-b", "v1.30.0", 5, 5); err != nil {
			t.Fatalf("upsert cluster-b: %v", err)
		}

		snapshots, err := client.ListClusterHealthSnapshots(ctx, tenantID)
		if err != nil {
			t.Fatalf("list snapshots: %v", err)
		}
		if len(snapshots) != 2 {
			t.Fatalf("want both clusters' snapshots, got %d: %v", len(snapshots), snapshots)
		}

		// A second push to cluster-a overwrites its row in place — proves
		// this is a single-row upsert, not an accumulating history.
		if err := client.UpsertClusterHealthSnapshot(ctx, tenantID, "cluster-a", "v1.29.1", 12, 12); err != nil {
			t.Fatalf("re-upsert cluster-a: %v", err)
		}
		snapshots, err = client.ListClusterHealthSnapshots(ctx, tenantID)
		if err != nil {
			t.Fatalf("list snapshots after re-upsert: %v", err)
		}
		if len(snapshots) != 2 {
			t.Fatalf("re-upsert should overwrite, not add a row: want 2 snapshots, got %d", len(snapshots))
		}
		var clusterA *ClusterHealthSnapshot
		for i := range snapshots {
			if snapshots[i].ClusterID == "cluster-a" {
				clusterA = &snapshots[i]
			}
		}
		if clusterA == nil {
			t.Fatal("cluster-a snapshot missing")
		}
		if clusterA.K8sVersion != "v1.29.1" || clusterA.PodsTotal != 12 || clusterA.PodsReady != 12 {
			t.Errorf("cluster-a snapshot not overwritten: got %+v", clusterA)
		}
	})
}

// TestServiceDependencyTenantIsolation is the RLS test that actually
// matters (ADR 0022 phase 2): proves cross-tenant isolation is real,
// enforced by Postgres itself, not just that the tenant-aware code compiles
// and happens to pass the right WHERE clause.
//
// Deliberately runs the RLS-sensitive calls through a SECOND connection,
// authenticated as a freshly-created, ordinary (non-superuser, non-owner)
// role — not through `client`/startEmbedded's bootstrap "postgres" role.
// That bootstrap role is a genuine Postgres superuser, and superusers always
// bypass RLS regardless of ENABLE/FORCE ROW LEVEL SECURITY (documented
// Postgres behavior, not a policy bug) — testing through it would prove
// nothing about whether the policy itself works. This matters beyond the
// test too: if agentify's real production DB connection also happens to use
// a superuser-equivalent role, this RLS policy would be silently ineffective
// in production the same way — worth confirming separately, not assumed here.
func TestServiceDependencyTenantIsolation(t *testing.T) {
	client := startEmbedded(t)
	ctx := context.Background()

	if _, err := client.db.ExecContext(ctx, `
		DO $$
		BEGIN
			IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rls_test_app') THEN
				CREATE ROLE rls_test_app LOGIN PASSWORD 'rls_test_app' NOSUPERUSER;
			END IF;
		END $$;
	`); err != nil {
		t.Fatalf("create restricted test role: %v", err)
	}
	if _, err := client.db.ExecContext(ctx, `GRANT SELECT, INSERT, UPDATE ON service_dependencies TO rls_test_app`); err != nil {
		t.Fatalf("grant restricted test role: %v", err)
	}
	appDB, err := sql.Open("postgres", "host=localhost port=54329 user=rls_test_app password=rls_test_app dbname=agentify_test sslmode=disable")
	if err != nil {
		t.Fatalf("open restricted-role connection: %v", err)
	}
	defer appDB.Close()
	// appClient runs the RLS-sensitive calls (as the restricted role);
	// client (still the superuser connection) handles Integration setup,
	// which isn't RLS-sensitive and needs CREATE-level privileges anyway.
	appClient := &Client{db: appDB, logger: client.logger}

	tenantA := uuid.New().String()
	tenantB := uuid.New().String()

	clusterA := &Integration{ID: uuid.New().String(), Name: "cluster-a", Namespaces: []string{}, Status: "inactive", TenantID: tenantA}
	clusterB := &Integration{ID: uuid.New().String(), Name: "cluster-b", Namespaces: []string{}, Status: "inactive", TenantID: tenantB}
	// CreateIntegration doesn't write tenant_id itself (phase 1's deliberate
	// scope) — set it directly after creation so this test controls it, not
	// relying on the DB default (which would put both under the same tenant).
	if err := client.CreateIntegration(ctx, clusterA); err != nil {
		t.Fatalf("create cluster A: %v", err)
	}
	if err := client.CreateIntegration(ctx, clusterB); err != nil {
		t.Fatalf("create cluster B: %v", err)
	}
	if _, err := client.db.ExecContext(ctx, `UPDATE integrations SET tenant_id = $1 WHERE id = $2`, tenantA, clusterA.ID); err != nil {
		t.Fatalf("set tenant A: %v", err)
	}
	if _, err := client.db.ExecContext(ctx, `UPDATE integrations SET tenant_id = $1 WHERE id = $2`, tenantB, clusterB.ID); err != nil {
		t.Fatalf("set tenant B: %v", err)
	}

	// Same namespace/from/to on purpose — this is exactly the case that
	// would silently collide under the OLD (namespace, from_service,
	// to_service) unique constraint, merging two tenants' evidence into
	// one row.
	if err := appClient.UpsertServiceDependency(ctx, uuid.New().String(), tenantA, clusterA.ID, "payments", "payment-ui", "payment-backend"); err != nil {
		t.Fatalf("upsert tenant A dependency: %v", err)
	}
	if err := appClient.UpsertServiceDependency(ctx, uuid.New().String(), tenantB, clusterB.ID, "payments", "payment-ui", "payment-backend"); err != nil {
		t.Fatalf("upsert tenant B dependency: %v", err)
	}
	// Evidence again for tenant A only — proves ON CONFLICT is scoped per
	// tenant (increments A's row), not global (which would also bump B's).
	if err := appClient.UpsertServiceDependency(ctx, uuid.New().String(), tenantA, clusterA.ID, "payments", "payment-ui", "payment-backend"); err != nil {
		t.Fatalf("re-upsert tenant A dependency: %v", err)
	}

	depsA, err := appClient.ListServiceDependencies(ctx, tenantA, "payments")
	if err != nil {
		t.Fatalf("list tenant A dependencies: %v", err)
	}
	if len(depsA) != 1 {
		t.Fatalf("tenant A: want exactly 1 edge (RLS should hide tenant B's row), got %d", len(depsA))
	}
	if depsA[0].EvidenceCount != 2 {
		t.Errorf("tenant A evidence_count: want 2 (two upserts), got %d — ON CONFLICT may not be tenant-scoped", depsA[0].EvidenceCount)
	}
	if depsA[0].TenantID != tenantA || depsA[0].ClusterID != clusterA.ID {
		t.Errorf("tenant A row: want tenant=%q cluster=%q, got tenant=%q cluster=%q", tenantA, clusterA.ID, depsA[0].TenantID, depsA[0].ClusterID)
	}

	depsB, err := appClient.ListServiceDependencies(ctx, tenantB, "payments")
	if err != nil {
		t.Fatalf("list tenant B dependencies: %v", err)
	}
	if len(depsB) != 1 {
		t.Fatalf("tenant B: want exactly 1 edge (RLS should hide tenant A's row), got %d", len(depsB))
	}
	if depsB[0].EvidenceCount != 1 {
		t.Errorf("tenant B evidence_count: want 1 (untouched by tenant A's re-upsert), got %d", depsB[0].EvidenceCount)
	}
}

func TestEventsWindowedQuery(t *testing.T) {
	client := startEmbedded(t)
	ctx := context.Background()
	pod := "k8fy.metrics"

	// A rising restart series for pod-x, plus one unrelated sample for pod-y.
	samples := []struct {
		ts       string
		podID    string
		restarts float64
	}{
		{"2026-06-05T14:00:00Z", "pod-x", 0},
		{"2026-06-05T14:10:00Z", "pod-x", 3},
		{"2026-06-05T14:20:00Z", "pod-x", 11},
		{"2026-06-05T14:30:00Z", "pod-x", 17},
		{"2026-06-05T14:15:00Z", "pod-y", 0},
	}
	for _, s := range samples {
		if _, err := client.Store(ctx, pod, map[string]interface{}{
			"id":              uuid.New().String(),
			"event_namespace": "k8fy.metrics",
			"type":            "pod_metrics",
			"timestamp":       s.ts,
			"payload":         map[string]interface{}{"pod_id": s.podID, "restarts": s.restarts},
		}); err != nil {
			t.Fatalf("store sample: %v", err)
		}
	}

	// Window 14:05–14:25, entity pod-x, chronological: expect the 14:10 and 14:20
	// samples only (excludes 14:00 boundary-before, 14:30 after, and pod-y).
	rows, err := client.Query(ctx, pod, map[string]interface{}{
		"since":  "2026-06-05T14:05:00Z",
		"until":  "2026-06-05T14:25:00Z",
		"entity": "pod-x",
		"order":  "asc",
	})
	if err != nil {
		t.Fatalf("windowed query: %v", err)
	}
	if len(rows) != 2 {
		t.Fatalf("want 2 windowed samples, got %d", len(rows))
	}
	// Chronological order + entity filter held.
	first := rows[0]["payload"].(map[string]interface{})
	second := rows[1]["payload"].(map[string]interface{})
	if first["restarts"] != float64(3) || second["restarts"] != float64(11) {
		t.Errorf("asc order/filter wrong: got %v then %v", first["restarts"], second["restarts"])
	}

	// Entity filter alone for pod-x: all 4 samples, recent-first by default.
	all, err := client.Query(ctx, pod, map[string]interface{}{"entity": "pod-x"})
	if err != nil || len(all) != 4 {
		t.Fatalf("entity query: err=%v rows=%d (want 4)", err, len(all))
	}
	if newest := all[0]["payload"].(map[string]interface{}); newest["restarts"] != float64(17) {
		t.Errorf("default order should be recent-first; got newest restarts=%v", newest["restarts"])
	}

	// limit clamps result count.
	lim, err := client.Query(ctx, pod, map[string]interface{}{"entity": "pod-x", "limit": float64(2)})
	if err != nil || len(lim) != 2 {
		t.Fatalf("limit query: err=%v rows=%d (want 2)", err, len(lim))
	}
}

func TestPurgeOlderThan(t *testing.T) {
	client := startEmbedded(t)
	ctx := context.Background()
	pod := "k8fy.metrics"

	// Use time.Now()-based timestamps so the test stays valid regardless of
	// when it runs. The per-pod retention window for k8fy.metrics is 7 days,
	// so "recent" must be within the last 7 days.
	old1 := time.Now().Add(-60 * 24 * time.Hour).UTC().Format(time.RFC3339)
	old2 := time.Now().Add(-30 * 24 * time.Hour).UTC().Format(time.RFC3339)
	recent := time.Now().Add(-1 * 24 * time.Hour).UTC().Format(time.RFC3339)
	cutoff := time.Now().Add(-8 * 24 * time.Hour) // 8 days ago — between old and recent

	store := func(ts string) {
		if _, err := client.Store(ctx, pod, map[string]interface{}{
			"id":              uuid.New().String(),
			"event_namespace": "k8fy.metrics",
			"type":            "pod_metrics",
			"timestamp":       ts,
			"payload":         map[string]interface{}{"pod_id": "p", "restarts": float64(1)},
		}); err != nil {
			t.Fatalf("store: %v", err)
		}
	}
	store(old1)   // 60 days ago — deleted by per-pod 7-day window
	store(old2)   // 30 days ago — deleted by per-pod 7-day window
	store(recent) // yesterday  — kept (within 7-day window)

	n, err := client.PurgeOlderThan(ctx, cutoff)
	if err != nil {
		t.Fatalf("purge: %v", err)
	}
	if n != 2 {
		t.Fatalf("purged %d rows, want 2", n)
	}
	rows, err := client.Query(ctx, pod, nil)
	if err != nil || len(rows) != 1 {
		t.Fatalf("after purge: err=%v rows=%d (want 1)", err, len(rows))
	}
}

// TestRemediationProposals covers the propose→approve/reject lifecycle (ADR
// 0020): create, list/filter by status, and — most importantly — that the
// decide step is idempotent under the WHERE status='pending' guard so a
// duplicate approve/reject (double click, webhook retry) never re-decides or
// re-executes an already-decided proposal.
func TestRemediationProposals(t *testing.T) {
	client := startEmbedded(t)
	ctx := context.Background()

	p := &RemediationProposal{
		ID:             uuid.New().String(),
		TraceID:        "trace-1",
		UseCase:        "incident_responder",
		Namespace:      "payments",
		Service:        "payment-worker",
		ProposedAction: "restart_deployment",
		ActionParams:   map[string]interface{}{"deployment": "payment-worker"},
		Analysis:       map[string]interface{}{"reasoning": "OOMKilled 3x", "confidence": 0.8},
		ExpiresAt:      time.Now().Add(30 * time.Minute),
	}
	if err := client.CreateRemediationProposal(ctx, p); err != nil {
		t.Fatalf("create: %v", err)
	}

	t.Run("get round-trips fields", func(t *testing.T) {
		got, err := client.GetRemediationProposal(ctx, p.ID)
		if err != nil {
			t.Fatalf("get: %v", err)
		}
		if got.Status != "pending" {
			t.Errorf("want status=pending, got %q", got.Status)
		}
		if got.ActionParams["deployment"] != "payment-worker" {
			t.Errorf("action_params not round-tripped: %v", got.ActionParams)
		}
		if got.Analysis["reasoning"] != "OOMKilled 3x" {
			t.Errorf("analysis not round-tripped: %v", got.Analysis)
		}
	})

	t.Run("list filters by status", func(t *testing.T) {
		pending, err := client.ListRemediationProposals(ctx, "pending", 100)
		if err != nil || len(pending) != 1 {
			t.Fatalf("list pending: err=%v rows=%d", err, len(pending))
		}
		approved, err := client.ListRemediationProposals(ctx, "approved", 100)
		if err != nil || len(approved) != 0 {
			t.Fatalf("list approved: err=%v rows=%d (want 0)", err, len(approved))
		}
	})

	t.Run("decide is idempotent — second decision is a no-op", func(t *testing.T) {
		ok, err := client.DecideRemediationProposal(ctx, p.ID, "approved", "test-actor")
		if err != nil || !ok {
			t.Fatalf("first decide: ok=%v err=%v (want ok=true)", ok, err)
		}
		ok2, err := client.DecideRemediationProposal(ctx, p.ID, "rejected", "someone-else")
		if err != nil {
			t.Fatalf("second decide errored: %v", err)
		}
		if ok2 {
			t.Fatal("second decide should be a no-op (ok=false) — proposal was already decided")
		}
		got, err := client.GetRemediationProposal(ctx, p.ID)
		if err != nil {
			t.Fatalf("get after decide: %v", err)
		}
		if got.Status != "approved" {
			t.Errorf("status should remain 'approved' from the first decision, got %q", got.Status)
		}
		if got.DecidedBy != "test-actor" {
			t.Errorf("decided_by should remain from the first decision, got %q", got.DecidedBy)
		}
	})

	t.Run("complete records execution outcome", func(t *testing.T) {
		if err := client.CompleteRemediationProposal(ctx, p.ID, "executed",
			map[string]interface{}{"status": "restarted"}, ""); err != nil {
			t.Fatalf("complete: %v", err)
		}
		got, err := client.GetRemediationProposal(ctx, p.ID)
		if err != nil {
			t.Fatalf("get after complete: %v", err)
		}
		if got.Status != "executed" {
			t.Errorf("want status=executed, got %q", got.Status)
		}
		if got.Result["status"] != "restarted" {
			t.Errorf("result not round-tripped: %v", got.Result)
		}
	})

	t.Run("ProposalExistsForEvent dedupes DeploymentGuardian's sweep", func(t *testing.T) {
		eventID := uuid.New().String()
		exists, err := client.ProposalExistsForEvent(ctx, eventID)
		if err != nil || exists {
			t.Fatalf("expected no proposal yet: exists=%v err=%v", exists, err)
		}
		dgp := &RemediationProposal{
			ID: uuid.New().String(), UseCase: "deployment_guardian",
			Namespace: "payments", Service: "payment-api", ProposedAction: "rollback_deployment",
			SourceEventID: eventID, ExpiresAt: time.Now().Add(30 * time.Minute),
		}
		if err := client.CreateRemediationProposal(ctx, dgp); err != nil {
			t.Fatalf("create: %v", err)
		}
		exists, err = client.ProposalExistsForEvent(ctx, eventID)
		if err != nil || !exists {
			t.Fatalf("expected proposal to exist: exists=%v err=%v", exists, err)
		}
	})

}

// TestIncidentEmbeddings covers the keyword-fallback path of P8's semantic
// memory layer. embedded-postgres (used here) doesn't ship pgvector, so the
// `embedding` column is never added by initSchema (see the DO $$ ...
// EXCEPTION block) — exactly the "pgvector unavailable" condition
// FindSimilarIncidents is documented to fall back from. The vector-search
// path (queryVec non-empty, embedding <-> ordering) needs a real Postgres
// with pgvector installed and isn't exercised by this suite.
func TestIncidentEmbeddings(t *testing.T) {
	client := startEmbedded(t)
	ctx := context.Background()

	// incident_embeddings.trace_id REFERENCES traces(id) — seed one first.
	seedTrace := func(id string) {
		t.Helper()
		if err := client.InsertTrace(ctx, TraceRecord{
			ID: id, TraceID: id, Question: "why is it crashing", Intent: "diagnose",
			Namespace: "payments", Tier: "tier2", Status: "answered", Confidence: 0.8,
		}); err != nil {
			t.Fatalf("seed trace %s: %v", id, err)
		}
	}

	t.Run("insert without embedding, then upsert updates the summary in place", func(t *testing.T) {
		seedTrace("trace-a")
		e := IncidentEmbedding{
			ID: uuid.New().String(), TraceID: "trace-a",
			Namespace: "payments", Service: "payment-worker",
			Summary: "OOMKilled, restarted",
		}
		if err := client.InsertIncidentEmbedding(ctx, e); err != nil {
			t.Fatalf("insert: %v", err)
		}
		e.Summary = "OOMKilled, restarted — root cause: memory leak in v1.4"
		if err := client.InsertIncidentEmbedding(ctx, e); err != nil {
			t.Fatalf("upsert: %v", err)
		}

		found, err := client.FindSimilarIncidents(ctx, "payments", "payment-worker", nil, 10)
		if err != nil {
			t.Fatalf("find: %v", err)
		}
		var got *IncidentEmbedding
		for i := range found {
			if found[i].ID == e.ID {
				got = &found[i]
			}
		}
		if got == nil {
			t.Fatal("upserted row not found via FindSimilarIncidents fallback")
		}
		if got.Summary != e.Summary {
			t.Errorf("upsert did not update summary in place: got %q", got.Summary)
		}
		if got.TenantID == "" {
			t.Error("tenant_id should default to the single-tenant placeholder, got empty")
		}
	})

	t.Run("fallback search scopes by namespace+service and orders most-recent-first", func(t *testing.T) {
		seedTrace("trace-b")
		seedTrace("trace-c")
		seedTrace("trace-d")

		older := IncidentEmbedding{ID: uuid.New().String(), TraceID: "trace-b", Namespace: "checkout", Service: "checkout-api", Summary: "older incident"}
		if err := client.InsertIncidentEmbedding(ctx, older); err != nil {
			t.Fatalf("insert older: %v", err)
		}
		time.Sleep(10 * time.Millisecond) // force a distinct created_at for ordering
		newer := IncidentEmbedding{ID: uuid.New().String(), TraceID: "trace-c", Namespace: "checkout", Service: "checkout-api", Summary: "newer incident"}
		if err := client.InsertIncidentEmbedding(ctx, newer); err != nil {
			t.Fatalf("insert newer: %v", err)
		}
		other := IncidentEmbedding{ID: uuid.New().String(), TraceID: "trace-d", Namespace: "checkout", Service: "other-service", Summary: "different service, must not match"}
		if err := client.InsertIncidentEmbedding(ctx, other); err != nil {
			t.Fatalf("insert other-service: %v", err)
		}

		found, err := client.FindSimilarIncidents(ctx, "checkout", "checkout-api", nil, 10)
		if err != nil {
			t.Fatalf("find: %v", err)
		}
		if len(found) != 2 {
			t.Fatalf("want 2 rows scoped to checkout/checkout-api, got %d", len(found))
		}
		if found[0].ID != newer.ID {
			t.Errorf("want most-recent-first ordering, got %q first", found[0].Summary)
		}

		t.Run("limit caps the result count", func(t *testing.T) {
			capped, err := client.FindSimilarIncidents(ctx, "checkout", "checkout-api", nil, 1)
			if err != nil {
				t.Fatalf("find: %v", err)
			}
			if len(capped) != 1 {
				t.Fatalf("want 1 row (limit=1), got %d", len(capped))
			}
		})

		t.Run("limit<=0 defaults to 3", func(t *testing.T) {
			defaulted, err := client.FindSimilarIncidents(ctx, "checkout", "checkout-api", nil, 0)
			if err != nil {
				t.Fatalf("find: %v", err)
			}
			if len(defaulted) != 2 { // only 2 rows exist in scope — default cap of 3 doesn't truncate them
				t.Fatalf("want 2 rows (below the default limit of 3), got %d", len(defaulted))
			}
		})
	})

	t.Run("empty namespace/service filters match everything", func(t *testing.T) {
		found, err := client.FindSimilarIncidents(ctx, "", "", nil, 100)
		if err != nil {
			t.Fatalf("find: %v", err)
		}
		if len(found) < 3 { // at least the 3 rows inserted by the earlier subtests
			t.Fatalf("want at least 3 rows across all namespaces/services, got %d", len(found))
		}
	})
}

func mustTime(t *testing.T, s string) time.Time {
	t.Helper()
	ts, err := time.Parse(time.RFC3339, s)
	if err != nil {
		t.Fatalf("parse time: %v", err)
	}
	return ts
}

func mustStore(t *testing.T, cs *CurrentState, pod, entity string, payload map[string]interface{}) {
	t.Helper()
	_, err := cs.Store(context.Background(), pod, map[string]interface{}{
		"entity_key":      entity,
		"event_namespace": "k8fy.live-state",
		"type":            "pod_modified",
		"source":          "kubernetes-api",
		"payload":         payload,
	})
	if err != nil {
		t.Fatalf("store %s: %v", entity, err)
	}
}

// TestTracePromptProvenance covers the prompt_name / prompt_version columns
// added for the Evaluator Agent's evidence trail (ROADMAP P19 gap C), and with
// them the previously untested scanTrace path.
//
// The nil case is the one that matters: prompt_version is NULL for Tier-1
// answers (no LLM call) and whenever the agent fell back to its local prompt
// string, and it must read back as a nil *int rather than 0 — "no version" and
// "version 0" mean different things to a proposal that cites a trace as
// evidence.
func TestTracePromptProvenance(t *testing.T) {
	client := startEmbedded(t)
	ctx := context.Background()

	insert := func(id, promptName string, promptVersion *int) {
		t.Helper()
		if err := client.InsertTrace(ctx, TraceRecord{
			ID: id, TraceID: id, Question: "is payment-worker healthy?",
			Intent: "health_check", Namespace: "payments", Tier: "tier2",
			Status: "healthy", Confidence: 0.9,
			PromptName: promptName, PromptVersion: promptVersion,
		}); err != nil {
			t.Fatalf("insert %s: %v", id, err)
		}
	}

	t.Run("session_id round-trips and groups a conversation's turns", func(t *testing.T) {
		// Conversation-level evaluation (ROADMAP P19 gap F) needs the turns of one
		// conversation to be identifiable. Before session_id existed, evidence
		// (this table) and conversation text (chat_sessions.messages) could not be
		// joined, so "what context should have been available earlier" — which is
		// inherently multi-turn — was not expressible.
		for i, id := range []string{"conv-turn-1", "conv-turn-2"} {
			v := i + 1
			if err := client.InsertTrace(ctx, TraceRecord{
				ID: id, TraceID: id, Question: "why is it crashing", Intent: "chat",
				Namespace: "payments", Tier: "tier2", Status: "ok", Confidence: 0.7,
				PromptName: "k8fy/chat", PromptVersion: &v, SessionID: "sess-abc",
			}); err != nil {
				t.Fatalf("insert %s: %v", id, err)
			}
		}
		// A single-shot query must NOT be attributed to any conversation.
		insert("one-shot", "k8fy/health-check", nil)

		got, err := client.GetTrace(ctx, "conv-turn-2")
		if err != nil {
			t.Fatalf("GetTrace: %v", err)
		}
		if got.SessionID != "sess-abc" {
			t.Errorf("SessionID = %q, want sess-abc", got.SessionID)
		}

		traces, err := client.ListTraces(ctx, 100)
		if err != nil {
			t.Fatalf("ListTraces: %v", err)
		}
		var inConv, oneShot int
		for _, tr := range traces {
			switch tr.SessionID {
			case "sess-abc":
				inConv++
			case "":
				oneShot++
			}
		}
		if inConv != 2 {
			t.Errorf("turns grouped under sess-abc = %d, want 2", inConv)
		}
		if oneShot == 0 {
			t.Error("expected at least one trace with an empty SessionID (single-shot query)")
		}
	})

	t.Run("a Langfuse-served prompt round-trips its name and version", func(t *testing.T) {
		v := 7
		insert("trace-versioned", "k8fy/health-check", &v)

		got, err := client.GetTrace(ctx, "trace-versioned")
		if err != nil {
			t.Fatalf("GetTrace: %v", err)
		}
		if got.PromptName != "k8fy/health-check" {
			t.Errorf("PromptName = %q, want k8fy/health-check", got.PromptName)
		}
		if got.PromptVersion == nil {
			t.Fatal("PromptVersion = nil, want 7")
		}
		if *got.PromptVersion != 7 {
			t.Errorf("PromptVersion = %d, want 7", *got.PromptVersion)
		}
	})

	t.Run("a fallback or Tier-1 answer round-trips a NULL version as nil", func(t *testing.T) {
		insert("trace-fallback", "", nil)

		got, err := client.GetTrace(ctx, "trace-fallback")
		if err != nil {
			t.Fatalf("GetTrace: %v", err)
		}
		if got.PromptName != "" {
			t.Errorf("PromptName = %q, want empty", got.PromptName)
		}
		if got.PromptVersion != nil {
			t.Errorf("PromptVersion = %d, want nil", *got.PromptVersion)
		}
	})

	t.Run("ListTraces carries provenance too", func(t *testing.T) {
		found := map[string]*TraceRecord{}
		traces, err := client.ListTraces(ctx, 100)
		if err != nil {
			t.Fatalf("ListTraces: %v", err)
		}
		for i := range traces {
			found[traces[i].ID] = &traces[i]
		}
		versioned, ok := found["trace-versioned"]
		if !ok {
			t.Fatal("trace-versioned missing from ListTraces")
		}
		if versioned.PromptVersion == nil || *versioned.PromptVersion != 7 {
			t.Errorf("listed PromptVersion = %v, want 7", versioned.PromptVersion)
		}
		fallback, ok := found["trace-fallback"]
		if !ok {
			t.Fatal("trace-fallback missing from ListTraces")
		}
		if fallback.PromptVersion != nil {
			t.Errorf("listed PromptVersion = %d, want nil", *fallback.PromptVersion)
		}
	})
}
