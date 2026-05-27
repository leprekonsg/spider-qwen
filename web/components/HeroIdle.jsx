/* global React, SQIcon */

// The editorial hero / idle screen — shown before a hunt begins.
// This is where Spider-Qwen makes its tonal promise: an editorial moment, not a SaaS form.

function HeroIdle({ query, setQuery, onRun, recent, onPickRecent }) {
  const refs = [
    "Find office cleaning vendors in Singapore and prepare RFQ drafts",
    "Industrial nitrogen gas vendors APAC, exact price",
    "ISO 27001 SOC2 auditors SEA-first",
    "Revalidate Q2 packaging suppliers",
  ];

  return (
    <div style={{
      flex: 1, minWidth: 0, overflow: "auto",
      position: "relative",
      background: `radial-gradient(ellipse at 50% 0%, rgba(242,229,230,0.04) 0%, transparent 55%), var(--sq-obsidian)`,
    }}>
      <WebMotif />

      <div style={{
        position: "relative", maxWidth: 1100, margin: "0 auto",
        padding: "80px 64px 64px",
      }}>
        {/* Overline + trio */}
        <div style={{
          display: "flex", alignItems: "center", gap: 24,
          marginBottom: 28,
        }}>
          <span className="sq-overline" style={{ color: "var(--sq-fg-muted)" }}>Agentic procurement search · v1.4</span>
          <span style={{ flex: 1, height: 1, background: "var(--sq-border)" }} />
          <span className="sq-trio">
            <span>predatory</span><span>precise</span><span>editorial</span>
          </span>
        </div>

        {/* Editorial title */}
        <h1 style={{
          margin: 0, fontFamily: "var(--sq-font-serif)", fontWeight: 500,
          fontSize: 80, lineHeight: 0.98, letterSpacing: "-0.02em",
          color: "var(--sq-bone)",
        }}>
          <span style={{ display: "block" }}>Signals.</span>
          <span style={{ display: "block" }}>Suppliers.</span>
          <span style={{ display: "block", fontStyle: "italic", color: "var(--sq-smoke)" }}>Strategy.</span>
        </h1>

        <p className="sq-tagline" style={{ maxWidth: 620, marginTop: 28 }}>
          Spider-Qwen weaves the supplier web — mapping signals, verifying evidence,
          ranking SEA-first, drafting RFQs for your review. No auto-submit. No black boxes.
        </p>

        {/* Query input */}
        <form
          onSubmit={(e) => { e.preventDefault(); onRun?.(); }}
          className="sq-brackets"
          style={{
            marginTop: 44,
            background: "var(--sq-bg-input)",
            border: "1px solid var(--sq-border-strong)",
            display: "flex", alignItems: "stretch",
            position: "relative",
            boxShadow: "0 0 0 6px rgba(10,10,10,0.6), 0 24px 60px rgba(0,0,0,0.55)",
          }}
        >
          <span className="br-bl"></span>
          <span className="br-br"></span>

          <div style={{
            padding: "0 18px", display: "flex", alignItems: "center", gap: 10,
            borderRight: "1px solid var(--sq-border-strong)",
            fontFamily: "var(--sq-font-mono)", fontSize: 11, letterSpacing: "0.1em",
            color: "var(--sq-slate-taupe)", textTransform: "uppercase",
          }}>
            <SQIcon name="spider" size={14} color="var(--sq-bone)" />
            <span>brief</span>
          </div>

          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="e.g. Find office cleaning vendors in Singapore and prepare RFQ drafts"
            style={{
              flex: 1, border: 0, outline: 0, background: "transparent",
              color: "var(--sq-bone)", fontFamily: "var(--sq-font-sans)",
              fontSize: 17, padding: "20px 18px",
            }}
          />

          <button type="submit" className="sq-btn sq-btn-primary" style={{
            margin: 8, height: "auto", padding: "0 24px", fontSize: 13,
          }}>
            Begin hunt
            <SQIcon name="arrow" size={14} stroke={1.8} />
          </button>
        </form>

        <div style={{
          marginTop: 14, display: "flex", flexWrap: "wrap", gap: 8,
        }}>
          {refs.map(r => (
            <button key={r} onClick={() => { setQuery(r); }}
              className="sq-pill" style={{
                background: "transparent", cursor: "pointer",
                color: "var(--sq-fg-muted)",
                transition: "border-color 160ms var(--sq-ease-sharp), color 160ms var(--sq-ease-sharp)",
              }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--sq-bone)"; e.currentTarget.style.color = "var(--sq-bone)"; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--sq-border-strong)"; e.currentTarget.style.color = "var(--sq-fg-muted)"; }}
            >
              <SQIcon name="sparkle" size={10}/>
              {r}
            </button>
          ))}
        </div>

        {/* Trio of capability cards */}
        <div style={{
          marginTop: 80,
          display: "grid", gridTemplateColumns: "repeat(3, 1fr)",
          gap: 0,
          borderTop: "1px solid var(--sq-border)",
        }}>
          {[
            { o: "01", t: "Map the web",  d: "Crawls public sources, vendor catalogues, registries, news — SEA-first." },
            { o: "02", t: "Verify evidence", d: "Every claim is pinned to a SHA-256 snippet in an append-only ledger." },
            { o: "03", t: "Draft for review", d: "RFQs, contact channels, and risk callouts — never auto-sent." },
          ].map((c, i) => (
            <div key={c.o} style={{
              padding: "28px 24px 30px",
              borderRight: i < 2 ? "1px solid var(--sq-border)" : "none",
            }}>
              <div className="sq-overline" style={{ color: "var(--sq-slate-taupe)", marginBottom: 14 }}>
                {c.o}
              </div>
              <div style={{
                fontFamily: "var(--sq-font-serif)", fontSize: 22, fontWeight: 500,
                color: "var(--sq-bone)", lineHeight: 1.2,
              }}>{c.t}</div>
              <p style={{ marginTop: 10, marginBottom: 0, fontSize: 13, color: "var(--sq-fg-muted)", lineHeight: 1.55, maxWidth: 280 }}>
                {c.d}
              </p>
            </div>
          ))}
        </div>

        {/* Recent runs */}
        <div style={{ marginTop: 56 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16 }}>
            <SQIcon name="history" size={14} color="var(--sq-slate-taupe)" />
            <span className="sq-overline">Recent hunts</span>
            <span style={{ flex: 1, height: 1, background: "var(--sq-border)" }} />
          </div>

          <div style={{ border: "1px solid var(--sq-border)" }}>
            {(!recent || recent.length === 0) && (
              <div style={{
                padding: "22px 20px", fontFamily: "var(--sq-font-serif)", fontStyle: "italic",
                fontSize: 15, color: "var(--sq-slate-taupe)",
              }}>
                No hunts yet — your runs will appear here once you begin.
              </div>
            )}
            {(recent || []).map((r, i) => (
              <button key={i} onClick={() => onPickRecent?.(r)} style={{
                width: "100%", textAlign: "left",
                display: "grid", gridTemplateColumns: "minmax(0,1fr) 220px 180px 110px",
                alignItems: "center", gap: 18,
                padding: "16px 20px",
                background: "transparent",
                border: 0, borderBottom: i < recent.length - 1 ? "1px solid var(--sq-border)" : "0",
                cursor: "pointer",
                color: "var(--sq-bone)",
                transition: "background 160ms var(--sq-ease-sharp)",
              }}
              onMouseEnter={e => e.currentTarget.style.background = "rgba(242,229,230,0.03)"}
              onMouseLeave={e => e.currentTarget.style.background = "transparent"}>
                <div style={{ fontFamily: "var(--sq-font-serif)", fontSize: 17, color: "var(--sq-bone)" }}>
                  {r.q}
                </div>
                <div className="sq-mono-chip" style={{ justifySelf: "start", color: "var(--sq-smoke)" }}>{r.mode}</div>
                <div style={{ fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "#A6BB87", letterSpacing: "0.04em" }}>
                  {r.verdict}
                </div>
                <div style={{ fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)", letterSpacing: "0.04em", textAlign: "right" }}>
                  {r.t}
                </div>
              </button>
            ))}
          </div>
        </div>

        {/* Footer disclaimer */}
        <p style={{
          marginTop: 56, textAlign: "center",
          fontFamily: "var(--sq-font-serif)", fontStyle: "italic", color: "var(--sq-slate-taupe)",
          fontSize: 14, letterSpacing: "0.01em",
        }}>
          No portal submission. No auto-send. Drafts only — every output trails back to a ledger entry.
        </p>
      </div>
    </div>
  );
}

// Spider-web SVG motif — radial lines + faint dashed circles.
function WebMotif({ opacity = 1, anchor = "top" }) {
  const cy = anchor === "top" ? 200 : 400;
  return (
    <svg
      className="sq-web-bg"
      viewBox="0 0 1200 800" preserveAspectRatio="xMidYMid slice"
      style={{ opacity }}
      aria-hidden
    >
      <g stroke="rgba(242,229,230,0.06)" fill="none" strokeWidth="0.7">
        {Array.from({ length: 22 }).map((_, i) => {
          const a = (i / 22) * Math.PI * 2;
          return <line key={i} x1="600" y1={cy} x2={600 + Math.cos(a) * 1400} y2={cy + Math.sin(a) * 1400} />;
        })}
        {[80, 160, 280, 420, 600, 820].map(r => (
          <circle key={r} cx="600" cy={cy} r={r} strokeDasharray="2 12" />
        ))}
      </g>
    </svg>
  );
}

window.HeroIdle = HeroIdle;
window.WebMotif = WebMotif;
