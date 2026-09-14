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
	"time"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

// fakeSecurityEngagementStore implements SecurityEngagementStore in memory —
// same shape as fakeSecurityFindingsStore's canned-row-set fake, sized for
// the propose->decide->complete lifecycle instead of a single upsert/list.
type fakeSecurityEngagementStore struct {
	byID       map[string]*pgstore.SecurityEngagement
	createErr  error
	decideErr  error
	completeErr error
}

func newFakeSecurityEngagementStore() *fakeSecurityEngagementStore {
	return &fakeSecurityEngagementStore{byID: map[string]*pgstore.SecurityEngagement{}}
}

func (f *fakeSecurityEngagementStore) CreateSecurityEngagement(ctx context.Context, e *pgstore.SecurityEngagement) error {
	if f.createErr != nil {
		return f.createErr
	}
	cp := *e
	cp.Status = "pending"
	cp.CreatedAt = time.Now()
	f.byID[e.ID] = &cp
	return nil
}

func (f *fakeSecurityEngagementStore) GetSecurityEngagement(ctx context.Context, tenantID, id string) (*pgstore.SecurityEngagement, error) {
	e, ok := f.byID[id]
	if !ok || e.TenantID != tenantID {
		return nil, errors.New("not found")
	}
	cp := *e
	return &cp, nil
}

func (f *fakeSecurityEngagementStore) ListSecurityEngagements(ctx context.Context, tenantID, status string, limit int) ([]pgstore.SecurityEngagement, error) {
	var out []pgstore.SecurityEngagement
	for _, e := range f.byID {
		if e.TenantID != tenantID {
			continue
		}
		if status != "" && e.Status != status {
			continue
		}
		out = append(out, *e)
	}
	return out, nil
}

func (f *fakeSecurityEngagementStore) DecideSecurityEngagement(ctx context.Context, tenantID, id, status, approvedBy string) (bool, error) {
	if f.decideErr != nil {
		return false, f.decideErr
	}
	e, ok := f.byID[id]
	if !ok || e.TenantID != tenantID || e.Status != "pending" {
		return false, nil
	}
	e.Status = status
	e.ApprovedBy = approvedBy
	now := time.Now()
	e.DecidedAt = &now
	return true, nil
}

func (f *fakeSecurityEngagementStore) CompleteSecurityEngagement(ctx context.Context, tenantID, id, status string, result map[string]interface{}, errMsg string, confirmed *bool) error {
	if f.completeErr != nil {
		return f.completeErr
	}
	e, ok := f.byID[id]
	if !ok || e.TenantID != tenantID {
		return errors.New("not found")
	}
	e.Status = status
	e.Result = result
	e.Error = errMsg
	now := time.Now()
	e.CompletedAt = &now
	return nil
}

func securityEngagementHandler(store *fakeSecurityEngagementStore, findings *fakeSecurityFindingsStore) *Handler {
	return &Handler{
		securityEngagementStore: store,
		securityFindingsStore:   findings,
		securityEngagementConfig: SecurityEngagementConfig{
			ProposalTTL: 30 * time.Minute,
			Env:         "dev", // unset AuthToken fails closed outside dev; tests run as dev
		},
		integrationStore: &fakeIntegrationStore{},
		logger:           slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

func TestSecurityEngagementCreate_RequiresMappedTechnique(t *testing.T) {
	h := securityEngagementHandler(newFakeSecurityEngagementStore(), &fakeSecurityFindingsStore{})
	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements", jsonBody(t, map[string]string{
		"namespace": "payments", "check_id": "namespace-has-networkpolicy",
		"resource_kind": "Namespace", "resource_name": "payments",
	}))
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementCreate(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (no phase-2 technique mapped to this check_id)", rec.Code)
	}
}

func TestSecurityEngagementCreate_RequiresTargetFindingToExist(t *testing.T) {
	findings := &fakeSecurityFindingsStore{getErr: errors.New("no rows")}
	h := securityEngagementHandler(newFakeSecurityEngagementStore(), findings)
	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements", jsonBody(t, map[string]string{
		"namespace": "payments", "check_id": "ingress-missing-tls",
		"resource_kind": "Ingress", "resource_name": "shop-ingress",
	}))
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementCreate(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
}

func TestSecurityEngagementCreate_RejectsResolvedFinding(t *testing.T) {
	findings := &fakeSecurityFindingsStore{getRow: &pgstore.SecurityFinding{
		CheckID: "ingress-missing-tls", ResourceKind: "Ingress", ResourceName: "shop-ingress",
		Status: "resolved", TargetHost: "shop-ingress.payments.svc",
	}}
	h := securityEngagementHandler(newFakeSecurityEngagementStore(), findings)
	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements", jsonBody(t, map[string]string{
		"namespace": "payments", "check_id": "ingress-missing-tls",
		"resource_kind": "Ingress", "resource_name": "shop-ingress",
	}))
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementCreate(rec, req)
	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409 (resolved finding — nothing to verify)", rec.Code)
	}
}

