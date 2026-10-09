package api

import (
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// TestHandleColdServicesList exercises ROADMAP P31 phase 2 (ADR 0038): a
// deterministic read over service_dependencies.last_seen and
// scan_coverage.last_scan, with no schema change — the handler's own job
// here is just threshold plumbing and the usual empty-list-not-error shape.
func TestHandleColdServicesList(t *testing.T) {
	t.Run("missing namespace is rejected", func(t *testing.T) {
		h := &Handler{serviceDepsStore: &fakeServiceDependencyStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cold-services", nil)
		w := httptest.NewRecorder()

		h.HandleColdServicesList(w, req)

		if w.Code != http.StatusBadRequest {
			t.Fatalf("status: want %d, got %d", http.StatusBadRequest, w.Code)
		}
	})

	t.Run("store not configured returns empty list, not an error", func(t *testing.T) {
		h := &Handler{}
		req := httptest.NewRequest(http.MethodGet, "/api/cold-services?namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleColdServicesList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if w.Body.String() != "[]" && !strings.Contains(w.Body.String(), "[]") {
			t.Errorf("body: want empty list, got %s", w.Body.String())
		}
	})

	t.Run("store error degrades to empty list, not a 500", func(t *testing.T) {
		store := &fakeServiceDependencyStore{coldServicesErr: errors.New("boom")}
		h := &Handler{serviceDepsStore: store, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
		req := httptest.NewRequest(http.MethodGet, "/api/cold-services?namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleColdServicesList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if !strings.Contains(w.Body.String(), "[]") {
			t.Errorf("body: want empty list on store error, got %s", w.Body.String())
		}
	})

	t.Run("default thresholds (14 stale / 2 scanned-within) are used when absent", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cold-services?namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleColdServicesList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if store.lastColdStaleDays != 14 || store.lastColdScannedWithinDays != 2 {
			t.Errorf("thresholds: want stale=14 scanned_within=2, got stale=%d scanned_within=%d",
				store.lastColdStaleDays, store.lastColdScannedWithinDays)
		}
	})

	t.Run("explicit thresholds override the defaults", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cold-services?namespace=payments&stale_days=30&scanned_within_days=5", nil)
		w := httptest.NewRecorder()

		h.HandleColdServicesList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if store.lastColdStaleDays != 30 || store.lastColdScannedWithinDays != 5 {
			t.Errorf("thresholds: want stale=30 scanned_within=5, got stale=%d scanned_within=%d",
				store.lastColdStaleDays, store.lastColdScannedWithinDays)
		}
	})

	t.Run("a malformed threshold falls back to the default rather than erroring", func(t *testing.T) {
		store := &fakeServiceDependencyStore{}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cold-services?namespace=payments&stale_days=not-a-number", nil)
		w := httptest.NewRecorder()

		h.HandleColdServicesList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if store.lastColdStaleDays != 14 {
			t.Errorf("stale_days: want default 14 on malformed input, got %d", store.lastColdStaleDays)
		}
	})

	t.Run("returns the store's rows, including the never-seen case", func(t *testing.T) {
		store := &fakeServiceDependencyStore{coldServicesRows: []pgstore.ColdService{
			{Namespace: "payments", Service: "legacy-worker", LastSeen: nil, LastScan: time.Now()},
		}}
		h := &Handler{serviceDepsStore: store, integrationStore: &fakeIntegrationStore{}}
		req := httptest.NewRequest(http.MethodGet, "/api/cold-services?namespace=payments", nil)
		w := httptest.NewRecorder()

		h.HandleColdServicesList(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("status: want %d, got %d", http.StatusOK, w.Code)
		}
		if !strings.Contains(w.Body.String(), "legacy-worker") {
			t.Errorf("body: want legacy-worker, got %s", w.Body.String())
		}
		if strings.Contains(w.Body.String(), `"last_seen"`) {
			t.Errorf("body: last_seen must be omitted (omitempty) when nil, got %s", w.Body.String())
		}
	})
}
