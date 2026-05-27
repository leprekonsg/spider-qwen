/* global React, SQIcon */

// RFQ Drafting drawer — renders the REAL rfq_draft from the controller:
// the checklist (each item evidence-backed or awaiting a buyer input), the
// generated email template, assumptions, and an explicit "hand off for human
// send" CTA. No network on send — drafts are never auto-submitted (v1 boundary).

function RfqDrawer({ vendor, draft, onClose }) {
  // Hooks must run unconditionally and in a stable order, so they precede the
  // early returns below.
  const [inputs, setInputs] = React.useState({});   // operator-provided values for un-evidenced fields
  const [sent, setSent] = React.useState(false);
  const vid = vendor && vendor.id;
  React.useEffect(() => { setInputs({}); setSent(false); }, [vid]);

  if (!vendor) return null;

  if (!draft) {
    return (
      <Shell vendor={vendor} onClose={onClose}>
        <div style={{ padding: "14px 16px", border: "1px solid rgba(201,145,85,0.5)", background: "rgba(201,145,85,0.06)" }}>
          <div className="sq-overline" style={{ color: "#D7A876", marginBottom: 6 }}>No RFQ draft</div>
          <div style={{ fontSize: 13, color: "var(--sq-bone)" }}>
            This candidate did not produce an RFQ draft — RFQs are only generated for service
            candidates with a verified quote channel.
          </div>
        </div>
      </Shell>
    );
  }

  const checklist = draft.required_inputs_checklist || [];
  const itemOk = (c) => !!c.evidence_ref || !!(inputs[c.field] && inputs[c.field].trim());
  const done = checklist.filter(itemOk).length;
  const total = checklist.length || 1;
  const completeness = done / total;

  const operatorLines = checklist
    .filter((c) => !c.evidence_ref && inputs[c.field] && inputs[c.field].trim())
    .map((c) => `${c.field}: ${inputs[c.field].trim()}`);

  const email = (draft.rfq_email_template || "") +
    (operatorLines.length ? `\n\nOperator-provided inputs:\n${operatorLines.map((l) => "  - " + l).join("\n")}` : "");

  const qc = draft.quote_channel;

  return (
    <Shell vendor={vendor} onClose={onClose}>
      {/* Status banner */}
      {sent ? (
        <div className="sq-in-up" style={{
          padding: "14px 16px", border: "1px solid rgba(122,140,94,0.5)", background: "rgba(122,140,94,0.07)",
          display: "flex", alignItems: "center", gap: 12,
        }}>
          <div style={{
            width: 28, height: 28, borderRadius: 2, border: "1px solid rgba(122,140,94,0.6)", color: "#A6BB87",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}><SQIcon name="check" size={14}/></div>
          <div>
            <div style={{ fontFamily: "var(--sq-font-sans)", fontSize: 10, fontWeight: 600, letterSpacing: "0.14em", textTransform: "uppercase", color: "#A6BB87" }}>
              Handed off to operator
            </div>
            <div style={{ fontSize: 13, color: "var(--sq-bone)", marginTop: 2 }}>
              Draft moved to the operator's outbox for human review and send. Nothing was sent by Spider-Qwen.
            </div>
          </div>
        </div>
      ) : (
        <div style={{
          padding: "14px 16px", border: "1px solid var(--sq-border)", background: "var(--sq-obsidian)",
          display: "flex", alignItems: "center", gap: 12,
        }}>
          <div style={{
            width: 28, height: 28, borderRadius: 2, border: "1px solid var(--sq-border-strong)", color: "var(--sq-smoke)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}><SQIcon name="shield" size={14}/></div>
          <div style={{ flex: 1 }}>
            <div className="sq-overline" style={{ marginBottom: 2 }}>
              Human-in-the-loop · draft {draft.status}
            </div>
            <div style={{ fontSize: 13, color: "var(--sq-bone)" }}>
              Spider-Qwen does not submit forms or send mail. Drafts are reviewed by an operator.
            </div>
          </div>
        </div>
      )}

      {/* Quote channel */}
      {qc && (
        <section>
          <div className="sq-overline" style={{ marginBottom: 10 }}>Quote channel</div>
          <div style={{
            display: "flex", alignItems: "center", gap: 12,
            padding: "14px 16px", border: "1px solid var(--sq-border)", background: "var(--sq-obsidian)",
          }}>
            <SQIcon name="link" size={18} color="var(--sq-smoke)"/>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontFamily: "var(--sq-font-mono)", fontSize: 13, color: "var(--sq-bone)",
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{qc.value}</div>
              <div style={{ fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-slate-taupe)", marginTop: 3, letterSpacing: "0.04em" }}>
                {qc.type} · evidence · {qc.evidence_ref ? qc.evidence_ref.ledger_id : "—"} · sha256:{qc.evidence_ref ? (qc.evidence_ref.snippet_hash || "").slice(0, 6) : "------"}
              </div>
            </div>
            <span className="sq-mono-chip" style={{ color: "#A6BB87", borderColor: "rgba(122,140,94,0.5)" }}>verified</span>
          </div>
        </section>
      )}

      {/* Checklist */}
      <section>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <div className="sq-overline">Required inputs · {done}/{checklist.length}</div>
          <span style={{ fontFamily: "var(--sq-font-mono)", fontSize: 11, color: "var(--sq-smoke)", letterSpacing: "0.06em" }}>
            completeness · {completeness.toFixed(2)}
          </span>
        </div>

        <div style={{ position: "relative", height: 1, background: "var(--sq-border)", marginBottom: 10 }}>
          <span style={{
            position: "absolute", left: 0, top: 0, height: 1, width: `${completeness * 100}%`,
            background: completeness === 1 ? "#A6BB87" : "var(--sq-bone)",
            transition: "width 280ms var(--sq-ease-sharp)",
          }}/>
        </div>

        <div style={{ border: "1px solid var(--sq-border)" }}>
          {checklist.map((c, i) => {
            const ok = itemOk(c);
            const needsInput = !c.evidence_ref;
            return (
              <div key={c.field} style={{
                display: "grid", gridTemplateColumns: "24px 200px 1fr",
                alignItems: "center", gap: 14, padding: "12px 16px",
                borderBottom: i < checklist.length - 1 ? "1px solid var(--sq-border)" : "none",
                background: ok ? "transparent" : "rgba(201,145,85,0.04)",
              }}>
                <span style={{
                  width: 18, height: 18,
                  border: `1px solid ${ok ? "rgba(122,140,94,0.6)" : "rgba(201,145,85,0.55)"}`,
                  display: "flex", alignItems: "center", justifyContent: "center", borderRadius: 2,
                  color: ok ? "#A6BB87" : "#D7A876",
                }}>
                  <SQIcon name={ok ? "check" : "info"} size={11}/>
                </span>
                <span style={{ fontFamily: "var(--sq-font-mono)", fontSize: 12, color: "var(--sq-bone)" }}>{c.field}</span>
                {needsInput ? (
                  <input
                    value={inputs[c.field] || ""}
                    onChange={(e) => setInputs((p) => ({ ...p, [c.field]: e.target.value }))}
                    placeholder={c.reason || "buyer to provide"}
                    style={{
                      background: "var(--sq-bg-input)", color: "var(--sq-bone)",
                      border: "1px solid var(--sq-border-strong)", borderRadius: 2,
                      fontFamily: "var(--sq-font-mono)", fontSize: 12, padding: "6px 10px", outline: "none",
                    }}
                  />
                ) : (
                  <span style={{ fontFamily: "var(--sq-font-sans)", fontSize: 13, color: "var(--sq-fg-muted)" }}>
                    {c.reason} · ev {c.evidence_ref.ledger_id}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </section>

      {/* Draft email */}
      <section>
        <div className="sq-overline" style={{ marginBottom: 10 }}>Draft email · preview</div>
        <pre style={{
          margin: 0, padding: "18px 18px",
          background: "var(--sq-obsidian)", border: "1px solid var(--sq-border)",
          fontFamily: "var(--sq-font-mono)", fontSize: 12, lineHeight: 1.7, color: "var(--sq-fog)",
          whiteSpace: "pre-wrap",
        }}>{`To:   ${qc ? qc.value : (vendor.contact || "<vendor channel>")}
From: <buyer-to-attach>
Subj: Request for Quotation — ${vendor.name}

${email}`}</pre>
      </section>

      {/* Assumptions */}
      {(draft.assumptions_and_limits || []).length > 0 && (
        <section>
          <div className="sq-overline" style={{ marginBottom: 10 }}>Assumptions and limits</div>
          <ul style={{ margin: 0, paddingLeft: 18, color: "var(--sq-fg-muted)", fontSize: 13, lineHeight: 1.6 }}>
            {draft.assumptions_and_limits.map((a, i) => <li key={i}>{a}</li>)}
          </ul>
        </section>
      )}

      <section style={{ display: "flex", gap: 10 }}>
        <button onClick={() => setSent(true)} className="sq-btn sq-btn-primary" style={{ flex: 1, height: 44 }} disabled={sent}>
          <SQIcon name="send" size={13}/>
          Hand off for human send
        </button>
        <button className="sq-btn sq-btn-secondary" style={{ height: 44, padding: "0 18px" }}>
          Re-validate evidence
        </button>
      </section>
    </Shell>
  );
}

// Drawer chrome shared by the loaded / empty states.
function Shell({ vendor, onClose, children }) {
  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)",
      zIndex: 60, display: "flex", justifyContent: "flex-end",
      animation: "sq-in-up 200ms var(--sq-ease-sharp) both",
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        width: "min(680px, 95vw)", height: "100%", background: "var(--sq-deep-shadow)",
        borderLeft: "1px solid var(--sq-border-strong)",
        boxShadow: "0 1px 0 rgba(255,255,255,0.05) inset, -24px 0 60px rgba(0,0,0,0.7)",
        overflow: "auto",
      }}>
        <div style={{
          padding: "22px 28px", borderBottom: "1px solid var(--sq-border)",
          display: "flex", alignItems: "center", justifyContent: "space-between",
        }}>
          <div>
            <div className="sq-overline">RFQ draft · review only</div>
            <h2 style={{ margin: "6px 0 0", fontFamily: "var(--sq-font-serif)", fontWeight: 500, fontSize: 28, color: "var(--sq-bone)" }}>
              {vendor.name}
            </h2>
          </div>
          <button onClick={onClose} className="sq-btn sq-btn-ghost" style={{ padding: "0 10px" }}>
            <SQIcon name="x" size={16}/>
          </button>
        </div>
        <div style={{ padding: "22px 28px", display: "flex", flexDirection: "column", gap: 26 }}>
          {children}
        </div>
      </div>
    </div>
  );
}

window.RfqDrawer = RfqDrawer;
