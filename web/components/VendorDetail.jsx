/* global React, SQIcon, ScoreRadial, TagPill, PricingBadge */

// Evidence drill-down for a single vendor. Right-side drawer with the full
// score breakdown bars, the vendor's real ledger entries, the quote channel,
// and "why this rank" reasoning derived from the actual ranker decomposition.

// Point caps per axis for service candidates (mirrors service_ranker.py).
const AXIS_CAP = { match: 25, channel: 25, geo: 20, contact: 15, checklist: 10, integrity: 20 };

function VendorDetail({ vendor, onClose, onDraft }) {
  if (!vendor) return null;
  const ledger = vendor.ledger || [];

  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)",
      zIndex: 50, display: "flex", justifyContent: "flex-end",
      animation: "sq-in-up 200ms var(--sq-ease-sharp) both",
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        width: "min(720px, 95vw)", height: "100%", background: "var(--sq-deep-shadow)",
        borderLeft: "1px solid var(--sq-border-strong)",
        boxShadow: "0 1px 0 rgba(255,255,255,0.05) inset, -24px 0 60px rgba(0,0,0,0.7)",
        overflow: "auto", display: "flex", flexDirection: "column",
      }}>
        {/* Header */}
        <div style={{
          padding: "22px 28px", borderBottom: "1px solid var(--sq-border)",
          display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 22,
        }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="sq-overline" style={{ marginBottom: 8 }}>Vendor dossier</div>
            <h2 style={{
              margin: 0, fontFamily: "var(--sq-font-serif)", fontWeight: 500, fontSize: 32,
              color: "var(--sq-bone)", letterSpacing: "-0.012em",
            }}>{vendor.name}</h2>
            <div style={{
              display: "flex", gap: 14, marginTop: 10, flexWrap: "wrap",
              fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
              letterSpacing: "0.04em",
            }}>
              <span>{vendor.country}</span>
              {vendor.domain && <><span>·</span>
                <span><a href={vendor.website || `https://${vendor.domain}`} onClick={e => e.preventDefault()}>{vendor.domain}</a></span></>}
              <span>·</span>
              <span>{vendor.match}</span>
            </div>
          </div>
          <button onClick={onClose} className="sq-btn sq-btn-ghost" style={{ padding: "0 10px" }} aria-label="Close">
            <SQIcon name="x" size={16}/>
          </button>
        </div>

        <div style={{ padding: "24px 28px", display: "flex", flexDirection: "column", gap: 26 }}>
          {/* Score + radial */}
          <section style={{
            display: "grid", gridTemplateColumns: "auto 1fr",
            gap: 28, alignItems: "center",
            padding: "20px 22px",
            border: "1px solid var(--sq-border)",
            background: "var(--sq-obsidian)",
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
              <ScoreRadial breakdown={vendor.breakdown} size={120}/>
              <div>
                <div style={{
                  fontFamily: "var(--sq-font-serif)", fontSize: 56, lineHeight: 1,
                  color: "var(--sq-bone)",
                }}>{vendor.score.toFixed(1)}</div>
                <div style={{
                  fontFamily: "var(--sq-font-mono)", fontSize: 10, color: "var(--sq-slate-taupe)",
                  letterSpacing: "0.16em", textTransform: "uppercase", marginTop: 6,
                }}>score · weighted · /{vendor.scoreCap}</div>
                <div style={{ marginTop: 12 }}>
                  <PricingBadge status={vendor.pricing}/>
                </div>
              </div>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {Object.entries(vendor.breakdown).map(([k, v]) => (
                <div key={k} style={{
                  display: "grid", gridTemplateColumns: "90px 1fr 38px 40px",
                  gap: 12, alignItems: "center",
                  fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-smoke)",
                }}>
                  <span style={{ color: "var(--sq-slate-taupe)", letterSpacing: "0.04em" }}>{k}</span>
                  <div style={{ position: "relative", height: 4, background: "var(--sq-border)" }}>
                    <span style={{
                      position: "absolute", left: 0, top: 0, height: "100%", width: `${v}%`,
                      background: "var(--sq-bone)",
                    }}/>
                  </div>
                  <span style={{ color: "var(--sq-bone)", textAlign: "right" }}>{v}</span>
                  <span style={{ color: "var(--sq-slate-taupe)", textAlign: "right" }}>{AXIS_CAP[k] ? `/${AXIS_CAP[k]}` : ""}</span>
                </div>
              ))}
            </div>
          </section>

          {/* Why this rank */}
          <section>
            <div className="sq-overline" style={{ marginBottom: 12 }}>Why this rank</div>
            <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 10 }}>
              {vendor.why.map((line, i) => (
                <li key={i} style={{
                  display: "grid", gridTemplateColumns: "20px 1fr",
                  gap: 12, alignItems: "flex-start",
                  fontFamily: "var(--sq-font-sans)", fontSize: 13.5, lineHeight: 1.55,
                  color: "var(--sq-fg)",
                }}>
                  <span style={{
                    fontFamily: "var(--sq-font-mono)", fontSize: 10, color: "var(--sq-slate-taupe)",
                    letterSpacing: "0.06em", paddingTop: 3,
                  }}>{String(i + 1).padStart(2, "0")}</span>
                  <span>{line}</span>
                </li>
              ))}
            </ul>
          </section>

          {/* Tags */}
          <section style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {vendor.tags.map((t, i) => <TagPill key={t.t + i} {...t}/>)}
          </section>

          {/* Quote channel */}
          <section>
            <div className="sq-overline" style={{ marginBottom: 12 }}>Quote channel</div>
            <div style={{
              display: "flex", alignItems: "center", gap: 14,
              padding: "14px 16px", border: "1px solid var(--sq-border)",
              background: "var(--sq-obsidian)",
            }}>
              <SQIcon name="link" size={18} color="var(--sq-smoke)"/>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontFamily: "var(--sq-font-mono)", fontSize: 13, color: "var(--sq-bone)",
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{vendor.contact}</div>
                <div style={{
                  fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)",
                  marginTop: 3, letterSpacing: "0.04em",
                }}>
                  {vendor.quoteType || "channel"} · evidence · {vendor.evidence[0] || "—"} · sha256:{vendor.sha}
                </div>
              </div>
              <span className="sq-mono-chip" style={{
                color: vendor.quoteType ? "#A6BB87" : "var(--sq-smoke)",
                borderColor: vendor.quoteType ? "rgba(122,140,94,0.5)" : "var(--sq-border-strong)",
              }}>{vendor.quoteType ? "verified" : "none"}</span>
            </div>
          </section>

          {/* Evidence ledger entries */}
          <section>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
              <div className="sq-overline">Evidence ledger · {ledger.length} entries</div>
              <span className="sq-mono-chip">append-only · sha256</span>
            </div>
            <div style={{ border: "1px solid var(--sq-border)" }}>
              {ledger.map((e, i) => (
                <div key={e.id} style={{
                  display: "grid", gridTemplateColumns: "120px 1fr 80px 80px 70px",
                  alignItems: "center", gap: 14,
                  padding: "12px 16px",
                  borderBottom: i < ledger.length - 1 ? "1px solid var(--sq-border)" : "none",
                  fontFamily: "var(--sq-font-mono)", fontSize: 11.5,
                }}>
                  <span style={{ color: "var(--sq-bone)", letterSpacing: "0.04em",
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.id}</span>
                  <span style={{ color: "var(--sq-smoke)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.src}</span>
                  <span style={{ color: "var(--sq-fog)", letterSpacing: "0.06em" }}>{e.kind}</span>
                  <span style={{
                    color: e.status === "verified" ? "#A6BB87"
                         : e.status === "disputed" ? "#D67F6B"
                         : "var(--sq-fog)",
                    letterSpacing: "0.06em",
                  }}>{e.status}</span>
                  <span style={{ color: "var(--sq-slate-taupe)", textAlign: "right" }}>{e.sha}</span>
                </div>
              ))}
              {!ledger.length && (
                <div style={{ padding: "16px", fontFamily: "var(--sq-font-mono)", fontSize: 11.5, color: "var(--sq-slate-taupe)" }}>
                  No ledger entries attached.
                </div>
              )}
            </div>
          </section>

          {/* Actions */}
          <section style={{ display: "flex", gap: 10 }}>
            <button onClick={() => onDraft?.(vendor)} className="sq-btn sq-btn-primary" style={{ flex: 1, height: 44 }}
              disabled={!vendor.quoteType}>
              Draft RFQ for review
              <SQIcon name="arrow" size={14} stroke={1.8}/>
            </button>
            <button className="sq-btn sq-btn-secondary" style={{ height: 44, padding: "0 18px" }}>
              <SQIcon name="refresh" size={13}/> Re-validate
            </button>
            <button className="sq-btn sq-btn-secondary" style={{ height: 44, padding: "0 18px" }}>
              <SQIcon name="file" size={13}/> Export dossier
            </button>
          </section>

          <p style={{
            margin: 0, fontFamily: "var(--sq-font-serif)", fontStyle: "italic",
            fontSize: 13, color: "var(--sq-slate-taupe)", lineHeight: 1.55,
          }}>
            Spider-Qwen does not contact vendors, submit forms, or auto-send mail. This dossier
            is for procurement officer review. Re-validation triggers a fresh ledger entry.
          </p>
        </div>
      </div>
    </div>
  );
}

window.VendorDetail = VendorDetail;
