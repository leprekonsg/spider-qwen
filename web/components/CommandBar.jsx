/* global React, SQIcon */

function CommandBar({ query, setQuery, mode, onRun, running, onPause }) {
  return (
    <div style={{
      borderBottom: "1px solid var(--sq-border)",
      background: "rgba(10,10,10,0.72)", backdropFilter: "blur(18px)",
      padding: "12px 28px",
      display: "flex", alignItems: "center", gap: 14,
      flex: "none",
      zIndex: 2, position: "relative",
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 8,
        fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
        letterSpacing: "0.12em", textTransform: "uppercase", flex: "none",
      }}>
        <SQIcon name="spider" size={14} color="var(--sq-bone)" />
        <span style={{ color: "var(--sq-smoke)" }}>spider-qwen</span>
        <span style={{ opacity: .5 }}>/</span>
        <span>query</span>
      </div>

      <form
        onSubmit={(e) => { e.preventDefault(); onRun?.(); }}
        style={{
          flex: 1, display: "flex", alignItems: "center",
          background: "var(--sq-bg-input)",
          border: "1px solid var(--sq-border-strong)",
          borderRadius: 2,
          padding: "0 12px", height: 42,
        }}
      >
        <SQIcon name="search" size={15} color="var(--sq-slate-taupe)" />
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder="Find suppliers, categories, contracts, signals…"
          className="sq-input"
        />

        <div style={{
          display: "flex", alignItems: "center", gap: 10, paddingLeft: 12, height: 28,
          borderLeft: "1px solid var(--sq-border)",
          fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-smoke)",
          letterSpacing: "0.04em",
        }}>
          <span style={{ color: "var(--sq-slate-taupe)" }}>mode</span>
          <span>{mode}</span>
          <span style={{
            padding: "2px 8px", border: "1px solid rgba(122,140,94,0.45)",
            color: "#A6BB87",
          }}>auto</span>
        </div>
      </form>

      {running ? (
        <button onClick={onPause} className="sq-btn sq-btn-risk" style={{ height: 42, padding: "0 18px", flex: "none" }}>
          <SQIcon name="pause" size={11} /> Halt run
        </button>
      ) : (
        <button onClick={onRun} className="sq-btn sq-btn-primary" style={{ height: 42, padding: "0 20px", flex: "none" }}>
          Run hunt
          <SQIcon name="arrow" size={13} stroke={1.8} />
        </button>
      )}

      <button className="sq-btn sq-btn-secondary" style={{ height: 42, padding: "0 12px", flex: "none" }} title="Filters">
        <SQIcon name="filter" size={13} />
      </button>
      <button className="sq-btn sq-btn-secondary" style={{ height: 42, padding: "0 12px", flex: "none", position: "relative" }} title="Alerts">
        <SQIcon name="bell" size={13} />
        <span style={{
          position: "absolute", top: 8, right: 10,
          width: 6, height: 6, borderRadius: 999, background: "#B65C4A",
          boxShadow: "0 0 0 1.5px var(--sq-obsidian)",
        }}/>
      </button>
    </div>
  );
}

window.CommandBar = CommandBar;
