package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/gorilla/websocket"
)

// liveFrame is the wire protocol for a collector's persistent outbound
// connection (ADR 0022 Decision #7 / ROADMAP P18 use case #9) — one JSON
// object per WebSocket frame, in both directions.
type liveFrame struct {
	ID     string          `json:"id"`
	Type   string          `json:"type"` // "request" (Hub->collector) | "response" (collector->Hub)
	Tool   string          `json:"tool,omitempty"`
	Args   map[string]any  `json:"args,omitempty"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  string          `json:"error,omitempty"`
}

const (
	// liveRequestTimeout matches live_diagnostics.py's own httpx timeout
	// (15.0s) so an on-demand fetch degrades on the same rough schedule
	// whether it's answered locally or relayed to a remote cluster.
	liveRequestTimeout = 15 * time.Second
	pingPeriod         = 30 * time.Second
	pongWait           = 60 * time.Second
)

// ErrClusterNotConnected means no collector currently holds an open
// connection for the requested clusterID.
var ErrClusterNotConnected = errors.New("cluster not connected")

// ErrLiveRequestTimeout means the collector didn't answer in time — kept
// distinct from ErrClusterNotConnected so callers can report each accurately.
var ErrLiveRequestTimeout = errors.New("live request timed out")

// collectorConn wraps one cluster's persistent connection: a write-mutex-
// guarded socket (gorilla/websocket.Conn is not safe for concurrent writers)
// plus the set of requests awaiting a response, keyed by request id.
type collectorConn struct {
	conn    *websocket.Conn
	writeMu sync.Mutex
	pendMu  sync.Mutex
	pending map[string]chan liveFrame
}

func (c *collectorConn) writeFrame(f liveFrame) error {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	return c.conn.WriteJSON(f)
}

// CollectorHub tracks each connected cluster collector's persistent outbound
// connection and relays on-demand live-diagnostic requests over it — the
// Hub-side half of ADR 0022 Decision #7 ("the collector initiates and holds
// open a persistent outbound connection... periodic push and the Hub's
// on-demand requests flow over that one already-established connection").
// One connection per clusterID (Integration.ID); a reconnect replaces the
// previous entry.
type CollectorHub struct {
	mu             sync.Mutex
	conns          map[string]*collectorConn
	requestTimeout time.Duration // unexported: tests shrink this so the timeout path doesn't take 15s to exercise
}

// NewCollectorHub creates an empty hub — safe zero-collector state; every
// RequestLive call simply returns ErrClusterNotConnected until a collector
// dials in.
func NewCollectorHub() *CollectorHub {
	return &CollectorHub{conns: map[string]*collectorConn{}, requestTimeout: liveRequestTimeout}
}

// Register begins tracking clusterID's connection and runs its read loop
// until the connection closes, at which point it unregisters itself. Blocks
// the calling goroutine for the connection's lifetime — callers (the HTTP
// handler that just upgraded the request) should call this directly rather
// than backgrounding it, since the underlying request context ends when the
// handler returns. Returns the error that ended the read loop (nil is not
// possible — ReadJSON only returns on error) so the caller can log *why* a
// connection dropped, since that's otherwise invisible.
func (h *CollectorHub) Register(clusterID string, conn *websocket.Conn) error {
	cc := &collectorConn{conn: conn, pending: map[string]chan liveFrame{}}

	h.mu.Lock()
	if old, ok := h.conns[clusterID]; ok {
		old.conn.Close() // a fresh reconnect replaces a stale connection
	}
	h.conns[clusterID] = cc
	h.mu.Unlock()

	defer func() {
		h.mu.Lock()
		if h.conns[clusterID] == cc {
			delete(h.conns, clusterID)
		}
		h.mu.Unlock()
	}()

	_ = conn.SetReadDeadline(time.Now().Add(pongWait))
	conn.SetPongHandler(func(string) error {
		return conn.SetReadDeadline(time.Now().Add(pongWait))
	})

	stopPing := make(chan struct{})
	defer close(stopPing)
	go func() {
		ticker := time.NewTicker(pingPeriod)
		defer ticker.Stop()
		for {
			select {
			case <-stopPing:
				return
			case <-ticker.C:
				cc.writeMu.Lock()
				err := conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(10*time.Second))
				cc.writeMu.Unlock()
				if err != nil {
					return
				}
			}
		}
	}()

	for {
		var f liveFrame
		if err := conn.ReadJSON(&f); err != nil {
			return err // disconnect — the deferred cleanup above unregisters this connection
		}
		if f.Type != "response" {
			continue // the collector never sends "request" — ignore anything unexpected
		}
		cc.pendMu.Lock()
		ch, ok := cc.pending[f.ID]
		if ok {
			delete(cc.pending, f.ID)
		}
		cc.pendMu.Unlock()
		if ok {
			ch <- f
		}
	}
}

// RequestLive relays one on-demand live-diagnostic tool call to clusterID's
// collector over its already-open connection and waits (bounded by
// liveRequestTimeout) for the matching response.
func (h *CollectorHub) RequestLive(ctx context.Context, clusterID, tool string, args map[string]any) (json.RawMessage, error) {
	h.mu.Lock()
	cc, ok := h.conns[clusterID]
	h.mu.Unlock()
	if !ok {
		return nil, ErrClusterNotConnected
	}

	id := uuid.New().String()
	respCh := make(chan liveFrame, 1)
	cc.pendMu.Lock()
	cc.pending[id] = respCh
	cc.pendMu.Unlock()
	defer func() {
		cc.pendMu.Lock()
		delete(cc.pending, id)
		cc.pendMu.Unlock()
	}()

	if err := cc.writeFrame(liveFrame{ID: id, Type: "request", Tool: tool, Args: args}); err != nil {
		return nil, fmt.Errorf("send live request: %w", err)
	}

	timeoutCtx, cancel := context.WithTimeout(ctx, h.requestTimeout)
	defer cancel()

	select {
	case f := <-respCh:
		if f.Error != "" {
			return nil, fmt.Errorf("collector error: %s", f.Error)
		}
		return f.Result, nil
	case <-timeoutCtx.Done():
		return nil, ErrLiveRequestTimeout
	}
}
