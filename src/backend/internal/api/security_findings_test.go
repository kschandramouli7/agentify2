package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// fakeSecurityFindingsStore implements SecurityFindingsStore, recording the
// last UpsertSecurityFindings call and serving ListSecurityFindings from a
// canned row set — same shape as fakeServiceDependencyStore's coverage fake.
type fakeSecurityFindingsStore struct {
	lastTenantID  string
	lastClusterID string
	lastNamespace string
	lastFindings  []pgstore.SecurityFinding
	upsertErr     error
	listRows      []pgstore.SecurityFinding
	listErr       error
	getRow        *pgstore.SecurityFinding
	getErr        error
}

func (f *fakeSecurityFindingsStore) UpsertSecurityFindings(ctx context.Context, tenantID, clusterID, namespace string, findings []pgstore.SecurityFinding) error {
	f.lastTenantID = tenantID
	f.lastClusterID = clusterID
	f.lastNamespace = namespace
	f.lastFindings = findings
	return f.upsertErr
}

func (f *fakeSecurityFindingsStore) ListSecurityFindings(ctx context.Context, tenantID, namespace string) ([]pgstore.SecurityFinding, error) {
	if f.listErr != nil {
		return nil, f.listErr
	}
	return f.listRows, nil
}

func (f *fakeSecurityFindingsStore) GetSecurityFinding(ctx context.Context, tenantID, namespace, checkID, resourceKind, resourceName string) (*pgstore.SecurityFinding, error) {
	if f.getErr != nil {
		return nil, f.getErr
	}
	return f.getRow, nil
}

func securityFindingsHandler(store *fakeSecurityFindingsStore) *Handler {
	return &Handler{
		securityFindingsStore: store,
		integrationStore:      &fakeIntegrationStore{},
		logger:                slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

func postSecurityFindings(t *testing.T, h *Handler, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/security-findings", bytes.NewReader([]byte(body)))
	rec := httptest.NewRecorder()
	h.HandleSecurityFindingsUpsert(rec, req)
	return rec
}

func TestSecurityFindingsUpsert_PassesFindingsThrough(t *testing.T) {
	store := &fakeSecurityFindingsStore{}
	rec := postSecurityFindings(t, securityFindingsHandler(store), `{
		"namespace":"payments",
		"findings":[
			{"check_id":"namespace-has-networkpolicy","resource_kind":"Namespace","resource_name":"payments","severity":"high","evidence":"0 NetworkPolicy objects"},
			{"check_id":"ingress-missing-tls","resource_kind":"Ingress","resource_name":"shop-ingress","severity":"critical","evidence":"no tls block"}
		]}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if store.lastNamespace != "payments" {
		t.Errorf("namespace = %q, want payments", store.lastNamespace)
	}
	if len(store.lastFindings) != 2 {
		t.Fatalf("got %d findings, want 2", len(store.lastFindings))
	}
	if store.lastFindings[0].CheckID != "namespace-has-networkpolicy" || store.lastFindings[0].Severity != "high" {
		t.Errorf("finding[0] = %+v", store.lastFindings[0])
	}
}

// An empty findings list is meaningful (the namespace passed every check)
// and must still reach UpsertSecurityFindings, not short-circuit — that's
// what lets a previously-open finding resolve.
func TestSecurityFindingsUpsert_EmptyFindingsStillCallsUpsert(t *testing.T) {
	store := &fakeSecurityFindingsStore{}
	rec := postSecurityFindings(t, securityFindingsHandler(store), `{"namespace":"payments","findings":[]}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if store.lastNamespace != "payments" {
		t.Errorf("UpsertSecurityFindings was not called with the empty push")
	}
	if len(store.lastFindings) != 0 {
		t.Errorf("findings = %+v, want empty", store.lastFindings)
	}
}

func TestSecurityFindingsUpsert_RequiresNamespace(t *testing.T) {
	rec := postSecurityFindings(t, securityFindingsHandler(&fakeSecurityFindingsStore{}), `{"findings":[]}`)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

func TestSecurityFindingsUpsert_SkipsEntriesMissingRequiredFields(t *testing.T) {
	store := &fakeSecurityFindingsStore{}
	rec := postSecurityFindings(t, securityFindingsHandler(store), `{
		"namespace":"payments",
		"findings":[
			{"check_id":"","resource_kind":"Namespace","resource_name":"payments","severity":"high"},
			{"check_id":"ingress-missing-tls","resource_kind":"Ingress","resource_name":"shop-ingress","severity":"critical"}
		]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if len(store.lastFindings) != 1 || store.lastFindings[0].CheckID != "ingress-missing-tls" {
		t.Errorf("findings = %+v, want only the well-formed entry", store.lastFindings)
	}
}

func TestSecurityFindingsUpsert_StoreFailureIs500(t *testing.T) {
	store := &fakeSecurityFindingsStore{upsertErr: errors.New("db down")}
	rec := postSecurityFindings(t, securityFindingsHandler(store), `{"namespace":"payments","findings":[]}`)
	if rec.Code != http.StatusInternalServerError {
		t.Errorf("status = %d, want 500", rec.Code)
	}
}

func TestSecurityFindingsList_RequiresNamespace(t *testing.T) {
	h := securityFindingsHandler(&fakeSecurityFindingsStore{})
	req := httptest.NewRequest(http.MethodGet, "/api/security-findings", nil)
	rec := httptest.NewRecorder()
	h.HandleSecurityFindingsList(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

func TestSecurityFindingsList_ReturnsRows(t *testing.T) {
	store := &fakeSecurityFindingsStore{listRows: []pgstore.SecurityFinding{
		{Namespace: "payments", CheckID: "namespace-has-networkpolicy", ResourceKind: "Namespace", ResourceName: "payments", Severity: "high", Status: "open", Confidence: "config-only"},
	}}
	h := securityFindingsHandler(store)
	req := httptest.NewRequest(http.MethodGet, "/api/security-findings?namespace=payments", nil)
	rec := httptest.NewRecorder()
	h.HandleSecurityFindingsList(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var out []pgstore.SecurityFinding
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(out) != 1 || out[0].CheckID != "namespace-has-networkpolicy" {
		t.Errorf("out = %+v", out)
	}
}

// An empty result must serialize as [] not null — the frontend maps over it.
func TestSecurityFindingsList_EmptyIsAnArrayNotNull(t *testing.T) {
	h := securityFindingsHandler(&fakeSecurityFindingsStore{})
	req := httptest.NewRequest(http.MethodGet, "/api/security-findings?namespace=empty", nil)
	rec := httptest.NewRecorder()
	h.HandleSecurityFindingsList(rec, req)
	if got := bytes.TrimSpace(rec.Body.Bytes()); string(got) != "[]" {
		t.Errorf("body = %s, want []", got)
	}
}
