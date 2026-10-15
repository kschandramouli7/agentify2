package api

import (
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// TestHandleServiceLabelUpsert pins ROADMAP P32 (ADR 0043): validation,
// the same ClusterID trusted-caller override serviceDependencyUpsertRequest
// already has, and the degrade-when-unconfigured path.
func TestHandleServiceLabelUpsert(t *testing.T) {
	t.Run("success — all required fields present", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/service-labels",
			strings.NewReader(`{"namespace":"payments","service":"checkout-ui","label_key":"apm-id","label_value":"APM123456"}`))
		w := httptest.NewRecorder()

		h.HandleServiceLabelUpsert(w, req)

		if w.Code != http.StatusNoContent {
			t.Fatalf("status: want %d, got %d (%s)", http.StatusNoContent, w.Code, w.Body.String())
		}
		if len(store.labelUpsertCalls) != 1 {
			t.Fatalf("want 1 upsert call, got %d", len(store.labelUpsertCalls))
		}
		got := store.labelUpsertCalls[0]
		if got.namespace != "payments" || got.service != "checkout-ui" || got.labelKey != "apm-id" || got.labelValue != "APM123456" {
			t.Errorf("unexpected call: %+v", got)
		}
		if got.tenantID != pgstore.DefaultTenantID {
			t.Errorf("tenantID: want DefaultTenantID, got %q", got.tenantID)
		}
	})

	t.Run("explicit cluster_id in body is honored when no collector credential is presented", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/service-labels",
			strings.NewReader(`{"namespace":"payments","service":"checkout-ui","label_key":"apm-id","label_value":"APM123456","cluster_id":"cluster-from-miner"}`))
		w := httptest.NewRecorder()

		h.HandleServiceLabelUpsert(w, req)

		if w.Code != http.StatusNoContent {
			t.Fatalf("status: want %d, got %d", http.StatusNoContent, w.Code)
		}
		if store.labelUpsertCalls[0].clusterID != "cluster-from-miner" {
			t.Errorf("clusterID: want %q, got %q", "cluster-from-miner", store.labelUpsertCalls[0].clusterID)
		}
	})

	for _, tc := range []struct {
		name string
		body string
	}{
		{"missing namespace", `{"service":"checkout-ui","label_key":"apm-id","label_value":"APM123456"}`},
		{"missing service", `{"namespace":"payments","label_key":"apm-id","label_value":"APM123456"}`},
		{"missing label_key", `{"namespace":"payments","service":"checkout-ui","label_value":"APM123456"}`},
		{"missing label_value", `{"namespace":"payments","service":"checkout-ui","label_key":"apm-id"}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := &fakeServiceDependencyStore{}
			h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
			req := httptest.NewRequest(http.MethodPost, "/api/service-labels", strings.NewReader(tc.body))
			w := httptest.NewRecorder()

			h.HandleServiceLabelUpsert(w, req)

			if w.Code != http.StatusBadRequest {
				t.Fatalf("status: want %d, got %d", http.StatusBadRequest, w.Code)
			}
			if len(store.labelUpsertCalls) != 0 {
				t.Error("store must not be called when validation fails")
			}
		})
	}

	t.Run("GET is rejected", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/service-labels", nil)
		w := httptest.NewRecorder()

		h.HandleServiceLabelUpsert(w, req)

		if w.Code != http.StatusMethodNotAllowed {
			t.Fatalf("status: want %d, got %d", http.StatusMethodNotAllowed, w.Code)
		}
	})

	t.Run("store not configured degrades to 503, never panics", func(t *testing.T) {
		h := &Handler{serviceDepsStore: nil, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodPost, "/api/service-labels",
			strings.NewReader(`{"namespace":"payments","service":"checkout-ui","label_key":"apm-id","label_value":"APM123456"}`))
		w := httptest.NewRecorder()

		h.HandleServiceLabelUpsert(w, req)

		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("status: want %d, got %d", http.StatusServiceUnavailable, w.Code)
		}
	})
}

// TestHandleServiceLabelsLookup pins the GET /api/service-labels lookup —
// the first half of a label-scoped chat query (ROADMAP P32, ADR 0043).
func TestHandleServiceLabelsLookup(t *testing.T) {
	t.Run("success — returns whatever the store resolved", func(t *testing.T) {
		store := &fakeServiceDependencyStore{
			servicesByLabelRows: []pgstore.ServiceRef{
				{ClusterID: "cluster-a", Namespace: "payments", Service: "checkout-ui"},
				{ClusterID: "cluster-a", Namespace: "payments", Service: "checkout-api"},
			},
		}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/service-labels?label_key=apm-id&label_value=APM123456", nil)
		w := httptest.NewRecorder()

		h.HandleServiceLabelsLookup(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if store.lastLabelKey != "apm-id" || store.lastLabelValue != "APM123456" {
			t.Errorf("want the query params passed through, got key=%q value=%q", store.lastLabelKey, store.lastLabelValue)
		}
		if !strings.Contains(w.Body.String(), "checkout-ui") || !strings.Contains(w.Body.String(), "checkout-api") {
			t.Errorf("response body missing expected services: %s", w.Body.String())
		}
	})

	for _, tc := range []struct {
		name string
		url  string
	}{
		{"missing label_key", "/api/service-labels?label_value=APM123456"},
		{"missing label_value", "/api/service-labels?label_key=apm-id"},
		{"missing both", "/api/service-labels"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := &fakeServiceDependencyStore{}
			h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
			req := httptest.NewRequest(http.MethodGet, tc.url, nil)
			w := httptest.NewRecorder()

			h.HandleServiceLabelsLookup(w, req)

			if w.Code != http.StatusBadRequest {
				t.Fatalf("status: want %d, got %d", http.StatusBadRequest, w.Code)
			}
		})
	}

	t.Run("store not configured degrades to an empty list, not an error", func(t *testing.T) {
		h := &Handler{serviceDepsStore: nil, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/service-labels?label_key=apm-id&label_value=APM123456", nil)
		w := httptest.NewRecorder()

		h.HandleServiceLabelsLookup(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if !strings.Contains(w.Body.String(), `"services":[]`) {
			t.Errorf("want an explicit empty list, got %s", w.Body.String())
		}
	})

	t.Run("a store error degrades to an empty list, never a 500 — same posture as every other list endpoint here", func(t *testing.T) {
		store := &fakeServiceDependencyStore{servicesByLabelErr: errors.New("boom")}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
		req := httptest.NewRequest(http.MethodGet, "/api/service-labels?label_key=apm-id&label_value=APM123456", nil)
		w := httptest.NewRecorder()

		h.HandleServiceLabelsLookup(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
	})
}

// TestHandleServiceDependenciesForServices pins the GET
// /api/service-dependencies-for-services endpoint — the second half of a
// label-scoped chat query (ROADMAP P32, ADR 0043).
func TestHandleServiceDependenciesForServices(t *testing.T) {
	t.Run("success — services param decodes and reaches the store", func(t *testing.T) {
		store := &fakeServiceDependencyStore{
			depsForServicesRows: []pgstore.ServiceDependency{
				{FromService: "checkout-ui", ToService: "checkout-api"},
			},
		}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet,
			`/api/service-dependencies-for-services?services=`+
				`%5B%7B%22cluster_id%22%3A%22cluster-a%22%2C%22namespace%22%3A%22payments%22%2C%22service%22%3A%22checkout-api%22%7D%5D`, nil)
		w := httptest.NewRecorder()

		h.HandleServiceDependenciesForServices(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d (%s)", http.StatusOK, w.Code, w.Body.String())
		}
		if len(store.lastServicesScope) != 1 || store.lastServicesScope[0].Service != "checkout-api" {
			t.Errorf("want the decoded scope to reach the store, got %+v", store.lastServicesScope)
		}
		if !strings.Contains(w.Body.String(), "checkout-ui") {
			t.Errorf("response body missing expected edge: %s", w.Body.String())
		}
	})

	t.Run("missing services param", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/service-dependencies-for-services", nil)
		w := httptest.NewRecorder()

		h.HandleServiceDependenciesForServices(w, req)

		if w.Code != http.StatusBadRequest {
			t.Fatalf("status: want %d, got %d", http.StatusBadRequest, w.Code)
		}
	})

	t.Run("malformed services JSON is a 400, not a 500", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/service-dependencies-for-services?services=not-json", nil)
		w := httptest.NewRecorder()

		h.HandleServiceDependenciesForServices(w, req)

		if w.Code != http.StatusBadRequest {
			t.Fatalf("status: want %d, got %d", http.StatusBadRequest, w.Code)
		}
	})

	t.Run("store not configured degrades to an empty list", func(t *testing.T) {
		h := &Handler{serviceDepsStore: nil, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, `/api/service-dependencies-for-services?services=%5B%5D`, nil)
		w := httptest.NewRecorder()

		h.HandleServiceDependenciesForServices(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
	})
}
