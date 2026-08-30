package api

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"net/http"
	"strings"
)

// Version-pinned prompt evaluation (ADR 0030 / ROADMAP P19 gap D1).
//
// The problem this solves: run_evals.py POSTs to /api/query, so it can only ever
// score whatever the `production` label currently points at. "Validate the
// candidate version before promoting it" — the gate-before-promote shape — was
// therefore not expressible, which is what blocked gap D.
//
// The design keeps two properties that pull against each other:
//
//   - Exercise the REAL deployed path (ADR 0019's whole point). An out-of-band
//     harness that constructs skills in-process would drop routing, tiering,
//     redaction and the backend->agent boundary out of coverage.
//   - Add nothing to the unauthenticated surface (ADR 0020 rule 5). A prompt pin
//     is a behaviour-substitution lever and does not belong on /api/query.
//
// Hence: a separate bearer-authenticated route that delegates to HandleQuery
// itself. Not a copy of it — literally the same function — because ADR 0030
// flags behavioural drift between the two as the main long-term cost. The pin
// travels in the query context, and eval-ness travels as a server-side request
// context value so it cannot be spoofed by a caller of the public route.

// evalContextKey marks a request as synthetic eval traffic. Unexported and
// carried on the request context (not the body) so /api/query callers cannot set
// it and mislabel their traces.
type evalContextKey struct{}

// isEvalRequest reports whether this request arrived via /admin/eval/query.
func isEvalRequest(r *http.Request) bool {
	v, _ := r.Context().Value(evalContextKey{}).(bool)
	return v
}

// SetEvalAuthToken configures the bearer token for POST /admin/eval/query.
//
// Set after construction rather than as another NewHandler parameter, which
// already takes sixteen.
func (h *Handler) SetEvalAuthToken(token string) {
	h.evalAuthToken = token
}

// checkEvalAuth authorises POST /admin/eval/query.
//
// An explicit in-handler check, mirroring checkRemediationAuth: an /admin/
// prefix grants nothing here. Every route passes through NewMiddleware, but that
// does request logging and metrics only and carries a "TODO: add auth
// middleware" — there is no authentication at the middleware or route layer.
// Adding it there instead would cover /api/query and the ingest path too, which
// is a larger decision than this endpoint should make.
//
// An unset token means open, matching the posture already used for
// REMEDIATION_AUTH_TOKEN and COLLECTOR_TOKEN. That is a deliberate consistency
// choice and it does mean a deployment that forgets EVAL_AUTH_TOKEN exposes the
// pin.
func (h *Handler) checkEvalAuth(r *http.Request) bool {
	if h.evalAuthToken == "" {
		return true // unauthenticated (dev only)
	}
	const prefix = "Bearer "
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, prefix) {
		return false
	}
	token := strings.TrimPrefix(auth, prefix)
	return subtle.ConstantTimeCompare([]byte(token), []byte(h.evalAuthToken)) == 1
}

// HandleEvalQuery answers a query using a pinned prompt version.
//
// Body: EvalQueryRequest. The pin is injected into the query context, which is
// forwarded verbatim to the agent, so no new field is needed on the
// backend->agent wire format.
func (h *Handler) HandleEvalQuery(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !h.checkEvalAuth(r) {
		h.logger.Warn("eval query rejected: bad or missing bearer token")
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}

	var req EvalQueryRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		h.logger.Error("failed to decode eval query request", "error", err)
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}

	pinned := req.PromptVersion > 0 || req.PromptLabel != ""
	if pinned && req.PromptName == "" {
		// Refuse rather than silently pin every prompt: an unscoped pin makes the
		// run score a mixture of one candidate and several fallbacks.
		h.logger.Warn("eval query rejected: pin given without prompt_name")
		http.Error(w, "prompt_name is required when prompt_label or prompt_version is set", http.StatusBadRequest)
		return
	}

	inner := QueryRequest{Question: req.Question, Context: req.Context}
	if inner.Context == nil {
		inner.Context = map[string]interface{}{}
	}
	if pinned {
		inner.Context["prompt_name"] = req.PromptName
		// Version wins over label when both are given: a version is unambiguous,
		// whereas a label can be moved underneath a run mid-flight.
		if req.PromptVersion > 0 {
			inner.Context["prompt_version"] = req.PromptVersion
		} else {
			inner.Context["prompt_label"] = req.PromptLabel
		}
	}

	h.logger.Info("eval query",
		"prompt_name", req.PromptName,
		"prompt_label", req.PromptLabel,
		"prompt_version", req.PromptVersion,
	)

	// Re-encode and delegate. Re-decoding the body costs one marshal per eval
	// request and buys the guarantee that the gate measures exactly the code
	// path production uses — no second implementation to drift.
	body, err := json.Marshal(inner)
	if err != nil {
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	r.Body = http.NoBody
	delegated := r.Clone(context.WithValue(r.Context(), evalContextKey{}, true))
	delegated.Body = newReadCloser(body)
	delegated.ContentLength = int64(len(body))

	h.HandleQuery(w, delegated)
}

func newReadCloser(b []byte) *readCloser {
	return &readCloser{Reader: bytes.NewReader(b)}
}

type readCloser struct{ *bytes.Reader }

func (readCloser) Close() error { return nil }
