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
