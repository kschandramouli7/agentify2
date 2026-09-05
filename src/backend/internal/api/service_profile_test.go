package api

import (
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	pgstore "github.com/chan/agentify/backend/internal/storage/postgres"
)

func profileHandler(store *fakeClusterServiceStore) *Handler {
	return &Handler{
		clusterServiceStore: store,
		integrationStore:    &fakeIntegrationStore{},
		logger:              slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

func getProfiles(t *testing.T, h *Handler, q string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/api/service-profiles"+q, nil)
	rec := httptest.NewRecorder()
	h.HandleServiceProfileList(rec, req)
	return rec
}

func TestServiceProfileList_ReturnsWhatEachServiceIs(t *testing.T) {
	two := 2
	zero := 0
	store := &fakeClusterServiceStore{profiles: map[string][]pgstore.ServiceProfile{
		"payments": {{
			Namespace: "payments", Service: "payment-api",
			ServiceType: "ClusterIP", WorkloadKind: "Deployment",
			ReplicasDesired: &two, ReplicasReady: &zero,
			Image: "nginx:1.25",
			Ports: []pgstore.ServicePort{{Name: "https", Port: 8443, Protocol: "TCP"}},
		}},
	}}
	rec := getProfiles(t, profileHandler(store), "?namespace=payments")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var out []pgstore.ServiceProfile
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(out) != 1 {
		t.Fatalf("got %d profiles, want 1", len(out))
	}
	p := out[0]
	if p.WorkloadKind != "Deployment" || p.Image != "nginx:1.25" {
		t.Errorf("profile = %+v", p)
	}
	// 0 ready of 2 desired is the OPS-5 shape. It must survive JSON as 0, not
	// as null and not as omitted — the diagram renders it as a finding.
	if p.ReplicasReady == nil || *p.ReplicasReady != 0 {
		t.Errorf("replicas_ready = %v, want 0", p.ReplicasReady)
	}
	if len(p.Ports) != 1 || p.Ports[0].Port != 8443 {
		t.Errorf("ports = %+v", p.Ports)
	}
}

// Distinguishing "scaled to zero" from "not reported" only works if null
// survives the wire as null.
func TestServiceProfileList_UnknownReplicasStayNull(t *testing.T) {
	store := &fakeClusterServiceStore{profiles: map[string][]pgstore.ServiceProfile{
		"payments": {{Namespace: "payments", Service: "batch", WorkloadKind: "CronJob", Schedule: "*/30 * * * *"}},
	}}
	rec := getProfiles(t, profileHandler(store), "?namespace=payments")
	var raw []map[string]interface{}
	json.Unmarshal(rec.Body.Bytes(), &raw)
	if len(raw) != 1 {
		t.Fatalf("got %d rows", len(raw))
	}
	if v, ok := raw[0]["replicas_ready"]; !ok || v != nil {
		t.Errorf("replicas_ready = %v (present=%v), want explicit null", v, ok)
	}
	if raw[0]["schedule"] != "*/30 * * * *" {
		t.Errorf("schedule = %v", raw[0]["schedule"])
	}
}

func TestServiceProfileList_RequiresNamespace(t *testing.T) {
	if rec := getProfiles(t, profileHandler(&fakeClusterServiceStore{}), ""); rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

// A profile failure must not blank the panel — the dependency graph is still
// worth drawing without it.
func TestServiceProfileList_StoreErrorDegradesToEmptyArray(t *testing.T) {
	store := &fakeClusterServiceStore{profileErr: errors.New("db down")}
	rec := getProfiles(t, profileHandler(store), "?namespace=payments")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := string(rec.Body.Bytes()); got != "[]\n" && got != "[]" {
		t.Errorf("body = %q, want []", got)
	}
}

func TestServiceProfileList_EmptyIsAnArrayNotNull(t *testing.T) {
	rec := getProfiles(t, profileHandler(&fakeClusterServiceStore{}), "?namespace=nothing")
	if got := string(rec.Body.Bytes()); got != "[]\n" && got != "[]" {
		t.Errorf("body = %q, want []", got)
	}
}

// ── per-service health (ROADMAP P22) ────────────────────────────────────────

func healthHandler(store *fakeClusterHealthStore) *Handler {
	return &Handler{
		clusterHealthStore: store,
		integrationStore:   &fakeIntegrationStore{},
		logger:             slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

func getHealth(t *testing.T, h *Handler, q string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/api/service-health"+q, nil)
	rec := httptest.NewRecorder()
	h.HandleServiceHealthList(rec, req)
	return rec
}

func TestServiceHealthList_ReturnsPerServiceState(t *testing.T) {
	store := &fakeClusterHealthStore{serviceHealth: map[string][]pgstore.ServiceHealth{
		"payments": {
			{Namespace: "payments", Service: "payment-api", Pods: 2, Ready: 0, Restarts: 47,
				Phases: []string{"CrashLoopBackOff"}},
			{Namespace: "payments", Service: "payment", Pods: 1, Ready: 1, Restarts: 0,
				Phases: []string{"Running"}},
		},
	}}
	rec := getHealth(t, healthHandler(store), "?namespace=payments")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var out []pgstore.ServiceHealth
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(out) != 2 {
		t.Fatalf("got %d rows, want 2", len(out))
	}
	// The shape that matters: 0 ready of 2 with a failure phase is the whole
	// point of this endpoint existing.
	if out[0].Ready != 0 || out[0].Pods != 2 || out[0].Restarts != 47 {
		t.Errorf("payment-api = %+v", out[0])
	}
	if len(out[0].Phases) != 1 || out[0].Phases[0] != "CrashLoopBackOff" {
		t.Errorf("phases = %v", out[0].Phases)
	}
}

func TestServiceHealthList_RequiresNamespace(t *testing.T) {
	if rec := getHealth(t, healthHandler(&fakeClusterHealthStore{}), ""); rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

// Structure without state is still worth drawing, so a health failure must not
// fail the panel.
func TestServiceHealthList_ErrorDegradesToEmptyArray(t *testing.T) {
	store := &fakeClusterHealthStore{serviceHealthErr: errors.New("db down")}
	rec := getHealth(t, healthHandler(store), "?namespace=payments")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := string(rec.Body.Bytes()); got != "[]\n" && got != "[]" {
		t.Errorf("body = %q, want []", got)
	}
}

func TestServiceHealthList_EmptyIsAnArrayNotNull(t *testing.T) {
	rec := getHealth(t, healthHandler(&fakeClusterHealthStore{}), "?namespace=nothing")
	if got := string(rec.Body.Bytes()); got != "[]\n" && got != "[]" {
		t.Errorf("body = %q, want []", got)
	}
}

// TestServiceHealthList_DoesNotCountDeletedPods is the regression test for the
// numbers a real cluster produced on 2026-09-05.
//
// current_state never forgets a pod: a DELETED watch event is upserted like any
// other, so after ten rollouts a 1-replica Deployment had ten rows. The health
// query counted all of them and the UI rendered "9/1 · 1 not ready" for a
// service whose single pod was Running 1/1.
//
// The store is faked here, so what this pins is the CONTRACT the query must
// satisfy: rows for deleted pods must never reach the caller. The predicate
// itself is exercised by the real-Postgres suite.
func TestServiceHealthList_DoesNotCountDeletedPods(t *testing.T) {
	store := &fakeClusterHealthStore{serviceHealth: map[string][]pgstore.ServiceHealth{
		// What a correct query returns for the observed cluster: one live pod.
		"agentify": {{Namespace: "agentify", Service: "agentify-agent", Pods: 1, Ready: 1,
			Restarts: 0, Phases: []string{"Running"}}},
	}}
	rec := getHealth(t, healthHandler(store), "?namespace=agentify")
	var out []pgstore.ServiceHealth
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(out) != 1 {
		t.Fatalf("got %d rows, want 1", len(out))
	}
	// The shape that was wrong: ready must never exceed pods, and neither may
	// exceed what the cluster actually runs.
	if out[0].Ready > out[0].Pods {
		t.Errorf("ready=%d exceeds pods=%d — the query is counting rows for pods that no longer exist",
			out[0].Ready, out[0].Pods)
	}
}
