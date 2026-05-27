/* global React, SQIcon */

// Validated-candidates list. Each card shows score, score-breakdown radial, pricing status, evidence chips.
// Clicking a card focuses it and reveals an evidence-trail strip + Draft RFQ button.
// Data is the mapped view-model from the real /run result (see api.js).

function TagPill({ t, kind }) {
  const k = {
    ok:    { c: "#A6BB87", b: "rgba(122,140,94,0.5)", d: "#7A8C5E" },
    warn:  { c: "#D7A876", b: "rgba(201,145,85,0.5)", d: "#C99155" },
    risk:  { c: "#D67F6B", b: "rgba(182,92,74,0.55)", d: "#B65C4A" },
    plain: { c: "var(--sq-smoke)", b: "var(--sq-border-strong)", d: null },
  }[kind] || { c: "var(--sq-smoke)", b: "var(--sq-border-strong)", d: null };
  return (
    <span className="sq-pill" style={{ color: k.c, borderColor: k.b }}>
      {k.d && <span className="dot" style={{ background: k.d }}/>}
      {t}
    </span>
  );
}

function PricingBadge({ status }) {
  const m = {
    EXACT_PRICE:         { c: "#A6BB87", b: "rgba(122,140,94,0.5)"  },
    PRICE_RANGE:         { c: "var(--sq-fog)", b: "var(--sq-border-strong)" },
    RATE_CARD_FOUND:     { c: "#D7A876", b: "rgba(201,145,85,0.5)"  },
    STARTING_FROM:       { c: "#D7A876", b: "rgba(201,145,85,0.5)"  },
    QUOTE_REQUIRED:      { c: "#D67F6B", b: "rgba(182,92,74,0.55)" },
    CONTACT_FOR_PRICING: { c: "#D67F6B", b: "rgba(182,92,74,0.55)" },
    NOT_FOUND:           { c: "var(--sq-smoke)", b: "var(--sq-border-strong)" },
    CONFLICTING:         { c: "#D67F6B", b: "rgba(182,92,74,0.55)" },
  }[status] || { c: "var(--sq-smoke)", b: "var(--sq-border-strong)" };
  return <span className="sq-mono-chip" style={{ color: m.c, borderColor: m.b }}>{status}</span>;
}

// 6-spoke score-breakdown radial. Axes come from the breakdown object keys, so
// service candidates show match/channel/geo/contact/checklist/integrity and
// other modes show their own derived axes — all 0..100.
function ScoreRadial({ breakdown, size = 80 }) {
  const keys = Object.keys(breakdown || {});
  const n = Math.max(keys.length, 3);
  const cx = size / 2, cy = size / 2, R = size / 2 - 6;
  const points = keys.map((k, i) => {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    const r = (breakdown[k] / 100) * R;
    return [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
  });
  const ring = keys.map((_, i) => {
    const a = (i / n) * Math.PI * 2 - Math.PI / 2;
    return [cx + Math.cos(a) * R, cy + Math.sin(a) * R];
  });
  const polyStr = (pts) => pts.map(p => p.join(",")).join(" ");
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
      <polygon points={polyStr(ring)} fill="none" stroke="var(--sq-border-strong)" strokeWidth="0.7" strokeDasharray="2 3" />
      <polygon points={polyStr(points)} fill="rgba(242,229,230,0.10)" stroke="var(--sq-bone)" strokeWidth="1" />
      {points.map((p, i) => <circle key={i} cx={p[0]} cy={p[1]} r="1.4" fill="var(--sq-bone)"/>)}
    </svg>
  );
}

