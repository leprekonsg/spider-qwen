/* global React, SQIcon */

function Pipeline({ activePhase, progress, running, budget = 18 }) {
  const steps = window.SQDATA.PIPELINE_STEPS;
  const activeIdx = Math.max(0, steps.findIndex(s => s.k === activePhase));
  return (
    <div style={{
      borderBottom: "1px solid var(--sq-border)",
      padding: "12px 28px",
      display: "flex", alignItems: "center", gap: 0,
      background: "var(--sq-obsidian)",
      flex: "none",
    }}>
      <div className="sq-overline" style={{ marginRight: 22, color: "var(--sq-slate-taupe)" }}>Pipeline</div>
      {steps.map((s, i) => {
        const state = !running && progress >= 1 ? "done"
                    : i < activeIdx ? "done"
                    : i === activeIdx && running ? "active"
                    : "idle";
        const colors = {
          done:   { dot: "#7A8C5E", text: "var(--sq-smoke)", line: "rgba(122,140,94,0.55)" },
          active: { dot: "var(--sq-bone)", text: "var(--sq-bone)", line: "var(--sq-border-strong)" },
          idle:   { dot: "var(--sq-border-strong)", text: "var(--sq-slate-taupe)", line: "var(--sq-border-strong)" },
        }[state];
        return (
          <React.Fragment key={s.k}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{
                width: 8, height: 8, borderRadius: 999,
                background: colors.dot,
                boxShadow: state === "active"
                  ? "0 0 0 1px rgba(242,229,230,0.25), 0 0 12px rgba(242,229,230,0.22)"
                  : "none",
                transition: "all 240ms var(--sq-ease-sharp)",
              }} className={state === "active" ? "sq-pulse" : ""}/>
              <span style={{
                fontFamily: "var(--sq-font-mono)", fontSize: 11, letterSpacing: "0.06em",
                color: colors.text, textTransform: "lowercase",
              }}>{s.label}</span>
            </div>
            {i < steps.length - 1 && (
              <span style={{
                flex: 1, height: 1, margin: "0 10px",
                background: `linear-gradient(90deg, ${colors.line} 0%, var(--sq-border-strong) 100%)`,
                opacity: 0.85,
              }}/>
            )}
          </React.Fragment>
        );
      })}
      <div style={{
        marginLeft: 18, fontFamily: "var(--sq-font-mono)", fontSize: 11,
        color: "var(--sq-slate-taupe)", letterSpacing: "0.06em",
        display: "flex", gap: 18,
      }}>
        <span>budget · {Math.min(Math.round(progress * budget), budget)}/{budget}</span>
        <span>fetches · {Math.min(Math.round(progress * 14), 14)}</span>
      </div>
    </div>
  );
}

window.Pipeline = Pipeline;
