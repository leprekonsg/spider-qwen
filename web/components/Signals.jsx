/* global React, SQIcon */

// Right-side signals rail + the optional floating toast intercept.
// Signals and ledger are derived from the real run result and passed in as props.

const TONE = {
  risk: { c: "#D67F6B", d: "#B65C4A", glow: "0 0 0 1px rgba(182,92,74,0.30), 0 0 18px rgba(182,92,74,0.16)" },
  warn: { c: "#D7A876", d: "#C99155", glow: "none" },
  info: { c: "var(--sq-fog)", d: "#8A8472", glow: "none" },
  ok:   { c: "#A6BB87", d: "#7A8C5E", glow: "none" },
};
const ICON_FOR = { risk: "risks", warn: "spark", info: "info", ok: "check" };

function SignalsRail({ feed = "rail", signals = [], ledger = [] }) {
  if (feed !== "rail") return null;
  return (
    <aside style={{
      width: 320, flex: "none",
      borderLeft: "1px solid var(--sq-border)",
      background: "var(--sq-obsidian)",
      padding: "22px 20px",
      display: "flex", flexDirection: "column", gap: 18, overflow: "auto",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div className="sq-overline">Live signals</div>
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          fontFamily: "var(--sq-font-mono)", fontSize: 10, color: "#A6BB87", letterSpacing: "0.06em",
        }}>
          <span className="sq-pulse" style={{ width: 6, height: 6, borderRadius: 999, background: "#A6BB87" }} />
          streaming
        </span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {signals.length
          ? signals.map(s => <SignalCard key={s.id} signal={s} />)
          : <Empty text="No signals yet — run a hunt to surface them." />}
      </div>

      <div style={{ borderTop: "1px solid var(--sq-border)", paddingTop: 16, marginTop: 4 }}>
        <div className="sq-overline" style={{ marginBottom: 10 }}>Evidence ledger</div>
        <div style={{ fontFamily: "var(--sq-font-mono)", fontSize: 11, lineHeight: 1.85, color: "var(--sq-smoke)" }}>
          {ledger.length
            ? ledger.slice(0, 8).map(e => (
                <div key={e.id} style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.id}</span>
                  <span style={{ color: e.status === "verified" ? "#A6BB87" : e.status === "disputed" ? "#D67F6B" : "var(--sq-fog)" }}>{e.status}</span>
                </div>
              ))
            : <span style={{ color: "var(--sq-slate-taupe)" }}>empty</span>}
        </div>
        <a href="#" onClick={e => e.preventDefault()} style={{
          display: "inline-flex", alignItems: "center", gap: 6, marginTop: 14,
          fontFamily: "var(--sq-font-sans)", fontSize: 12, color: "var(--sq-bone)",
        }}>
          Open full ledger <SQIcon name="arrow" size={10} stroke={1.8}/>
        </a>
      </div>
    </aside>
  );
}

function Empty({ text }) {
  return <div style={{ fontSize: 12, color: "var(--sq-slate-taupe)", padding: "8px 2px", lineHeight: 1.5 }}>{text}</div>;
}

function SignalCard({ signal }) {
  const t = TONE[signal.kind] || TONE.info;
  return (
    <div style={{
      background: "var(--sq-deep-shadow)", border: "1px solid var(--sq-border)",
      padding: "12px 14px",
      boxShadow: signal.kind === "risk" ? t.glow : "none",
      display: "flex", gap: 12,
    }}>
      <div style={{
        width: 32, height: 32, flex: "none", borderRadius: 2,
        border: `1px solid ${signal.kind === "ok" ? "rgba(122,140,94,0.45)" : "var(--sq-border-strong)"}`,
        display: "flex", alignItems: "center", justifyContent: "center", color: t.c,
      }}>
        <SQIcon name={ICON_FOR[signal.kind] || "info"} size={14}/>
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{
          fontFamily: "var(--sq-font-sans)", fontSize: 10, fontWeight: 600,
          letterSpacing: "0.14em", textTransform: "uppercase", color: t.c,
        }}>{signal.ovl}</div>
        <div style={{ fontSize: 13, color: "var(--sq-bone)", marginTop: 3, lineHeight: 1.35,
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{signal.title}</div>
        <div style={{
          fontFamily: "var(--sq-font-mono)", fontSize: 10, color: "var(--sq-slate-taupe)",
          marginTop: 6, letterSpacing: "0.04em",
        }}>{signal.meta}</div>
      </div>
    </div>
  );
}

// Floating intercept toast — bottom-right, animates in then can be dismissed.
function SignalToast({ signal, onDismiss, onOpen }) {
  if (!signal) return null;
  const t = TONE[signal.kind] || TONE.info;
  return (
    <div className="sq-in-up" style={{
      position: "fixed", bottom: 24, right: 24, zIndex: 70,
      width: 360,
      background: "var(--sq-deep-shadow)", border: `1px solid ${signal.kind === "risk" ? "rgba(182,92,74,0.55)" : "var(--sq-border-strong)"}`,
      boxShadow: signal.kind === "risk" ? "0 24px 60px rgba(0,0,0,0.7), 0 0 24px rgba(182,92,74,0.22)" : "0 24px 60px rgba(0,0,0,0.7)",
    }}>
      <div style={{
        padding: "12px 14px", borderBottom: "1px solid var(--sq-border)",
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 8,
          fontFamily: "var(--sq-font-sans)", fontSize: 10, fontWeight: 600,
          letterSpacing: "0.14em", textTransform: "uppercase", color: t.c,
        }}>
          <span className="sq-pulse" style={{ width: 6, height: 6, borderRadius: 999, background: t.d }} />
          Signal intercept
        </span>
        <button onClick={onDismiss} className="sq-btn sq-btn-ghost" style={{ height: 24, padding: "0 6px" }}>
          <SQIcon name="x" size={12}/>
        </button>
      </div>
      <div style={{ padding: "14px" }}>
        <div style={{
          fontFamily: "var(--sq-font-sans)", fontSize: 10, fontWeight: 600,
          letterSpacing: "0.14em", textTransform: "uppercase", color: t.c, marginBottom: 6,
        }}>{signal.ovl}</div>
        <div style={{
          fontFamily: "var(--sq-font-serif)", fontSize: 18, fontWeight: 500, color: "var(--sq-bone)",
          lineHeight: 1.25,
        }}>{signal.title}</div>
        <div style={{
          fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
          marginTop: 10, letterSpacing: "0.04em",
        }}>{signal.meta}</div>

        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          <button onClick={onOpen} className="sq-btn sq-btn-primary" style={{ height: 30, padding: "0 12px", fontSize: 12, flex: 1 }}>
            Investigate
            <SQIcon name="arrow" size={11} stroke={1.8}/>
          </button>
          <button onClick={onDismiss} className="sq-btn sq-btn-secondary" style={{ height: 30, padding: "0 12px", fontSize: 12 }}>
            Snooze 1h
          </button>
        </div>
      </div>
    </div>
  );
}

window.SignalsRail = SignalsRail;
window.SignalToast = SignalToast;
window.SignalCard = SignalCard;
