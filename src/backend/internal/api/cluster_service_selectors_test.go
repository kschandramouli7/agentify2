package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// TestHandleClusterServiceSelectors exercises the ADR 0029 endpoint the
// Glue-based dependency miner reads to resolve from_service precisely,
// without live cluster access of its own — same "degrade to an empty
// result, never an error" contract as HandleResolveCluster, but scoped to
// one explicit cluster_id (a service name can mean a different Service,
// with a different selector, in each cluster).
func TestHandleClusterServiceSelectors(t *testing.T) {
	t.Run("missing cluster_id is rejected", func(t *testing.T) {
		h := &Handler{clusterServiceStore: &fakeClusterServiceStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cluster-service-selectors?namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleClusterServiceSelectors(w, req)

		if w.Code != http.StatusBadRequest {
			t.Fatalf("status: want %d, got %d", http.StatusBadRequest, w.Code)
		}
	})

	t.Run("missing namespace is rejected", func(t *testing.T) {
		h := &Handler{clusterServiceStore: &fakeClusterServiceStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cluster-service-selectors?cluster_id=cluster-a", nil)
		w := httptest.NewRecorder()

		h.HandleClusterServiceSelectors(w, req)

		if w.Code != http.StatusBadRequest {
			t.Fatalf("status: want %d, got %d", http.StatusBadRequest, w.Code)
		}
	})

	t.Run("no match returns 200 with an empty map, not an error", func(t *testing.T) {
		h := &Handler{clusterServiceStore: &fakeClusterServiceStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cluster-service-selectors?cluster_id=cluster-a&namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleClusterServiceSelectors(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		var resp clusterServiceSelectorsResponse
		if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
			t.Fatalf("decode: %v", err)
		}
		if len(resp.Selectors) != 0 {
			t.Errorf("Selectors: want empty, got %v", resp.Selectors)
		}
	})

	t.Run("returns only the requested cluster's selectors, not another cluster's", func(t *testing.T) {
		h := &Handler{clusterServiceStore: &fakeClusterServiceStore{
			selectors: map[string]map[string]map[string]string{
				"cluster-a/payments": {"payment-api": {"app": "payment-api"}},
				"cluster-b/payments": {"payment-api": {"app": "payment-api-b"}},
			},
		}}
		req := httptest.NewRequest(http.MethodGet, "/api/cluster-service-selectors?cluster_id=cluster-a&namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleClusterServiceSelectors(w, req)

		var resp clusterServiceSelectorsResponse
		json.NewDecoder(w.Body).Decode(&resp)
		if resp.Selectors["payment-api"]["app"] != "payment-api" {
			t.Errorf("payment-api selector: want app=payment-api, got %v", resp.Selectors["payment-api"])
		}
	})

	t.Run("no cluster service store configured degrades to an empty map", func(t *testing.T) {
		h := &Handler{}
		req := httptest.NewRequest(http.MethodGet, "/api/cluster-service-selectors?cluster_id=cluster-a&namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleClusterServiceSelectors(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
	})
}
