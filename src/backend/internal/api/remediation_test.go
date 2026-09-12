package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCheckRemediationAuth(t *testing.T) {
	// An empty token FAILS CLOSED outside dev (ADR 0020 amendment, ROADMAP
	// OPS-3) — mirrors checkEvalAuth's identical fix (eval_query_test.go).
	t.Run("unset token is open in dev only", func(t *testing.T) {
		for _, env := range []string{"dev", ""} {
			h := &Handler{remediationConfig: RemediationConfig{AuthToken: "", Env: env}}
			req := httptest.NewRequest(http.MethodPost, "/admin/remediation/x/approve", nil)
			if !h.checkRemediationAuth(req) {
				t.Errorf("env=%q: expected open for local development", env)
			}
		}
	})

	t.Run("unset token is DISABLED outside dev", func(t *testing.T) {
		for _, env := range []string{"prod", "staging", "production", "PROD"} {
			h := &Handler{remediationConfig: RemediationConfig{AuthToken: "", Env: strings.ToLower(env)}}
			req := httptest.NewRequest(http.MethodPost, "/admin/remediation/x/approve", nil)
			if h.checkRemediationAuth(req) {
				t.Errorf("env=%q: an unset token must not open approve/reject", env)
			}
		}
	})

	t.Run("a configured token works regardless of env", func(t *testing.T) {
		h := &Handler{remediationConfig: RemediationConfig{AuthToken: "s3cret", Env: "prod"}}
		req := httptest.NewRequest(http.MethodPost, "/admin/remediation/x/approve", nil)
		req.Header.Set("Authorization", "Bearer s3cret")
		if !h.checkRemediationAuth(req) {
			t.Error("a correct token must be accepted in prod")
		}
	})

	t.Run("correct bearer token passes", func(t *testing.T) {
		h := &Handler{remediationConfig: RemediationConfig{AuthToken: "s3cret"}}
		req := httptest.NewRequest(http.MethodPost, "/admin/remediation/x/approve", nil)
		req.Header.Set("Authorization", "Bearer s3cret")
		if !h.checkRemediationAuth(req) {
			t.Fatal("expected auth to pass with the correct token")
		}
	})

	t.Run("wrong token fails", func(t *testing.T) {
		h := &Handler{remediationConfig: RemediationConfig{AuthToken: "s3cret"}}
		req := httptest.NewRequest(http.MethodPost, "/admin/remediation/x/approve", nil)
		req.Header.Set("Authorization", "Bearer wrong")
		if h.checkRemediationAuth(req) {
			t.Fatal("expected auth to fail with the wrong token")
		}
	})

	t.Run("missing header fails when token configured", func(t *testing.T) {
		h := &Handler{remediationConfig: RemediationConfig{AuthToken: "s3cret"}}
		req := httptest.NewRequest(http.MethodPost, "/admin/remediation/x/approve", nil)
		if h.checkRemediationAuth(req) {
			t.Fatal("expected auth to fail with no Authorization header")
		}
	})

	t.Run("malformed header (no Bearer prefix) fails", func(t *testing.T) {
		h := &Handler{remediationConfig: RemediationConfig{AuthToken: "s3cret"}}
		req := httptest.NewRequest(http.MethodPost, "/admin/remediation/x/approve", nil)
		req.Header.Set("Authorization", "s3cret")
		if h.checkRemediationAuth(req) {
			t.Fatal("expected auth to fail without the Bearer prefix")
		}
	})
}
