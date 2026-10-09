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

// TestHandleCrossClusterPairsList exercises ROADMAP P31 phase 3 (ADR 0039):
// a store-only, convention-based read — same shape as every other
// best-effort list handler here (empty-list-not-error, tunable thresholds
// via the same intQueryParam helper HandleColdServicesList uses).
func TestHandleCrossClusterPairsList(t *testing.T) {
	t.Run("missing namespace is rejected", func(t *testing.T) {
		h := &Handler{clusterServiceStore: &fakeClusterServiceStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cross-cluster-pairs", nil)
		w := httptest.NewRecorder()

		h.HandleCrossClusterPairsList(w, req)

		if w.Code != http.StatusBadRequest {
			t.Fatalf("status: want %d, got %d", http.StatusBadRequest, w.Code)
		}
	})

	t.Run("store not configured returns empty list, not an error", func(t *testing.T) {
		h := &Handler{}
		req := httptest.NewRequest(http.MethodGet, "/api/cross-cluster-pairs?namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleCrossClusterPairsList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if !strings.Contains(w.Body.String(), "[]") {
			t.Errorf("body: want empty list, got %s", w.Body.String())
		}
	})

	t.Run("store error degrades to empty list, not a 500", func(t *testing.T) {
		store := &fakeClusterServiceStore{crossClusterErr: errors.New("boom")}
		h := &Handler{clusterServiceStore: store, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
		req := httptest.NewRequest(http.MethodGet, "/api/cross-cluster-pairs?namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleCrossClusterPairsList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if !strings.Contains(w.Body.String(), "[]") {
			t.Errorf("body: want empty list on store error, got %s", w.Body.String())
		}
	})

	t.Run("default thresholds are used when absent, and are tunable", func(t *testing.T) {
		store := &fakeClusterServiceStore{}
		h := &Handler{clusterServiceStore: store}
		req := httptest.NewRequest(http.MethodGet, "/api/cross-cluster-pairs?namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleCrossClusterPairsList(w, req)

		if store.lastCCPStaleDays != 14 || store.lastCCPScannedWithin != 2 {
			t.Errorf("defaults: want stale=14 scanned_within=2, got stale=%d scanned_within=%d",
				store.lastCCPStaleDays, store.lastCCPScannedWithin)
		}

		req2 := httptest.NewRequest(http.MethodGet, "/api/cross-cluster-pairs?namespace=payments&stale_days=30&scanned_within_days=7", nil)
		w2 := httptest.NewRecorder()
		h.HandleCrossClusterPairsList(w2, req2)
		if store.lastCCPStaleDays != 30 || store.lastCCPScannedWithin != 7 {
			t.Errorf("explicit: want stale=30 scanned_within=7, got stale=%d scanned_within=%d",
				store.lastCCPStaleDays, store.lastCCPScannedWithin)
		}
	})

	t.Run("returns the store's pairs, including multiple sides", func(t *testing.T) {
		store := &fakeClusterServiceStore{crossClusterPairs: []pgstore.CrossClusterPair{
			{
				Namespace: "payments", Service: "checkout-api",
				Sides: []pgstore.CrossClusterSide{
					{ClusterID: "cluster-old", Cold: true},
					{ClusterID: "cluster-new", Cold: false},
				},
			},
		}}
		h := &Handler{clusterServiceStore: store}
		req := httptest.NewRequest(http.MethodGet, "/api/cross-cluster-pairs?namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleCrossClusterPairsList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if !strings.Contains(w.Body.String(), "cluster-old") || !strings.Contains(w.Body.String(), "cluster-new") {
			t.Errorf("body: want both cluster sides, got %s", w.Body.String())
		}
	})
}
