import { useState, useRef, useEffect, useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  listChatSessions, getChatSession, createChatSession,
  sendChatMessage, deleteChatSession,
  type ChatSession, type ChatMessage,
} from "../api";
import { DiagnosisReport } from "./DiagnosisReport";

// ── Helpers ───────────────────────────────────────────────────────────────────

function relTime(iso: string) {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`;
  return new Date(iso).toLocaleDateString();
}

// ── Message bubble ────────────────────────────────────────────────────────────

// Exported so DependencyChatPanel.tsx (ROADMAP P29's docked chat panel in
// the Dependencies view) can render messages identically, rather than
// reimplementing the structured-details-vs-prose branch a second time.
export function Bubble({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === "user";
  const hasStructuredDetails =
    !isUser && msg.details && Object.keys(msg.details).length > 0;
  return (
    <div className={`chat-bubble chat-bubble--${isUser ? "user" : "assistant"}`}>
      <div className="chat-bubble__role">{isUser ? "You" : "K8fy"}</div>
      {hasStructuredDetails ? (
        <DiagnosisReport details={msg.details!} />
      ) : (
        <div className="chat-bubble__content">{msg.content}</div>
      )}
    </div>
  );
}

// ── Session list sidebar ──────────────────────────────────────────────────────

function SessionList({
  sessions, activeId, onSelect, onNew, onDelete,
}: {
  sessions: ChatSession[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div className="chat-sidebar">
      <button className="chat-new-btn" onClick={onNew}>
        <span>＋</span> New conversation
      </button>
      <div className="chat-session-list">
        {sessions.length === 0 && (
          <p className="chat-empty-sessions">No conversations yet.</p>
        )}
        {sessions.map(s => (
          <div
            key={s.id}
            className={`chat-session-item${s.id === activeId ? " chat-session-item--active" : ""}`}
            onClick={() => onSelect(s.id)}
          >
            <span className="chat-session-title" title={s.title || "New conversation"}>
              {s.title || "New conversation"}
            </span>
            <span className="chat-session-meta">{relTime(s.last_active)}</span>
            <button
              className="chat-session-delete"
              title="Delete conversation"
              onClick={e => { e.stopPropagation(); onDelete(s.id); }}
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────

export function ChatPanel() {
  const qc = useQueryClient();
  const [activeId, setActiveId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef  = useRef<HTMLTextAreaElement>(null);

  // Session list (no messages — kept small)
  const { data: sessions = [] } = useQuery<ChatSession[]>({
    queryKey: ["chatSessions"],
    queryFn: listChatSessions,
    refetchInterval: 30_000,
  });

  // Active session with full message history
  const { data: activeSession } = useQuery<ChatSession>({
    queryKey: ["chatSession", activeId],
    queryFn: () => getChatSession(activeId!),
    enabled: !!activeId,
  });

  // Scroll to bottom whenever messages change
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [activeSession?.messages?.length, pending]);

  const handleNewSession = useCallback(async () => {
    const s = await createChatSession();
    setActiveId(s.id);
    qc.invalidateQueries({ queryKey: ["chatSessions"] });
    inputRef.current?.focus();
  }, [qc]);

  const handleDeleteSession = useCallback(async (id: string) => {
    await deleteChatSession(id);
    if (activeId === id) setActiveId(null);
    qc.invalidateQueries({ queryKey: ["chatSessions"] });
  }, [activeId, qc]);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || pending) return;

    let sessionId = activeId;

    // Create a session on the fly if none is active
    if (!sessionId) {
      const s = await createChatSession();
      sessionId = s.id;
      setActiveId(sessionId);
      qc.invalidateQueries({ queryKey: ["chatSessions"] });
    }

    setInput("");
    setSendError(null);
    setPending(true);

    try {
      await sendChatMessage(sessionId, text);
      qc.invalidateQueries({ queryKey: ["chatSession", sessionId] });
      qc.invalidateQueries({ queryKey: ["chatSessions"] });
    } catch (e: unknown) {
      setSendError(e instanceof Error ? e.message : "Failed to send message.");
    } finally {
      setPending(false);
      inputRef.current?.focus();
    }
  }, [activeId, input, pending, qc]);

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  const messages = activeSession?.messages ?? [];

  return (
    <div className="chat-layout">
      {/* Sidebar */}
      <SessionList
        sessions={sessions}
        activeId={activeId}
        onSelect={id => { setActiveId(id); setSendError(null); }}
        onNew={handleNewSession}
        onDelete={handleDeleteSession}
      />

      {/* Main conversation area */}
      <div className="chat-main">
        {/* Thread */}
        <div className="chat-thread">
          {!activeId && (
            <div className="chat-welcome">
              <div className="chat-welcome__icon">⬡</div>
              <h2 className="chat-welcome__title">K8fy Investigator</h2>
              <p className="chat-welcome__desc">
                Ask anything about your Kubernetes cluster.<br />
                K8fy fetches live data and reasons through incidents with you.
              </p>
              <div className="chat-welcome__examples">
                {[
                  "Why is payment-worker crashing?",
                  "Which services in staging are unhealthy?",
                  "What changed in payments before the last outage?",
                  "Are any TLS certificates expiring soon?",
                ].map(ex => (
                  <button
                    key={ex}
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
              <div className="chat-typing">
                <span /><span /><span />
              </div>
            </div>
          )}

          {sendError && (
            <p className="chat-error">{sendError}</p>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Input row */}
        <div className="chat-input-row">
          <textarea
            ref={inputRef}
            className="chat-input"
            rows={2}
            placeholder={activeId
              ? "Ask a follow-up question… (Enter to send, Shift+Enter for newline)"
              : "Ask anything about your cluster…"}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={pending}
          />
          <button
            className="chat-send-btn"
            onClick={handleSend}
            disabled={!input.trim() || pending}
            title="Send (Enter)"
          >
            {pending ? <span className="chat-send-spinner" /> : "↑"}
          </button>
        </div>
      </div>
    </div>
  );
}