func TestSecurityEngagementCreate_RejectsFindingWithNoTargetHost(t *testing.T) {
	findings := &fakeSecurityFindingsStore{getRow: &pgstore.SecurityFinding{
		CheckID: "ingress-missing-tls", ResourceKind: "Ingress", ResourceName: "shop-ingress",
		Status: "open", TargetHost: "",
	}}
	h := securityEngagementHandler(newFakeSecurityEngagementStore(), findings)
	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements", jsonBody(t, map[string]string{
		"namespace": "payments", "check_id": "ingress-missing-tls",
		"resource_kind": "Ingress", "resource_name": "shop-ingress",
	}))
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementCreate(rec, req)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422", rec.Code)
	}
}

func TestSecurityEngagementCreate_Succeeds(t *testing.T) {
	findings := &fakeSecurityFindingsStore{getRow: &pgstore.SecurityFinding{
		CheckID: "ingress-missing-tls", ResourceKind: "Ingress", ResourceName: "shop-ingress",
		Status: "open", TargetHost: "shop-ingress.payments.svc",
	}}
	store := newFakeSecurityEngagementStore()
	h := securityEngagementHandler(store, findings)
	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements", jsonBody(t, map[string]string{
		"namespace": "payments", "check_id": "ingress-missing-tls",
		"resource_kind": "Ingress", "resource_name": "shop-ingress",
	}))
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementCreate(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body=%s", rec.Code, rec.Body.String())
	}
	var out SecurityEngagementResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if out.Status != "pending" || out.Technique != "confirm-http-reachable" || out.Phase != 2 {
		t.Errorf("out = %+v", out)
	}
	if len(store.byID) != 1 {
		t.Fatalf("want exactly one engagement created, got %d", len(store.byID))
	}
}

func TestSecurityEngagementApprove_RequiresAuthOutsideDev(t *testing.T) {
	store := newFakeSecurityEngagementStore()
	e := &pgstore.SecurityEngagement{
		ID: "eng-1", TenantID: pgstore.DefaultTenantID, Technique: "confirm-http-reachable",
		Scope: map[string]interface{}{"target_host": "shop-ingress.payments.svc"},
		ExpiresAt: time.Now().Add(30 * time.Minute),
	}
	_ = store.CreateSecurityEngagement(context.Background(), e)

	h := securityEngagementHandler(store, &fakeSecurityFindingsStore{})
	h.securityEngagementConfig.Env = "prod"
	h.securityEngagementConfig.AuthToken = "secret-token"

	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements/eng-1/approve", nil)
	req.SetPathValue("id", "eng-1")
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementApprove(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401 (no Authorization header, prod env, token set)", rec.Code)
	}
}

func TestSecurityEngagementApprove_ExpiredIsGone(t *testing.T) {
	store := newFakeSecurityEngagementStore()
	e := &pgstore.SecurityEngagement{
		ID: "eng-1", TenantID: pgstore.DefaultTenantID, Technique: "confirm-http-reachable",
		Scope: map[string]interface{}{"target_host": "shop-ingress.payments.svc"},
		ExpiresAt: time.Now().Add(-1 * time.Minute), // already expired
	}
	_ = store.CreateSecurityEngagement(context.Background(), e)

	h := securityEngagementHandler(store, &fakeSecurityFindingsStore{})
	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements/eng-1/approve", nil)
	req.SetPathValue("id", "eng-1")
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementApprove(rec, req)
	if rec.Code != http.StatusGone {
		t.Fatalf("status = %d, want 410", rec.Code)
	}
	updated, _ := store.GetSecurityEngagement(context.Background(), pgstore.DefaultTenantID, "eng-1")
	if updated.Status != "expired" {
		t.Errorf("engagement status = %q, want expired", updated.Status)
	}
}

func TestSecurityEngagementApprove_AlreadyDecidedIsConflict(t *testing.T) {
	store := newFakeSecurityEngagementStore()
	e := &pgstore.SecurityEngagement{
		ID: "eng-1", TenantID: pgstore.DefaultTenantID, Technique: "confirm-http-reachable",
		Scope: map[string]interface{}{"target_host": "shop-ingress.payments.svc"},
		ExpiresAt: time.Now().Add(30 * time.Minute),
	}
	_ = store.CreateSecurityEngagement(context.Background(), e)
	_, _ = store.DecideSecurityEngagement(context.Background(), pgstore.DefaultTenantID, "eng-1", "rejected", "alice")

	h := securityEngagementHandler(store, &fakeSecurityFindingsStore{})
	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements/eng-1/approve", nil)
	req.SetPathValue("id", "eng-1")
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementApprove(rec, req)
	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409 (already decided)", rec.Code)
	}
}

