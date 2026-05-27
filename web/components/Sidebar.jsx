/* global React, SQIcon */

function Sidebar({ active, onNav, onNewRun, runState }) {
  const top = [
    { k: "hunt",      label: "Hunt",      icon: "spider" },
    { k: "shortlist", label: "Shortlist", icon: "suppliers" },
    { k: "rfq",       label: "RFQ drafts", icon: "rfq" },
    { k: "ledger",    label: "Evidence ledger", icon: "ledger" },
    { k: "signals",   label: "Signals", icon: "risks" },
  ];
  const mid = [
    { k: "contracts", label: "Contracts", icon: "contracts" },
    { k: "insights",  label: "Insights",  icon: "insights" },
  ];

  const Row = ({ k, label, icon }) => (
    <button
      onClick={() => onNav?.(k)}
      style={{
        display: "flex", alignItems: "center", gap: 12,
        width: "100%", textAlign: "left",
        height: 36, padding: "0 14px",
        background: active === k ? "rgba(242,229,230,0.06)" : "transparent",
        borderLeft: active === k ? "2px solid var(--sq-bone)" : "2px solid transparent",
        color: active === k ? "var(--sq-bone)" : "var(--sq-smoke)",
        border: "none", cursor: "pointer",
        fontFamily: "var(--sq-font-sans)", fontSize: 13,
        fontWeight: active === k ? 600 : 500,
        transition: "background 160ms var(--sq-ease-sharp), color 160ms var(--sq-ease-sharp)",
      }}
    >
      <SQIcon name={icon} size={15} stroke={1.5} />
      <span>{label}</span>
    </button>
  );

  return (
    <aside style={{
      width: 232, flex: "none",
      background: "var(--sq-deep-shadow)",
      borderRight: "1px solid var(--sq-border)",
      display: "flex", flexDirection: "column",
      position: "relative", zIndex: 2,
    }}>
      {/* Wordmark */}
      <div style={{ padding: "20px 18px 18px", borderBottom: "1px solid var(--sq-border)" }}>
        <img src="assets/logo-darkmode.png" alt="Spider-Qwen" style={{ height: 22, display: "block", marginBottom: 12 }} />
        <div className="sq-overline" style={{ color: "var(--sq-slate-taupe)" }}>
          Workspace · v1.4
        </div>
      </div>

      {/* New run CTA */}
      <div style={{ padding: "14px 14px 8px" }}>
        <button onClick={onNewRun} className="sq-btn sq-btn-secondary" style={{ width: "100%", height: 36, justifyContent: "space-between" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <SQIcon name="plus" size={13} stroke={2}/> New hunt
          </span>
          <kbd style={{
            fontFamily: "var(--sq-font-mono)", fontSize: 10, color: "var(--sq-slate-taupe)",
            letterSpacing: "0.04em", padding: "2px 6px", border: "1px solid var(--sq-border-strong)",
            borderRadius: 2,
          }}>⌘K</kbd>
        </button>
      </div>

      <nav style={{ padding: "8px 0", display: "flex", flexDirection: "column", gap: 2 }}>
        {top.map(r => <Row key={r.k} {...r} />)}
      </nav>

      <div style={{ margin: "8px 14px", borderTop: "1px solid var(--sq-border)" }} />

      <nav style={{ padding: "0 0 6px", display: "flex", flexDirection: "column", gap: 2 }}>
        {mid.map(r => <Row key={r.k} {...r} />)}
      </nav>

      {/* Run state footer */}
      <div style={{ marginTop: "auto", padding: "16px 18px", borderTop: "1px solid var(--sq-border)" }}>
        <div className="sq-overline" style={{ marginBottom: 8, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span>Run state</span>
          <span style={{
            display: "inline-flex", alignItems: "center", gap: 6,
            color: runState === "running" ? "#A6BB87" : "var(--sq-slate-taupe)",
            fontSize: 9,
          }}>
            <span className={runState === "running" ? "sq-pulse" : ""} style={{
              width: 5, height: 5, borderRadius: 999,
              background: runState === "running" ? "#A6BB87" : "var(--sq-slate-taupe)",
            }} />
            {runState === "running" ? "LIVE" : "IDLE"}
          </span>
        </div>
        <div style={{ fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-smoke)", lineHeight: 1.85 }}>
          <Row2 k="provider" v="tinyfish.qwen-3" />
          <Row2 k="mode"     v="service_quote" />
          <Row2 k="geo"      v="SEA-first" />
          <Row2 k="ledger"   v="ev_001 … ev_014" />
        </div>
      </div>

      {/* Operator pill */}
      <div style={{ padding: "12px 18px", borderTop: "1px solid var(--sq-border)", display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{
          width: 28, height: 28, borderRadius: 2, background: "var(--sq-graphite)",
          border: "1px solid var(--sq-border-strong)",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontFamily: "var(--sq-font-serif)", fontSize: 13, color: "var(--sq-bone)",
        }}>A</div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 12, color: "var(--sq-bone)", fontWeight: 600 }}>Aria Chen</div>
          <div style={{ fontFamily: "var(--sq-font-mono)", fontSize: 10, color: "var(--sq-slate-taupe)", letterSpacing: "0.04em" }}>
            head · strategic sourcing
          </div>
        </div>
      </div>
    </aside>
  );
}

function Row2({ k, v }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
      <span style={{ color: "var(--sq-slate-taupe)" }}>{k}</span>
      <span style={{ color: "var(--sq-smoke)" }}>{v}</span>
    </div>
  );
}

window.Sidebar = Sidebar;
