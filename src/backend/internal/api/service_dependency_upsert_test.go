package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// fakeServiceDependencyStore implements ServiceDependencyStore, recording
// the last UpsertServiceDependency call.
type fakeServiceDependencyStore struct {
	lastTenantID  string
	lastClusterID string
	lastNamespace string
	lastFrom      string
	lastTo        string
	upsertCalled  bool

	// Scan-coverage calls, recorded so the coverage tests can assert on them
	// (ROADMAP P27 phase 1).
	coverageCalls []coverageCall
	coverageErr   error
	coverageRows  []pgstore.ScanCoverage
}

type coverageCall struct {
	tenantID, clusterID, namespace, service     string
	cycles, podsSeen, podsSampled, logsReadable int
	logLines                                    int64
}

func (f *fakeServiceDependencyStore) UpsertScanCoverage(ctx context.Context, tenantID, clusterID, namespace, service string, cycles, podsSeen, podsSampled, logsReadable int, logLines int64) error {
	f.coverageCalls = append(f.coverageCalls, coverageCall{
		tenantID: tenantID, clusterID: clusterID, namespace: namespace, service: service,
		cycles: cycles, podsSeen: podsSeen, podsSampled: podsSampled,
		logsReadable: logsReadable, logLines: logLines,
	})
	return f.coverageErr
}

func (f *fakeServiceDependencyStore) ListScanCoverage(ctx context.Context, tenantID, namespace string) ([]pgstore.ScanCoverage, error) {
	if f.coverageErr != nil {
		return nil, f.coverageErr
	}
	return f.coverageRows, nil
}

func (f *fakeServiceDependencyStore) UpsertServiceDependency(ctx context.Context, id, tenantID, clusterID, namespace, fromService, toService string) error {
	f.upsertCalled = true
	f.lastTenantID = tenantID
	f.lastClusterID = clusterID
	f.lastNamespace = namespace
	f.lastFrom = fromService
	f.lastTo = toService
	return nil
}

func (f *fakeServiceDependencyStore) ListServiceDependencies(ctx context.Context, tenantID, namespace string) ([]pgstore.ServiceDependency, error) {
	return nil, nil
}

// TestHandleServiceDependencyUpsert_ClusterIDOverride exercises ADR 0029's
// trusted-internal-caller fallback: an explicit cluster_id in the body is
// honored only when resolveTenantContext resolved no clusterID of its own
// (no CollectorToken presented) — never clobbering a real collector's own
// credential-derived scoping.
func TestHandleServiceDependencyUpsert_ClusterIDOverride(t *testing.T) {
	body := `{"namespace":"payments","from_service":"payment-worker","to_service":"payment-api","cluster_id":"cluster-from-glue-miner"}`

	t.Run("no credential presented — explicit cluster_id in body is honored", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/service-dependencies", strings.NewReader(body))
		w := httptest.NewRecorder()

		h.HandleServiceDependencyUpsert(w, req)

		if w.Code != http.StatusNoContent {
			t.Fatalf("status: want %d, got %d", http.StatusNoContent, w.Code)
		}
		if !store.upsertCalled {
			t.Fatal("UpsertServiceDependency was not called")
		}
		if store.lastClusterID != "cluster-from-glue-miner" {
			t.Errorf("clusterID: want %q (from body), got %q", "cluster-from-glue-miner", store.lastClusterID)
		}
		if store.lastTenantID != pgstore.DefaultTenantID {
			t.Errorf("tenantID: want DefaultTenantID, got %q", store.lastTenantID)
		}
	})

	t.Run("a real collector's own CollectorToken-derived clusterID always wins over the body field", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		integStore := &fakeIntegrationStore{
			byToken: map[string]*pgstore.Integration{
				"real-token": {ID: "cluster-42", TenantID: "tenant-a"},
			},
		}
		h := &Handler{serviceDepsStore: store, integrationStore: integStore}
		req := httptest.NewRequest(http.MethodPost, "/api/service-dependencies", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer real-token")
		w := httptest.NewRecorder()

		h.HandleServiceDependencyUpsert(w, req)

		if w.Code != http.StatusNoContent {
			t.Fatalf("status: want %d, got %d", http.StatusNoContent, w.Code)
		}
		if store.lastClusterID != "cluster-42" {
			t.Errorf("clusterID: want %q (from the collector's own credential, not the body), got %q", "cluster-42", store.lastClusterID)
		}
		if store.lastTenantID != "tenant-a" {
			t.Errorf("tenantID: want tenant-a, got %q", store.lastTenantID)
		}
	})

	t.Run("no credential and no explicit cluster_id — clusterID stays empty, unchanged from today", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/service-dependencies",
			strings.NewReader(`{"namespace":"payments","from_service":"payment-worker","to_service":"payment-api"}`))
		w := httptest.NewRecorder()

		h.HandleServiceDependencyUpsert(w, req)

		if w.Code != http.StatusNoContent {
			t.Fatalf("status: want %d, got %d", http.StatusNoContent, w.Code)
		}
		if store.lastClusterID != "" {
			t.Errorf("clusterID: want empty, got %q", store.lastClusterID)
		}
	})

	t.Run("unrecognized bearer token is still rejected before any cluster_id logic runs", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/service-dependencies", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer nonexistent-token")
		w := httptest.NewRecorder()

		h.HandleServiceDependencyUpsert(w, req)

		if w.Code != http.StatusUnauthorized {
			t.Fatalf("status: want %d, got %d", http.StatusUnauthorized, w.Code)
		}
		if store.upsertCalled {
			t.Error("UpsertServiceDependency should not have been called")
		}
	})
}