function Shortlist({ focusId, onFocus, onDraft, onDetail, vendors = [], meta = {}, title = "" }) {
  if (!vendors.length) {
    return (
      <div style={{ padding: "60px 0", textAlign: "center", color: "var(--sq-fg-muted)" }}>
        <div className="sq-overline" style={{ marginBottom: 12 }}>No validated candidates</div>
        <div style={{ fontFamily: "var(--sq-font-serif)", fontSize: 24, color: "var(--sq-bone)", fontStyle: "italic" }}>
          Nothing cleared the evidence bar.
        </div>
        <p style={{ maxWidth: 460, margin: "12px auto 0", fontSize: 13.5, lineHeight: 1.55 }}>
          The hunt finished but no candidate met the completeness threshold. Try a broader brief or a different mode.
        </p>
      </div>
    );
  }

  return (
    <div className="sq-section" style={{ display: "flex", flexDirection: "column", gap: 14, padding: 0 }}>
      {/* Header */}
      <div style={{
        display: "flex", alignItems: "flex-end", justifyContent: "space-between",
        gap: 24, marginBottom: 4,
      }}>
        <div style={{ minWidth: 0 }}>
          <div className="sq-overline">Validated candidates</div>
          <div style={{
            fontFamily: "var(--sq-font-serif)", fontSize: 28, fontWeight: 500,
            color: "var(--sq-bone)", marginTop: 8, lineHeight: 1.1,
            maxWidth: 640, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>
            {title || "Validated suppliers"}
          </div>
        </div>

        <div style={{
          display: "flex", gap: 22, alignItems: "flex-end",
          fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
          letterSpacing: "0.06em",
        }}>
          <Metric label="retained"      value={meta.retained || `${vendors.length}`}/>
          <Metric label="quote rate"    value={meta.quoteRate || "—"}/>
          <Metric label="fetches"       value={meta.fetches || "—"}/>
          <Metric label="ledger ents."  value={meta.ledger || "—"}/>
        </div>
      </div>

      {/* Sort/filter strip — weights are the real service-ranker point caps */}
      <div style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "10px 0", borderTop: "1px solid var(--sq-border)", borderBottom: "1px solid var(--sq-border)",
        marginTop: 4, marginBottom: 4,
        fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
        letterSpacing: "0.06em", flexWrap: "wrap",
      }}>
        <span>sort ·</span>
        <Chip active>fit score ↓</Chip>
        <Chip>match</Chip>
        <Chip>geo</Chip>
        <Chip>completeness</Chip>
        <span style={{ marginLeft: 18 }}>filter ·</span>
        <Chip>SEA</Chip>
        <Chip>quote channel verified</Chip>
        <span style={{ marginLeft: "auto" }}>caps · match /25 · channel /25 · geo /20 · contact /15 · checklist /10</span>
      </div>

      {/* Cards */}
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {vendors.map((v, i) => {
          const focused = focusId === v.id;
          return (
            <article
              key={v.id}
              className="sq-shortlist-card"
              onClick={() => onFocus?.(v.id)}
              style={{
                background: focused ? "rgba(242,229,230,0.045)" : "var(--sq-deep-shadow)",
                border: `1px solid ${focused ? "var(--sq-bone)" : "var(--sq-border)"}`,
                padding: "20px 22px",
                cursor: "pointer",
                transition: "border-color 160ms var(--sq-ease-sharp), background 160ms var(--sq-ease-sharp)",
                position: "relative",
              }}
              onMouseEnter={e => { if (!focused) e.currentTarget.style.borderColor = "var(--sq-border-strong)"; }}
              onMouseLeave={e => { if (!focused) e.currentTarget.style.borderColor = "var(--sq-border)"; }}
            >
              <div style={{ display: "flex", alignItems: "flex-start", gap: 22 }}>
                {/* Rank */}
                <div style={{
                  width: 38, flex: "none", textAlign: "right",
                  fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
                  paddingTop: 6, letterSpacing: "0.06em",
                }}>{String(i + 1).padStart(2, "0")}</div>

                {/* Body */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
                    <h3 style={{
                      margin: 0, fontFamily: "var(--sq-font-serif)", fontWeight: 500, fontSize: 22,
                      color: "var(--sq-bone)", letterSpacing: "-0.005em",
                    }}>{v.name}</h3>
                    <span style={{
                      fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
                      letterSpacing: "0.06em",
                    }}>· {v.country}{v.domain ? ` · ${v.domain}` : ""}</span>
                    <PricingBadge status={v.pricing}/>
                  </div>

                  <p style={{
                    margin: "10px 0 0", fontSize: 13.5, lineHeight: 1.55,
                    color: "var(--sq-fg-muted)", maxWidth: 640,
                  }}>{v.summary}</p>

                  <div style={{ marginTop: 14, display: "flex", flexWrap: "wrap", gap: 8 }}>
                    {v.tags.map((t, ti) => <TagPill key={t.t + ti} {...t}/>)}
                  </div>

                  {/* Evidence trail */}
                  <div style={{
                    marginTop: 14, display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap",
                    fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
                    letterSpacing: "0.04em",
                  }}>
                    <span>evidence ·</span>
                    {v.evidence.slice(0, 5).map(eid => (
                      <span key={eid} style={{
                        color: "var(--sq-smoke)",
                        padding: "2px 8px",
                        border: "1px solid var(--sq-border)",
                      }}>{eid}</span>
                    ))}
                    <span style={{ color: "var(--sq-slate-taupe)" }}>·</span>
                    <span>{v.match}</span>
                  </div>
                </div>

                {/* Score block */}
                <div style={{
                  width: 200, flex: "none", textAlign: "right",
                  display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 8,
                }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <div>
                      <div style={{
                        fontFamily: "var(--sq-font-serif)", fontSize: 36, lineHeight: 1,
                        color: "var(--sq-bone)", textAlign: "right",
                      }}>{v.score.toFixed(1)}</div>
                      <div style={{
                        fontFamily: "var(--sq-font-mono)", fontSize: 9, color: "var(--sq-slate-taupe)",
                        letterSpacing: "0.16em", textTransform: "uppercase", marginTop: 4,
                      }}>score · /{v.scoreCap}</div>
                    </div>
                    <ScoreRadial breakdown={v.breakdown} size={72}/>
                  </div>

                  <div style={{ width: "100%", marginTop: 8 }}>
                    <div style={{
                      display: "flex", justifyContent: "space-between",
                      fontFamily: "var(--sq-font-mono)", fontSize: 10, color: "var(--sq-slate-taupe)",
                      letterSpacing: "0.06em",
                    }}>
                      <span>completeness</span><span>{v.reliability}%</span>
                    </div>
                    <div style={{ position: "relative", height: 2, background: "var(--sq-border)", marginTop: 6 }}>
                      <span style={{
                        position: "absolute", left: 0, top: 0, height: "100%",
                        width: `${v.reliability}%`, background: "var(--sq-bone)",
                        transition: "width 400ms var(--sq-ease-sharp)",
                      }}/>
                    </div>
                  </div>

                  {focused && (
                    <div className="sq-in-up" style={{
                      marginTop: 8, display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap",
                    }}>
                      <button onClick={(e) => { e.stopPropagation(); onDetail?.(v); }}
                        className="sq-btn sq-btn-secondary" style={{ height: 32, padding: "0 12px", fontSize: 12 }}>
                        <SQIcon name="eye" size={12}/> Evidence
                      </button>
                      <button onClick={(e) => { e.stopPropagation(); onDraft?.(v); }}
                        className="sq-btn sq-btn-primary" style={{ height: 32, padding: "0 14px", fontSize: 12 }}>
                        Draft RFQ
                        <SQIcon name="arrow" size={11} stroke={1.8} />
                      </button>
                    </div>
                  )}
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}

function Metric({ label, value }) {
  return (
    <div style={{ textAlign: "right" }}>
      <div style={{ color: "var(--sq-slate-taupe)" }}>{label}</div>
      <div style={{ color: "var(--sq-bone)", fontSize: 13, marginTop: 3, letterSpacing: "0.04em" }}>{value}</div>
    </div>
  );
}

function Chip({ children, active }) {
  return (
    <span style={{
      padding: "3px 10px",
      border: `1px solid ${active ? "var(--sq-bone)" : "var(--sq-border-strong)"}`,
      color: active ? "var(--sq-bone)" : "var(--sq-smoke)",
      cursor: "default",
    }}>{children}</span>
  );
}

window.Shortlist = Shortlist;
window.TagPill = TagPill;
window.PricingBadge = PricingBadge;
window.ScoreRadial = ScoreRadial;
