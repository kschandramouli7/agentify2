import { useState } from "react";
import { ServiceEvaluator } from "./components/ServiceEvaluator";
import { RegistryPanel } from "./components/RegistryPanel";
import { TracesPanel } from "./components/TracesPanel";
import { SyncPanel } from "./components/SyncPanel";
import { MetricsPanel } from "./components/MetricsPanel";
import { PricingPanel } from "./components/PricingPanel";
import { ChatPanel } from "./components/ChatPanel";
import { RemediationPanel } from "./components/RemediationPanel";
import { TopologyPanel } from "./components/TopologyPanel";

type Page = "observability" | "registry" | "traces" | "sync" | "metrics" | "pricing" | "chat" | "remediation" | "topology";

interface NavItem {
  id: Page;
  label: string;
  icon: string;
  description: string;
}

const MAIN_NAV: NavItem[] = [
  {
    id: "observability",
    label: "K8s Observability",
    icon: "◎",
    description: "Diagnose services & pods",
  },
  {
    id: "chat",
    label: "Investigate",
    icon: "✦",
    description: "Multi-turn AI conversation",
  },
];

const ADMIN_NAV: NavItem[] = [
  { id: "registry",     label: "Pod Registry",    icon: "⬡", description: "Browse live pods"            },
  { id: "traces",       label: "Query History",   icon: "≡", description: "Past queries & traces"       },
  { id: "topology",     label: "Dependencies",    icon: "⇄", description: "Mined service-to-service call graph (P18 #2)" },
  { id: "sync",         label: "Namespace Sync",  icon: "⟳", description: "Sync cluster namespaces"    },
  { id: "metrics",      label: "Metrics",         icon: "◈", description: "Token usage & cost"          },
  { id: "pricing",      label: "Model Pricing",   icon: "⊙", description: "$/MTok rates for all models" },
  { id: "remediation",  label: "Remediation",     icon: "⏸", description: "Proposed actions awaiting approval (ADR 0020)" },
];

const ALL_NAV = [...MAIN_NAV, ...ADMIN_NAV];

function NavButton({ item, active, onClick }: { item: NavItem; active: boolean; onClick: () => void }) {
  return (
    <button
      className={`sidebar__item${active ? " sidebar__item--active" : ""}`}
      onClick={onClick}
      title={item.description}
    >
      <span className="sidebar__icon">{item.icon}</span>
      <span className="sidebar__item-text">
        <span className="sidebar__item-label">{item.label}</span>
        <span className="sidebar__item-desc">{item.description}</span>
      </span>
    </button>
  );
}

function Sidebar({ page, onNavigate }: { page: Page; onNavigate: (p: Page) => void }) {
  return (
    <nav className="sidebar">
      <div className="sidebar__group">
        {MAIN_NAV.map(item => (
          <NavButton key={item.id} item={item} active={page === item.id} onClick={() => onNavigate(item.id)} />
        ))}
      </div>

      <div className="sidebar__divider">
        <span className="sidebar__divider-label">Admin</span>
      </div>

      <div className="sidebar__group">
        {ADMIN_NAV.map(item => (
          <NavButton key={item.id} item={item} active={page === item.id} onClick={() => onNavigate(item.id)} />
        ))}
      </div>

      <div className="sidebar__footer">
        <span className="sidebar__footer-text">agentify</span>
        <span className="sidebar__footer-env">dev</span>
      </div>
    </nav>
  );
}

function PageHeader({ page }: { page: Page }) {
  const item = ALL_NAV.find(n => n.id === page);
  if (!item) return null;
  return (
    <div className="page-header">
      <span className="page-header__icon">{item.icon}</span>
      <div className="page-header__text">
        <h2 className="page-header__title">{item.label}</h2>
        <p className="page-header__desc">{item.description}</p>
      </div>
    </div>
  );
}

export function App() {
  const [page, setPage] = useState<Page>("observability");

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          <span className="app__brand-icon">⬡</span>
          <span className="app__brand-name">agentify</span>
        </div>
        <span className="app__header-divider" />
        <span className="app__subtitle">K8s Service Intelligence</span>
        <div className="app__header-spacer" />
        <span className="app__status-badge">
          <span className="app__status-dot" />
          Live
        </span>
      </header>
      <div className="app__body">
        <Sidebar page={page} onNavigate={setPage} />
        <main className={`app__content${page === "chat" ? " app__content--chat" : ""}`}>
          {page !== "chat" && <PageHeader page={page} />}
          {/* ServiceEvaluator stays mounted so search + results survive navigation */}
          <div style={{ display: page === "observability" ? "" : "none" }}>
            <ServiceEvaluator />
          </div>
          {page === "registry" && <RegistryPanel />}
          {page === "traces"   && <TracesPanel />}
          {page === "sync"     && <SyncPanel />}
          {page === "metrics"  && <MetricsPanel />}
          {page === "pricing"  && <PricingPanel />}
          {page === "chat"     && <ChatPanel />}
          {page === "remediation" && <RemediationPanel />}
          {page === "topology"    && <TopologyPanel />}
        </main>
      </div>
    </div>
  );
}