func TestSecurityEngagementReject_NeverDispatches(t *testing.T) {
	store := newFakeSecurityEngagementStore()
	e := &pgstore.SecurityEngagement{
		ID: "eng-1", TenantID: pgstore.DefaultTenantID, Technique: "confirm-http-reachable",
		Scope: map[string]interface{}{"target_host": "shop-ingress.payments.svc"},
		ExpiresAt: time.Now().Add(30 * time.Minute),
	}
	_ = store.CreateSecurityEngagement(context.Background(), e)

	h := securityEngagementHandler(store, &fakeSecurityFindingsStore{})
	// No verifier configured (h.securityVerifier is nil) — if reject somehow
	// dispatched, dispatchSecurityEngagement would flip status to "failed".
	// It must stay "rejected" instead.
	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements/eng-1/reject", nil)
	req.SetPathValue("id", "eng-1")
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementReject(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	updated, _ := store.GetSecurityEngagement(context.Background(), pgstore.DefaultTenantID, "eng-1")
	if updated.Status != "rejected" {
		t.Errorf("status = %q, want rejected (must never dispatch)", updated.Status)
	}
}

// TestSecurityEngagementApprove_DispatchesAndCompletes proves the full
// approve -> dispatch -> complete path against a fake agentify-security-
// verifier, and that dispatch never goes through agentify-agent (there is no
// AgentClient anywhere in this handler at all — SecurityVerifierClient is a
// plain HTTP client pointed straight at the isolated service).
func TestSecurityEngagementApprove_DispatchesAndCompletes(t *testing.T) {
	verifier := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/verify" {
			http.NotFound(w, r)
			return
		}
		var body map[string]interface{}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body["technique"] != "confirm-http-reachable" {
			t.Errorf("technique = %v, want confirm-http-reachable", body["technique"])
		}
		confirmed := true
		_ = json.NewEncoder(w).Encode(VerifyResult{Confirmed: &confirmed, Evidence: "HTTP 200 over plaintext"})
	}))
	defer verifier.Close()

	store := newFakeSecurityEngagementStore()
	e := &pgstore.SecurityEngagement{
		ID: "eng-1", TenantID: pgstore.DefaultTenantID, Technique: "confirm-http-reachable",
		Scope: map[string]interface{}{"target_host": "shop-ingress.payments.svc"},
		ExpiresAt: time.Now().Add(30 * time.Minute),
	}
	_ = store.CreateSecurityEngagement(context.Background(), e)

	h := securityEngagementHandler(store, &fakeSecurityFindingsStore{})
	h.securityVerifier = NewSecurityVerifierClient(verifier.URL, "")

	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements/eng-1/approve", nil)
	req.SetPathValue("id", "eng-1")
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementApprove(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body=%s", rec.Code, rec.Body.String())
	}
	var out SecurityEngagementResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if out.Status != "completed" {
		t.Fatalf("status = %q, want completed", out.Status)
	}
	if confirmed, _ := out.Result["confirmed"].(bool); !confirmed {
		t.Errorf("result[confirmed] = %v, want true", out.Result["confirmed"])
	}
}

// TestSecurityEngagementApprove_VerifierUnconfiguredFailsClosed proves a nil
// securityVerifier (SECURITY_VERIFIER_URL unset) fails the engagement rather
// than silently skipping the check or hanging.
func TestSecurityEngagementApprove_VerifierUnconfiguredFailsClosed(t *testing.T) {
	store := newFakeSecurityEngagementStore()
	e := &pgstore.SecurityEngagement{
		ID: "eng-1", TenantID: pgstore.DefaultTenantID, Technique: "confirm-http-reachable",
		Scope: map[string]interface{}{"target_host": "shop-ingress.payments.svc"},
		ExpiresAt: time.Now().Add(30 * time.Minute),
	}
	_ = store.CreateSecurityEngagement(context.Background(), e)

	h := securityEngagementHandler(store, &fakeSecurityFindingsStore{})
	req := httptest.NewRequest(http.MethodPost, "/admin/security-engagements/eng-1/approve", nil)
	req.SetPathValue("id", "eng-1")
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementApprove(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (approve itself succeeds; dispatch failure lands in the record)", rec.Code)
	}
	var out SecurityEngagementResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if out.Status != "failed" {
		t.Fatalf("status = %q, want failed", out.Status)
	}
}

func TestSecurityEngagementList_ScopesByTenant(t *testing.T) {
	store := newFakeSecurityEngagementStore()
	_ = store.CreateSecurityEngagement(context.Background(), &pgstore.SecurityEngagement{
		ID: "eng-mine", TenantID: pgstore.DefaultTenantID, Technique: "confirm-http-reachable",
		ExpiresAt: time.Now().Add(30 * time.Minute),
	})
	_ = store.CreateSecurityEngagement(context.Background(), &pgstore.SecurityEngagement{
		ID: "eng-other", TenantID: "some-other-tenant", Technique: "confirm-http-reachable",
		ExpiresAt: time.Now().Add(30 * time.Minute),
	})

	h := securityEngagementHandler(store, &fakeSecurityFindingsStore{})
	req := httptest.NewRequest(http.MethodGet, "/admin/security-engagements", nil)
	rec := httptest.NewRecorder()
	h.HandleSecurityEngagementList(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var out []SecurityEngagementResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(out) != 1 || out[0].ID != "eng-mine" {
		t.Fatalf("out = %+v, want only the default-tenant engagement", out)
	}
}

func jsonBody(t *testing.T, v interface{}) *bytes.Reader {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	return bytes.NewReader(b)
}
