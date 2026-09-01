import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { askQuery, type QueryResponse } from "../api";
import { DependencyFlow } from "./DependencyFlow";

export function AskPanel() {
  const [question, setQuestion] = useState("Is the payment service healthy?");
  const [namespace, setNamespace] = useState("prod");
  // Focus starts wherever the answer put it (the service named in the question)
  // and is then the reader's to move.
  const [focus, setFocus] = useState<string | null>(null);

  const mutation = useMutation<QueryResponse, Error, void>({
    mutationFn: () => askQuery(question, { namespace }),
    // A new answer carries its own focus; keeping the previous one would point
    // at a service the new graph may not contain.
    onSuccess: (data) => setFocus(data.details?.service_graph?.focus ?? null),
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (question.trim()) mutation.mutate();
  }

  const resp = mutation.data;

  return (
    <section className="panel">
      <h2>Ask</h2>
      <form className="ask-form" onSubmit={onSubmit}>
        <input
          className="ask-form__q"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask about service / pod health, certs…"
          aria-label="Question"
        />
        <input
          className="ask-form__ns"
          value={namespace}
          onChange={(e) => setNamespace(e.target.value)}
          placeholder="namespace"
          aria-label="Namespace"
        />
        <button type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? "Asking…" : "Ask"}
        </button>
      </form>

      {mutation.isError && <p className="error">Error: {mutation.error.message}</p>}

      {resp && (
        <div className="answer">
          <div className="answer__row">
            <span className={`badge badge--${resp.status}`}>{resp.status}</span>
            <span className="answer__confidence">
              confidence {(resp.confidence * 100).toFixed(0)}%
            </span>
          </div>
          <p className="answer__text">{resp.answer}</p>

          {(resp.details?.service_graph?.dependencies ?? []).length > 0 && (
            <DependencyFlow
              edges={resp.details!.service_graph!.dependencies}
              focus={focus}
              onFocus={setFocus}
            />
          )}
          {resp.sources.length > 0 && (
            <p className="answer__sources">
              sources:{" "}
              {resp.sources.map((s) => (
                <code key={s}>{s}</code>
              ))}
            </p>
          )}
          {resp.trace_id && (
            <p className="answer__trace">
              trace_id: <code>{resp.trace_id}</code>
              {/* Worth surfacing: tier1 means this answer cost nothing and
                  involved no model, which is the whole point of routing
                  structured intents deterministically (ADR 0006). */}
              {resp.tier && <> · tier <code>{resp.tier}</code></>}
              {resp.intent && <> · intent <code>{resp.intent}</code></>}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
