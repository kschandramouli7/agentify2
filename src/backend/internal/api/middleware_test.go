package api

import (
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

// TestStatusRecorderSupportsHijack guards against a real regression found
// live (2026-08-29): wrapping http.ResponseWriter in statusRecorder without
// an explicit Hijack() passthrough silently breaks any WebSocket upgrade
// routed through NewMiddleware — gorilla/websocket.Upgrade needs the
// ResponseWriter to satisfy http.Hijacker, and embedding
// http.ResponseWriter alone does not promote that method (it's not part of
// the http.ResponseWriter interface, so a type assertion against
// http.Hijacker on the wrapper only sees what the wrapper itself declares).
func TestStatusRecorderSupportsHijack(t *testing.T) {
	hijacked := false

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hj, ok := w.(http.Hijacker)
		if !ok {
			t.Fatal("statusRecorder does not implement http.Hijacker")
		}
		conn, _, err := hj.Hijack()
		if err != nil {
			t.Fatalf("Hijack() failed: %v", err)
		}
		hijacked = true
		conn.Close()
	})

	logger := slog.New(slog.DiscardHandler)
	srv := httptest.NewServer(NewMiddleware(handler, logger))
	defer srv.Close()

	resp, err := http.Get(srv.URL)
	// A closed hijacked connection with no response written is expected to
	// surface as a client-side read error, not a clean HTTP response — the
	// point of this test is that Hijack() itself succeeded server-side.
	if err == nil {
		resp.Body.Close()
	}
	if !hijacked {
		t.Fatal("handler never reached the point of confirming a successful hijack")
	}
}
