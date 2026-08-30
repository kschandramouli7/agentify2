package api

import (
	"bytes"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

// The eval endpoint's whole reason for existing is that a prompt pin must NOT be
// reachable without authentication (ADR 0030, ADR 0020 rule 5). These tests
// cover the gate and the fact that eval-ness cannot be spoofed from the public
// route.

func TestCheckEvalAuth(t *testing.T) {
	t.Run("unset token means open, matching the collector/remediation posture", func(t *testing.T) {
		h := &Handler{evalAuthToken: ""}
		if !h.checkEvalAuth(httptest.NewRequest("POST", "/admin/eval/query", nil)) {
			t.Error("expected open when no token is configured")
		}
	})

	t.Run("correct bearer token is accepted", func(t *testing.T) {
		h := &Handler{evalAuthToken: "s3cret"}
		r := httptest.NewRequest("POST", "/admin/eval/query", nil)
		r.Header.Set("Authorization", "Bearer s3cret")
		if !h.checkEvalAuth(r) {
			t.Error("expected the matching token to be accepted")
		}
	})

	for name, header := range map[string]string{
		"missing header":     "",
		"wrong token":        "Bearer nope",
		"no Bearer prefix":   "s3cret",
		"prefix only":        "Bearer ",
		"case-wrong prefix":  "bearer s3cret",
		"token as substring": "Bearer s3cretX",
	} {
		t.Run("rejected: "+name, func(t *testing.T) {
			h := &Handler{evalAuthToken: "s3cret"}
			r := httptest.NewRequest("POST", "/admin/eval/query", nil)
			if header != "" {
				r.Header.Set("Authorization", header)
			}
			if h.checkEvalAuth(r) {
				t.Errorf("expected rejection for %q", header)
			}
		})
	}
}

func TestHandleEvalQueryRejectsUnauthorized(t *testing.T) {
	h := &Handler{evalAuthToken: "s3cret", logger: slog.Default()}
	w := httptest.NewRecorder()
	r := httptest.NewRequest("POST", "/admin/eval/query",
		bytes.NewReader([]byte(`{"question":"why is it crashing"}`)))

	h.HandleEvalQuery(w, r)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401", w.Code)
	}
}

func TestHandleEvalQueryRejectsNonPost(t *testing.T) {
	h := &Handler{logger: slog.Default()}
	w := httptest.NewRecorder()
	h.HandleEvalQuery(w, httptest.NewRequest("GET", "/admin/eval/query", nil))
	if w.Code != http.StatusMethodNotAllowed {
		t.Errorf("status = %d, want 405", w.Code)
	}
}

func TestIsEvalRequest(t *testing.T) {
	// A request through the public route must never look like eval traffic —
	// otherwise a caller could mislabel traces and have them excluded from
	// quality sampling.
	if isEvalRequest(httptest.NewRequest("POST", "/api/query", nil)) {
		t.Error("a plain request must not be marked as eval")
	}

	// Body-supplied fields must not be able to set it either.
	r := httptest.NewRequest("POST", "/api/query",
		bytes.NewReader([]byte(`{"question":"q","context":{"is_eval":true}}`)))
	if isEvalRequest(r) {
		t.Error("eval-ness must not be settable from the request body")
	}
}
