/* global React, SQIcon */

// The "agent in motion" screen — shown DURING a hunt.
// Left:  the agent's reasoning stream (token-by-token feel)
// Right: evidence cards materialising as they're verified
// Bottom: a candidate map building up

function HuntInProgress({ stream, evidence, candidates, query, activePhase, progress }) {
  const streamRef = React.useRef(null);

  React.useEffect(() => {
    if (streamRef.current) {
      streamRef.current.scrollTop = streamRef.current.scrollHeight;
    }
  }, [stream.length]);

  return (
    <div style={{
      flex: 1, minWidth: 0, overflow: "hidden", display: "flex", flexDirection: "column",
      position: "relative",
      background: "var(--sq-obsidian)",
    }}>
      <WebMotif opacity={0.5} />

      {/* Title strip */}
      <div style={{
        position: "relative",
        padding: "20px 28px 18px",
        borderBottom: "1px solid var(--sq-border)",
        display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 24,
      }}>
        <div>
          <div className="sq-overline" style={{ marginBottom: 8 }}>
            <span style={{
              color: "#A6BB87", display: "inline-flex", alignItems: "center", gap: 6, marginRight: 12,
            }}>
              <span className="sq-pulse" style={{ width: 6, height: 6, borderRadius: 999, background: "#A6BB87" }} />
              Hunt in progress
            </span>
            <span style={{ color: "var(--sq-slate-taupe)" }}>· run_{Date.now().toString(36).slice(-6)}</span>
          </div>
          <h2 style={{
            margin: 0, fontFamily: "var(--sq-font-serif)", fontSize: 30, fontWeight: 500,
            color: "var(--sq-bone)", letterSpacing: "-0.01em",
          }}>
            {query}<span className="sq-caret"/>
          </h2>
        </div>
        <div style={{
          fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
          letterSpacing: "0.06em", textAlign: "right", lineHeight: 1.8,
        }}>
          <div>phase · <span style={{ color: "var(--sq-bone)" }}>{activePhase}</span></div>
          <div>completeness · <span style={{ color: "var(--sq-bone)" }}>{progress.toFixed(2)}</span></div>
        </div>
      </div>

      <div style={{ position: "relative", flex: 1, minHeight: 0, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 0 }}>
        {/* LEFT: Reasoning trace */}
        <section style={{
          borderRight: "1px solid var(--sq-border)",
          padding: "22px 26px",
          overflow: "hidden",
          display: "flex", flexDirection: "column",
          background: "linear-gradient(180deg, transparent, rgba(0,0,0,0.2))",
        }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
            <span className="sq-overline">Reasoning trace</span>
            <span className="sq-mono-chip">qwen-3 · streaming</span>
          </div>
          <div ref={streamRef} style={{
            flex: 1, minHeight: 0, overflow: "auto",
            fontFamily: "var(--sq-font-mono)", fontSize: 12, lineHeight: 1.7,
            color: "var(--sq-fog)",
            paddingRight: 12,
          }}>
            {stream.map((s, i) => (
              <div key={i} className="sq-in-up" style={{
                display: "grid", gridTemplateColumns: "70px 1fr",
                gap: 12, padding: "8px 0",
                borderBottom: "1px dashed var(--sq-border)",
              }}>
                <span style={{ color: "var(--sq-slate-taupe)" }}>
                  {String(s.t).padStart(4, "0")}ms
                </span>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <span style={{
                    fontFamily: "var(--sq-font-mono)", fontSize: 9, letterSpacing: "0.14em",
                    textTransform: "uppercase", color: phaseColor(s.phase),
                  }}>
                    {s.phase}
                  </span>
                  <span style={{ color: i === stream.length - 1 ? "var(--sq-bone)" : "var(--sq-smoke)" }}>
                    {s.text}
                    {i === stream.length - 1 && <span className="sq-caret"/>}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* RIGHT: Evidence stream */}
        <section style={{
          padding: "22px 26px",
          overflow: "hidden",
          display: "flex", flexDirection: "column",
        }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
            <span className="sq-overline">Evidence stream</span>
            <span className="sq-mono-chip" style={{ color: "#A6BB87", borderColor: "rgba(122,140,94,0.5)" }}>
              {evidence.length} pinned · sha256
            </span>
          </div>

          <div style={{
            flex: 1, minHeight: 0, overflow: "auto",
            display: "flex", flexDirection: "column", gap: 8, paddingRight: 12,
          }}>
            {evidence.map((e, i) => (
              <div key={e.id} className="sq-in-up sq-card" style={{
                padding: "10px 14px",
                display: "grid", gridTemplateColumns: "70px 1fr 80px 70px",
                alignItems: "center", gap: 14,
                animationDelay: `${Math.min(i, 6) * 30}ms`,
              }}>
                <span style={{
                  fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-bone)",
                  letterSpacing: "0.04em",
                }}>{e.id}</span>
                <div style={{ minWidth: 0 }}>
                  <div style={{
                    fontFamily: "var(--sq-font-mono)", fontSize: 9, letterSpacing: "0.14em",
                    textTransform: "uppercase", color: kindColor(e.kind), marginBottom: 2,
                  }}>{e.kind}</div>
                  <div style={{
                    fontFamily: "var(--sq-font-mono)", fontSize: 12, color: "var(--sq-smoke)",
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }}>{e.src}</div>
                </div>
                <span className="sq-mono-chip" style={{
                  color: statusColor(e.status), borderColor: statusBorder(e.status),
                  textAlign: "center", justifySelf: "start",
                }}>{e.status}</span>
                <span style={{
                  fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
                  letterSpacing: "0.04em", textAlign: "right",
                }}>{e.sha}</span>
              </div>
            ))}
          </div>

          {/* Candidate counter */}
          <div style={{
            marginTop: 16, padding: "14px 16px",
            border: "1px solid var(--sq-border-strong)",
            display: "flex", alignItems: "center", justifyContent: "space-between",
            background: "var(--sq-deep-shadow)",
          }}>
            <div>
              <div className="sq-overline" style={{ marginBottom: 4 }}>Candidate pool</div>
              <div style={{ fontFamily: "var(--sq-font-serif)", fontSize: 32, color: "var(--sq-bone)", lineHeight: 1 }}>
                <span style={{ color: "var(--sq-bone)" }}>{candidates}</span>
                <span style={{ color: "var(--sq-slate-taupe)", fontSize: 22 }}> / 27</span>
              </div>
            </div>
            <div style={{ display: "flex", gap: 22, fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)", letterSpacing: "0.06em" }}>
              <div><div style={{ color: "#A6BB87" }}>verified</div><div style={{ color: "var(--sq-bone)", fontSize: 14, marginTop: 4 }}>{Math.min(candidates, 4)}</div></div>
              <div><div style={{ color: "#D7A876" }}>partial</div><div style={{ color: "var(--sq-bone)", fontSize: 14, marginTop: 4 }}>{Math.max(0, candidates - 4)}</div></div>
              <div><div style={{ color: "#D67F6B" }}>disputed</div><div style={{ color: "var(--sq-bone)", fontSize: 14, marginTop: 4 }}>{candidates >= 5 ? 1 : 0}</div></div>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

function phaseColor(p) {
  return {
    classify: "var(--sq-fog)",
    budget:   "var(--sq-smoke)",
    search:   "#A6BB87",
    fetch:    "#A6BB87",
    extract:  "var(--sq-bone)",
    rank:     "var(--sq-bone)",
    draft:    "#D7A876",
    persist:  "var(--sq-smoke)",
  }[p] || "var(--sq-smoke)";
}
function kindColor(k) {
  return {
    search:  "var(--sq-fog)",
    fetch:   "#A6BB87",
    extract: "var(--sq-bone)",
  }[k] || "var(--sq-smoke)";
}
function statusColor(s) {
  return {
    verified: "#A6BB87",
    fresh:    "var(--sq-fog)",
    disputed: "#D67F6B",
  }[s] || "var(--sq-smoke)";
}
function statusBorder(s) {
  return {
    verified: "rgba(122,140,94,0.5)",
    fresh:    "var(--sq-border-strong)",
    disputed: "rgba(182,92,74,0.55)",
  }[s] || "var(--sq-border-strong)";
}

window.HuntInProgress = HuntInProgress;
