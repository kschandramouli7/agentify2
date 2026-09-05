package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// fakeClusterHealthStore implements ClusterHealthStore, recording the last
// UpsertClusterHealthSnapshot call and serving ListClusterHealthSnapshots
// from a canned slice.
type fakeClusterHealthStore struct {
	lastTenantID   string
	lastClusterID  string
	lastK8sVersion string
	lastPodsTotal  int
	lastPodsReady  int
	snapshots      []pgstore.ClusterHealthSnapshot
	listErr        error

	// Per-service health (ROADMAP P22).
	serviceHealth    map[string][]pgstore.ServiceHealth // namespace -> rows
	serviceHealthErr error
}

func (f *fakeClusterHealthStore) ListServiceHealth(ctx context.Context, tenantID, namespace string) ([]pgstore.ServiceHealth, error) {
	if f.serviceHealthErr != nil {
		return nil, f.serviceHealthErr
	}
	return f.serviceHealth[namespace], nil
}

func (f *fakeClusterHealthStore) UpsertClusterHealthSnapshot(ctx context.Context, tenantID, clusterID, k8sVersion string, podsTotal, podsReady int) error {
	f.lastTenantID = tenantID
	f.lastClusterID = clusterID
	f.lastK8sVersion = k8sVersion
	f.lastPodsTotal = podsTotal
	f.lastPodsReady = podsReady
	return nil
}

func (f *fakeClusterHealthStore) ListClusterHealthSnapshots(ctx context.Context, tenantID string) ([]pgstore.ClusterHealthSnapshot, error) {
	if f.listErr != nil {
		return nil, f.listErr
	}
	return f.snapshots, nil
}

