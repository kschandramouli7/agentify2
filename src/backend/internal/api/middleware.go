package api

import (
	"bufio"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"time"

	"github.com/chan/agentify/backend/internal/telemetry"
)

// statusRecorder captures the response status code for logging + metrics.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// Hijack passes through to the underlying ResponseWriter's http.Hijacker.
// Wrapping http.ResponseWriter in a struct — even via embedding — does not
// promote Hijack(): http.ResponseWriter's interface doesn't declare it, so
// only the wrapper's own explicit methods are visible to a type assertion
// against http.Hijacker. Without this, every request through this
// middleware silently loses the ability to hijack the connection, breaking
// any WebSocket upgrade (HandleCollectorConnect's gorilla/websocket.Upgrade)
// with "response does not implement http.Hijacker" — confirmed live
// (2026-08-29) via the first real Discovery collector connection attempt
// against this middleware.
func (r *statusRecorder) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	hj, ok := r.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, fmt.Errorf("underlying ResponseWriter does not support hijacking")
	}
	return hj.Hijack()
}

// Middleware wraps the HTTP handler with logging and metrics.
func NewMiddleware(next http.Handler, logger *slog.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		logger.Info("request started", "method", r.Method, "path", r.URL.Path)

		// TODO: add auth middleware
		// TODO: add rate limiting middleware

		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)

		// Record metrics. r.URL.Path is bounded (routes carry no path-param ids — ADR 0011).
		elapsed := time.Since(start)
		telemetry.HTTPRequestsTotal.WithLabelValues(r.Method, r.URL.Path, strconv.Itoa(rec.status)).Inc()
		telemetry.HTTPRequestDuration.WithLabelValues(r.Method, r.URL.Path).Observe(elapsed.Seconds())

		logger.Info("request completed",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rec.status,
			"latency_ms", elapsed.Milliseconds(),
		)
	})
}
