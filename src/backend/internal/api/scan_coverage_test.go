package api

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

func coverageHandler(store *fakeServiceDependencyStore) *Handler {
	return &Handler{
		serviceDepsStore: store,
		integrationStore: &fakeIntegrationStore{},
		logger:           slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

func postCoverage(t *testing.T, h *Handler, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/scan-coverage", bytes.NewReader([]byte(body)))
	rec := httptest.NewRecorder()
	h.HandleScanCoverageUpsert(rec, req)
	return rec
}

// One report per namespace covering many services — batched deliberately, so
// the handler must fan it out into one upsert per service.
func TestScanCoverageUpsert_FansOutPerService(t *testing.T) {
	store := &fakeServiceDependencyStore{}
	rec := postCoverage(t, coverageHandler(store), `{
		"namespace":"payments",
		"services":[
			{"service":"api","scan_cycles":1,"pods_seen":4,"pods_sampled":2,"logs_readable":2,"log_lines":400},
			{"service":"worker","scan_cycles":1,"pods_seen":1,"pods_sampled":0,"logs_readable":0,"log_lines":0}
		]}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if len(store.coverageCalls) != 2 {
		t.Fatalf("got %d upserts, want 2", len(store.coverageCalls))
	}
	api := store.coverageCalls[0]
	if api.service != "api" || api.podsSeen != 4 || api.podsSampled != 2 || api.logLines != 400 {
		t.Errorf("api call = %+v", api)
	}
	// The case the denominator exists for: seen but never sampled.
	worker := store.coverageCalls[1]
	if worker.podsSeen != 1 || worker.podsSampled != 0 {
		t.Errorf("worker call = %+v, want pods_seen 1 / pods_sampled 0", worker)
	}
}

func TestScanCoverageUpsert_RequiresNamespace(t *testing.T) {
	rec := postCoverage(t, coverageHandler(&fakeServiceDependencyStore{}), `{"services":[]}`)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

func TestScanCoverageUpsert_SkipsEntriesWithNoServiceName(t *testing.T) {
	store := &fakeServiceDependencyStore{}
	rec := postCoverage(t, coverageHandler(store), `{
		"namespace":"payments",
		"services":[{"service":"","scan_cycles":1},{"service":"api","scan_cycles":1}]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if len(store.coverageCalls) != 1 || store.coverageCalls[0].service != "api" {
		t.Errorf("calls = %+v, want only api", store.coverageCalls)
	}
}

// Coverage is best-effort accounting: a partial denominator beats none, so a
// store failure on one service must not discard the whole report.
func TestScanCoverageUpsert_TotalStoreFailureIs500(t *testing.T) {
	store := &fakeServiceDependencyStore{coverageErr: errors.New("db down")}
	rec := postCoverage(t, coverageHandler(store), `{
		"namespace":"payments","services":[{"service":"api","scan_cycles":1}]}`)
	if rec.Code != http.StatusInternalServerError {
		t.Errorf("status = %d, want 500 when nothing could be written", rec.Code)
	}
}

func TestScanCoverageList_RequiresNamespace(t *testing.T) {
	h := coverageHandler(&fakeServiceDependencyStore{})
	req := httptest.NewRequest(http.MethodGet, "/api/scan-coverage", nil)
	rec := httptest.NewRecorder()
	h.HandleScanCoverageList(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

func TestScanCoverageList_ReturnsRows(t *testing.T) {
	store := &fakeServiceDependencyStore{coverageRows: []pgstore.ScanCoverage{
		{Namespace: "payments", Service: "worker", ScanCycles: 2880, PodsSeen: 1, PodsSampled: 51},
	}}
	h := coverageHandler(store)
	req := httptest.NewRequest(http.MethodGet, "/api/scan-coverage?namespace=payments", nil)
	rec := httptest.NewRecorder()
	h.HandleScanCoverageList(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var out []pgstore.ScanCoverage
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(out) != 1 || out[0].PodsSampled != 51 {
		t.Errorf("out = %+v", out)
	}
}

// An empty result must serialize as [] not null — the frontend maps over it.
func TestScanCoverageList_EmptyIsAnArrayNotNull(t *testing.T) {
	h := coverageHandler(&fakeServiceDependencyStore{})
	req := httptest.NewRequest(http.MethodGet, "/api/scan-coverage?namespace=empty", nil)
	rec := httptest.NewRecorder()
	h.HandleScanCoverageList(rec, req)
	if got := bytes.TrimSpace(rec.Body.Bytes()); string(got) != "[]" {
		t.Errorf("body = %s, want []", got)
	}
}
