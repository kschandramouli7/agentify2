import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createChatSession, getChatSession, listChatSessions, sendChatMessage, type ChatSession } from "../api";
import { Bubble, relTime } from "./ChatPanel";

// A docked chat panel for the Dependencies view (ROADMAP P29) — same
// interface style as the Investigate page (ChatPanel.tsx), but scoped to
// one namespace/focus and without a session-list sidebar: this is a single
// running conversation for the current view, not a multi-conversation
// manager. Remounted by the caller (key={namespace}) on namespace change,
// so a fresh conversation starts rather than carrying stale context forward.
//
// Two things it answers, both through the SAME free-form chat send path:
//   - "trace <trace-id-or-METHOD-/path>" — an on-demand cross-cluster log
//     search for one specific call (trace_search.py), rendered as a
//     sequence diagram via DiagnosisReport's call_trace section.
//   - anything else — a normal question, scoped to this namespace (and, if
//     one is focused, this service) via the session's own namespace/service
//     fields, which reason_chat's context threads into every deterministic
//     route and every model call for this session.
export function DependencyChatPanel({
  namespace, focus, initialSessionId,
}: {
  namespace: string;
  focus: string | null;
  // Set by the standalone tab (see main.tsx) to resume the conversation the
  // docked panel already had going, rather than starting a blank one.
  initialSessionId?: string | null;
}) {
  const qc = useQueryClient();
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId ?? null);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const { data: session } = useQuery<ChatSession>({
    queryKey: ["dependencyChatSession", sessionId],
    queryFn: () => getChatSession(sessionId!),
    enabled: !!sessionId,
  });

  // ROADMAP P33 (ADR 0044): the tenant's 20 most recently active sessions,
  // across every namespace -- deliberately unfiltered (resolved design
  // question #3, spec 013). Shares ChatPanel.tsx's "chatSessions" query key
  // so the two views don't double-fetch the same list.
  const { data: recentSessions = [] } = useQuery<ChatSession[]>({
    queryKey: ["chatSessions"],
    queryFn: listChatSessions,
    enabled: showHistory,
    staleTime: 10_000,
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [session?.messages?.length, pending]);

  async function handleSend(text?: string) {
    const content = (text ?? input).trim();
    if (!content || pending) return;
    setInput("");
    setSendError(null);
    setPending(true);
    try {
      let sid = sessionId;
      if (!sid) {
        const s = await createChatSession({ namespace, service: focus ?? undefined });
        sid = s.id;
        setSessionId(sid);
      }
      await sendChatMessage(sid, content);
      qc.invalidateQueries({ queryKey: ["dependencyChatSession", sid] });
    } catch (e) {
      setSendError(e instanceof Error ? e.message : "Failed to send message.");
    } finally {
      setPending(false);
      inputRef.current?.focus();
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  // Opens the SAME conversation in its own tab — same namespace/focus, and
  // the session id too once one exists, so the new tab resumes rather than
  // starting a second, disconnected conversation. Before a session exists
  // (no message sent yet) the new tab just starts fresh with the same scope.
  function openInNewTab() {
    const url = new URL(window.location.href);
    url.search = "";
    url.searchParams.set("view", "dependency-chat");
    url.searchParams.set("namespace", namespace);
    if (focus) url.searchParams.set("focus", focus);
    if (sessionId) url.searchParams.set("session", sessionId);
    window.open(url.toString(), "_blank", "noopener,noreferrer");
  }

  const messages = session?.messages ?? [];
  // "trace GET /health", not "POST /charge": these are the actual routes the
  // namespace's own services expose (a plain nginx catch-all + /health) —
  // an invented path would be a guaranteed dead end for anyone who tries it.
  const examples = [
    "trace GET /health",
    focus ? `What does ${focus} depend on?` : "What are the entry points in this namespace?",
    focus ? `Is ${focus} healthy right now?` : "Which services here are unhealthy?",
  ];

  return (
    <div className="topo-chat">
      <div className="topo-chat__header">
        <span className="topo-chat__header-icon" aria-hidden="true">✦</span>
        <span className="topo-chat__header-title">
          Ask about {focus ?? (namespace || "this namespace")}
        </span>
        <button
          type="button"
          className={`topo-chat__expand${showHistory ? " topo-chat__expand--active" : ""}`}
          onClick={() => setShowHistory(v => !v)}
          title="Recent conversations"
          aria-label="Recent conversations"
          aria-expanded={showHistory}
        >
          ≡
        </button>
        <button
          type="button"
          className="topo-chat__expand"
          onClick={openInNewTab}
          title="Open this conversation in a new tab"
          aria-label="Open this conversation in a new tab"
        >
          ⤢
        </button>
      </div>

      {showHistory && (
        <div className="topo-chat__history">
          <div className="chat-session-list">
            {recentSessions.length === 0 && (
              <p className="chat-empty-sessions">No conversations yet.</p>
            )}
            {recentSessions.map(s => (
              <div
                key={s.id}
                className={`chat-session-item${s.id === sessionId ? " chat-session-item--active" : ""}`}
                onClick={() => { setSessionId(s.id); setShowHistory(false); }}
              >
                <span className="chat-session-title" title={s.title || "New conversation"}>
                  {s.title || "New conversation"}
                </span>
                <span className="chat-session-meta">
                  {s.namespace || "—"} · {relTime(s.last_active)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="topo-chat__thread">
        {messages.length === 0 && (
          <div className="topo-chat__welcome">
            <p>
              Search a specific call — paste a <strong>trace ID</strong> or a request like{" "}
              <code>GET /health</code> — or ask a question about{" "}
              {focus ? <strong>{focus}</strong> : "this namespace"}'s dependencies.
            </p>
            <div className="topo-chat__examples-label">Try asking</div>
            <div className="topo-chat__examples">
              {examples.map(ex => (
                <button
                  key={ex}
                  type="button"
                  className="chat-example"
                  onClick={() => { setInput(ex); inputRef.current?.focus(); }}
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) => <Bubble key={i} msg={m} />)}

        {pending && (
          <div className="chat-bubble chat-bubble--assistant">
            <div className="chat-bubble__role">K8fy</div>
            <div className="chat-typing"><span /><span /><span /></div>
          </div>
        )}

        {sendError && <p className="chat-error">{sendError}</p>}

        <div ref={bottomRef} />
      </div>

      <div className="chat-input-row">
        <textarea
          ref={inputRef}
          className="chat-input"
          rows={2}
          placeholder="trace <id or GET /path>, or ask a question…"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={pending}
        />
        <button
          className="chat-send-btn"
          type="button"
          onClick={() => handleSend()}
          disabled={!input.trim() || pending}
          title="Send (Enter)"
        >
          {pending ? <span className="chat-send-spinner" /> : "↑"}
        </button>
      </div>
    </div>
  );
}
