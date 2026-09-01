package api

import (
	"bytes"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// The eval endpoint's whole reason for existing is that a prompt pin must NOT be
// reachable without authentication (ADR 0030, ADR 0020 rule 5). These tests
// cover the gate and the fact that eval-ness cannot be spoofed from the public
// route.

func TestCheckEvalAuth(t *testing.T) {
	// An empty token FAILS CLOSED outside dev. The deployed cluster ran with the
	// token unset and ENV=prod, so "empty means open" left the prompt-pin lever
	// reachable by anyone who could reach the backend.
	t.Run("unset token is open in dev only", func(t *testing.T) {
		for _, env := range []string{"dev", ""} {
			h := &Handler{evalAuthToken: "", evalEnv: env}
			if !h.checkEvalAuth(httptest.NewRequest("POST", "/admin/eval/query", nil)) {
				t.Errorf("env=%q: expected open for local development", env)
			}
		}
	})

	t.Run("unset token is DISABLED outside dev", func(t *testing.T) {
		for _, env := range []string{"prod", "staging", "production", "PROD"} {
			h := &Handler{evalAuthToken: "", evalEnv: strings.ToLower(env)}
			if h.checkEvalAuth(httptest.NewRequest("POST", "/admin/eval/query", nil)) {
				t.Errorf("env=%q: an unset token must not open the endpoint", env)
			}
		}
	})

	t.Run("a configured token works regardless of env", func(t *testing.T) {
		h := &Handler{evalAuthToken: "s3cret", evalEnv: "prod"}
		r := httptest.NewRequest("POST", "/admin/eval/query", nil)
		r.Header.Set("Authorization", "Bearer s3cret")
		if !h.checkEvalAuth(r) {
			t.Error("a correct token must be accepted in prod")
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
			h := &Handler{evalAuthToken: "s3cret", evalEnv: "prod"}
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

func TestHandleEvalQueryDisabledWhenTokenUnsetOutsideDev(t *testing.T) {
	// 503 rather than 401: the caller's credential is not the problem, the
	// deployment's configuration is. Conflating them sends an operator chasing a
	// token issue that does not exist.
	h := &Handler{evalAuthToken: "", evalEnv: "prod", logger: slog.Default()}
	w := httptest.NewRecorder()
	h.HandleEvalQuery(w, httptest.NewRequest("POST", "/admin/eval/query",
		bytes.NewReader([]byte(`{"question":"q"}`))))
	if w.Code != http.StatusServiceUnavailable {
		t.Errorf("status = %d, want 503", w.Code)
	}
}

func TestHandleEvalQueryRejectsUnauthorized(t *testing.T) {
	h := &Handler{evalAuthToken: "s3cret", evalEnv: "prod", logger: slog.Default()}
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
