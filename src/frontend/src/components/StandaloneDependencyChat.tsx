import { DependencyChatPanel } from "./DependencyChatPanel";

// The page main.tsx renders instead of <App/> when the URL carries
// ?view=dependency-chat — what DependencyChatPanel's expand button
// (topo-chat__expand) opens in a new tab. This app has no router (App.tsx
// is plain useState tab-switching, no history/URL involvement anywhere
// else), so a query-param check in main.tsx is the smallest way to get an
// addressable, bookmarkable, shareable URL for one docked panel without
// pulling in a router for a single page.
//
// Deliberately a thin shell around the same DependencyChatPanel the docked
// view uses — not a fork of it — so a fix or feature there (like this one)
// never needs a second copy kept in sync.
export function StandaloneDependencyChat() {
  const params = new URLSearchParams(window.location.search);
  const namespace = params.get("namespace") ?? "";
  const focus = params.get("focus") || null;
  const sessionId = params.get("session") || null;

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          <span className="app__brand-icon">⬡</span>
          <span className="app__brand-name">agentify</span>
        </div>
        <span className="app__header-divider" />
        <span className="app__subtitle">
          Dependencies — {focus ?? (namespace || "this namespace")}
        </span>
        <div className="app__header-spacer" />
        <span className="app__status-badge">
          <span className="app__status-dot" />
          Live
        </span>
      </header>
      <div className="app__body">
        <main className="app__content standalone-chat">
          <DependencyChatPanel namespace={namespace} focus={focus} initialSessionId={sessionId} />
        </main>
      </div>
    </div>
  );
}
