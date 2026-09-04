package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// fakeClusterServiceStore implements ClusterServiceStore, recording the last
// UpsertClusterServices call and serving ResolveServiceClusters from a
// canned map keyed by "namespace/service".
type fakeClusterServiceStore struct {
	lastTenantID  string
	lastClusterID string
	lastUpsert    map[string][]pgstore.ServiceEntry
	resolved      map[string][]string                     // "namespace/service" -> cluster ids
	selectors     map[string]map[string]map[string]string // "cluster_id/namespace" -> service -> selector
	profiles      map[string][]pgstore.ServiceProfile     // namespace -> profiles (ROADMAP P22)
	profileErr    error
}

func (f *fakeClusterServiceStore) ListServiceProfiles(ctx context.Context, tenantID, namespace string) ([]pgstore.ServiceProfile, error) {
	if f.profileErr != nil {
		return nil, f.profileErr
	}
	return f.profiles[namespace], nil
}

func (f *fakeClusterServiceStore) UpsertClusterServices(ctx context.Context, tenantID, clusterID string, byNamespace map[string][]pgstore.ServiceEntry) error {
	f.lastTenantID = tenantID
	f.lastClusterID = clusterID
	f.lastUpsert = byNamespace
	return nil
}

func (f *fakeClusterServiceStore) ResolveServiceClusters(ctx context.Context, tenantID, namespace, service string) ([]string, error) {
	if f.resolved == nil {
		return []string{}, nil
	}
	return f.resolved[namespace+"/"+service], nil
}

func (f *fakeClusterServiceStore) ListClusterServices(ctx context.Context, tenantID string) (map[string][]string, error) {
	if f.lastUpsert == nil {
		return map[string][]string{}, nil
	}
	names := map[string][]string{}
	for ns, entries := range f.lastUpsert {
		for _, e := range entries {
			names[ns] = append(names[ns], e.Name)
		}
	}
	return names, nil
}

func (f *fakeClusterServiceStore) ListClusterServiceSelectors(ctx context.Context, tenantID, clusterID, namespace string) (map[string]map[string]string, error) {
	if f.selectors == nil {
		return map[string]map[string]string{}, nil
	}
	return f.selectors[clusterID+"/"+namespace], nil
}

// TestHandleClusterInventoryUpsert exercises the fleet collector's inventory
// push (ADR 0022 / ROADMAP P18 use case #1, extended by ROADMAP P16 / ADR
// 0023 to also carry per-namespace service names) — unlike
// HandleServiceDependencyUpsert, an absent or unrecognized credential is
// always rejected here since there's no Integration row to attach namespaces
// to without one.
func TestHandleClusterInventoryUpsert(t *testing.T) {
	t.Run("no Authorization header is rejected — no cluster to attach namespaces to", func(t *testing.T) {
		h := &Handler{integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/cluster-inventory", strings.NewReader(`{"namespaces":[{"name":"payments","services":[]}]}`))
		w := httptest.NewRecorder()

		h.HandleClusterInventoryUpsert(w, req)

		if w.Code != http.StatusUnauthorized {
			t.Fatalf("status: want %d, got %d", http.StatusUnauthorized, w.Code)
		}
	})

	t.Run("unrecognized bearer token is rejected", func(t *testing.T) {
		h := &Handler{integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/cluster-inventory", strings.NewReader(`{"namespaces":[{"name":"payments","services":[]}]}`))
		req.Header.Set("Authorization", "Bearer nonexistent-token")
		w := httptest.NewRecorder()

		h.HandleClusterInventoryUpsert(w, req)

		if w.Code != http.StatusUnauthorized {
			t.Fatalf("status: want %d, got %d", http.StatusUnauthorized, w.Code)
		}
	})

	t.Run("valid collector token updates Integration namespaces and cluster_services", func(t *testing.T) {
		integStore := &fakeIntegrationStore{
			byToken: map[string]*pgstore.Integration{
				"real-token": {ID: "cluster-42", TenantID: "tenant-a"},
			},
		}
		csStore := &fakeClusterServiceStore{}
		h := &Handler{integrationStore: integStore, clusterServiceStore: csStore}
		body := `{"namespaces":[{"name":"payments","services":["payment-api","payment-worker"]},{"name":"checkout","services":["checkout-api"]}]}`
		req := httptest.NewRequest(http.MethodPost, "/api/cluster-inventory", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer real-token")
		w := httptest.NewRecorder()

		h.HandleClusterInventoryUpsert(w, req)

		if w.Code != http.StatusNoContent {
			t.Fatalf("status: want %d, got %d", http.StatusNoContent, w.Code)
		}
		gotNS := integStore.updatedNamespaces["cluster-42"]
		if len(gotNS) != 2 || gotNS[0] != "payments" || gotNS[1] != "checkout" {
			t.Errorf("updatedNamespaces[cluster-42]: want [payments checkout], got %v", gotNS)
		}
		if csStore.lastTenantID != "tenant-a" || csStore.lastClusterID != "cluster-42" {
			t.Errorf("UpsertClusterServices called with wrong tenant/cluster: %q/%q", csStore.lastTenantID, csStore.lastClusterID)
		}
		if len(csStore.lastUpsert["payments"]) != 2 || len(csStore.lastUpsert["checkout"]) != 1 {
			t.Errorf("UpsertClusterServices byNamespace: got %v", csStore.lastUpsert)
		}
	})

	t.Run("malformed JSON is rejected", func(t *testing.T) {
		h := &Handler{integrationStore: &fakeIntegrationStore{
			byToken: map[string]*pgstore.Integration{
				"real-token": {ID: "cluster-42", TenantID: "tenant-a"},
			},
		}}
		req := httptest.NewRequest(http.MethodPost, "/api/cluster-inventory", strings.NewReader(`not json`))
		req.Header.Set("Authorization", "Bearer real-token")
		w := httptest.NewRecorder()

		h.HandleClusterInventoryUpsert(w, req)

		if w.Code != http.StatusBadRequest {
			t.Fatalf("status: want %d, got %d", http.StatusBadRequest, w.Code)
		}
	})

	t.Run("wrong HTTP method is rejected", func(t *testing.T) {
		h := &Handler{integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cluster-inventory", nil)
		w := httptest.NewRecorder()

		h.HandleClusterInventoryUpsert(w, req)

		if w.Code != http.StatusMethodNotAllowed {
			t.Fatalf("status: want %d, got %d", http.StatusMethodNotAllowed, w.Code)
		}
	})
}