// TestHandleClusterHealthUpsert exercises the fleet collector's health/
// version snapshot push (ROADMAP P18 use case #5) — same auth shape as
// HandleClusterIngressUpsert: an absent or unrecognized credential is
// always rejected since there's no cluster identity to attach a snapshot to
// otherwise.
func TestHandleClusterHealthUpsert(t *testing.T) {
	t.Run("no Authorization header is rejected — no cluster to attach a snapshot to", func(t *testing.T) {
		h := &Handler{integrationStore: &fakeIntegrationStore{}, clusterHealthStore: &fakeClusterHealthStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/cluster-health", strings.NewReader(`{}`))
		w := httptest.NewRecorder()

		h.HandleClusterHealthUpsert(w, req)

		if w.Code != http.StatusUnauthorized {
			t.Fatalf("status: want %d, got %d", http.StatusUnauthorized, w.Code)
		}
	})

	t.Run("unrecognized bearer token is rejected", func(t *testing.T) {
		h := &Handler{integrationStore: &fakeIntegrationStore{}, clusterHealthStore: &fakeClusterHealthStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/cluster-health", strings.NewReader(`{}`))
		req.Header.Set("Authorization", "Bearer nonexistent-token")
		w := httptest.NewRecorder()

		h.HandleClusterHealthUpsert(w, req)

		if w.Code != http.StatusUnauthorized {
			t.Fatalf("status: want %d, got %d", http.StatusUnauthorized, w.Code)
		}
	})

	t.Run("cluster health store not configured returns 503", func(t *testing.T) {
		h := &Handler{integrationStore: &fakeIntegrationStore{
			byToken: map[string]*pgstore.Integration{"real-token": {ID: "cluster-42", TenantID: "tenant-a"}},
		}}
		req := httptest.NewRequest(http.MethodPost, "/api/cluster-health", strings.NewReader(`{}`))
		req.Header.Set("Authorization", "Bearer real-token")
		w := httptest.NewRecorder()

		h.HandleClusterHealthUpsert(w, req)

		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("status: want %d, got %d", http.StatusServiceUnavailable, w.Code)
		}
	})

	t.Run("valid collector token upserts the snapshot", func(t *testing.T) {
		integStore := &fakeIntegrationStore{
			byToken: map[string]*pgstore.Integration{"real-token": {ID: "cluster-42", TenantID: "tenant-a"}},
		}
		chStore := &fakeClusterHealthStore{}
		h := &Handler{integrationStore: integStore, clusterHealthStore: chStore}
		body := `{"k8s_version":"v1.30.0","pods_total":10,"pods_ready":8}`
		req := httptest.NewRequest(http.MethodPost, "/api/cluster-health", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer real-token")
		w := httptest.NewRecorder()

		h.HandleClusterHealthUpsert(w, req)

		if w.Code != http.StatusNoContent {
			t.Fatalf("status: want %d, got %d", http.StatusNoContent, w.Code)
		}
		if chStore.lastTenantID != "tenant-a" || chStore.lastClusterID != "cluster-42" {
			t.Errorf("UpsertClusterHealthSnapshot called with wrong tenant/cluster: %q/%q", chStore.lastTenantID, chStore.lastClusterID)
		}
		if chStore.lastK8sVersion != "v1.30.0" || chStore.lastPodsTotal != 10 || chStore.lastPodsReady != 8 {
			t.Errorf("UpsertClusterHealthSnapshot values: got version=%q total=%d ready=%d", chStore.lastK8sVersion, chStore.lastPodsTotal, chStore.lastPodsReady)
		}
	})

	t.Run("malformed JSON is rejected", func(t *testing.T) {
		h := &Handler{integrationStore: &fakeIntegrationStore{
			byToken: map[string]*pgstore.Integration{"real-token": {ID: "cluster-42", TenantID: "tenant-a"}},
		}, clusterHealthStore: &fakeClusterHealthStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/cluster-health", strings.NewReader(`not json`))
		req.Header.Set("Authorization", "Bearer real-token")
		w := httptest.NewRecorder()

		h.HandleClusterHealthUpsert(w, req)

		if w.Code != http.StatusBadRequest {
			t.Fatalf("status: want %d, got %d", http.StatusBadRequest, w.Code)
		}
	})

	t.Run("wrong HTTP method is rejected", func(t *testing.T) {
		h := &Handler{clusterHealthStore: &fakeClusterHealthStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cluster-health", nil)
		w := httptest.NewRecorder()

		h.HandleClusterHealthUpsert(w, req)

		if w.Code != http.StatusMethodNotAllowed {
			t.Fatalf("status: want %d, got %d", http.StatusMethodNotAllowed, w.Code)
		}
	})
}

// TestHandleClusterHealthList exercises the read side (store-only surface —
// no agent tool or frontend fleet dashboard consumes this yet).
func TestHandleClusterHealthList(t *testing.T) {
	t.Run("store not configured returns empty list, not an error", func(t *testing.T) {
		h := &Handler{}
		req := httptest.NewRequest(http.MethodGet, "/api/cluster-health", nil)
		w := httptest.NewRecorder()

		h.HandleClusterHealthList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if !strings.Contains(w.Body.String(), `"snapshots":[]`) {
			t.Errorf("body: want empty snapshots, got %s", w.Body.String())
		}
	})

	t.Run("returns every cluster's snapshot for the tenant", func(t *testing.T) {
		chStore := &fakeClusterHealthStore{snapshots: []pgstore.ClusterHealthSnapshot{
			{ClusterID: "cluster-a", K8sVersion: "v1.29.0", PodsTotal: 10, PodsReady: 8},
			{ClusterID: "cluster-b", K8sVersion: "v1.30.0", PodsTotal: 5, PodsReady: 5},
		}}
		h := &Handler{clusterHealthStore: chStore}
		req := httptest.NewRequest(http.MethodGet, "/api/cluster-health", nil)
		w := httptest.NewRecorder()

		h.HandleClusterHealthList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if !strings.Contains(w.Body.String(), "cluster-a") || !strings.Contains(w.Body.String(), "cluster-b") {
			t.Errorf("body: want both clusters, got %s", w.Body.String())
		}
	})
}
