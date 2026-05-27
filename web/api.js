/* Spider-Qwen web client — talks to the FastAPI controller (/run, /classify)
 * and maps the deterministic RunResult into the workspace view-model.
 *
 * Every UI value traces back to a real backend field. Derived visuals (the
 * 6-spoke radial, "why this rank") are computed from the actual service-ranker
 * decomposition — service_match*25 + channel*25 + geo*20 + contact*15 +
 * checklist*10 + conflict_penalty — never invented narrative.
 *
 * Plain ES (no JSX) so it loads before Babel compiles the components. */

(function () {
  "use strict";

  // ---- HTTP -----------------------------------------------------------------
  async function postJSON(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch (_) { detail = await res.text(); }
      throw new Error(`${path} failed (${res.status})${detail ? ": " + detail : ""}`);
    }
    return res.json();
  }

  const SQAPI = {
    classify: (query) => postJSON("/classify", { query, offline: true }),
    run: (query, opts = {}) =>
      postJSON("/run", { query, mode: opts.mode || "auto", country: opts.country || null, offline: opts.offline !== false }),
  };

  // ---- helpers --------------------------------------------------------------
  const SEA = ["Singapore", "Malaysia", "Indonesia", "Thailand", "Vietnam", "Philippines", "Brunei", "Cambodia", "Laos", "Myanmar"];

  // Quote-channel quality, mirrored from spider_qwen/ranking/service_ranker.py.
  const CHANNEL_QUALITY = {
    rfq_form: 1.0, contact_email: 0.9, contact_page: 0.6,
    phone: 0.5, rate_card: 0.4, portal_login_required: 0.2,
  };

  const clamp = (n, lo = 0, hi = 100) => Math.max(lo, Math.min(hi, n));
  const pct = (n) => clamp(Math.round(n));

  function hostOf(url) {
    if (!url) return "";
    try { return new URL(url).host.replace(/^www\./, ""); }
    catch (_) { return String(url).replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0]; }
  }
  function registrable(url) {
    const h = hostOf(url);
    const parts = h.split(".");
    return parts.length >= 2 ? parts.slice(-2).join(".") : h;
  }
  function shortenUrl(url) {
    const h = hostOf(url);
    let path = "";
    try { path = new URL(url).pathname; } catch (_) { path = "/" + String(url).split("/").slice(3).join("/"); }
    if (path.length > 30) path = path.slice(0, 29) + "…";
    return path && path !== "/" ? h + path : h;
  }
  function clock(iso) {
    // Render a ledger timestamp as mm:ss within the run window — relative to the
    // earliest ref so the column reads like a run clock, not wall time.
    const d = Date.parse(iso);
    return Number.isNaN(d) ? "00:00" : d;
  }

  // ---- ledger / evidence ----------------------------------------------------
  function deriveKind(url) {
    const u = (url || "").toLowerCase();
    if (/request-a-quote|\/quote|rate-card|pricing|contact|iso|reference/.test(u)) return "extract";
    if (/registry|acra|ssm|nib|search/.test(u)) return "search";
    return "fetch";
  }

  function toLedger(result) {
    const refs = dedupeRefs(result.evidence_refs || []);
    const times = refs.map((r) => clock(r.retrieved_at)).filter((n) => typeof n === "number");
    const base = times.length ? Math.min(...times) : 0;
    return refs.map((r) => {
      const ms = clock(r.retrieved_at);
      const secs = typeof ms === "number" && base ? Math.max(0, Math.round((ms - base) / 1000)) : 0;
      return {
        id: r.ledger_id,
        src: shortenUrl(r.url),
        url: r.url,
        kind: deriveKind(r.url),
        status: "verified",
        sha: (r.snippet_hash || "").slice(0, 6) || "------",
        t: `00:${String(secs % 60).padStart(2, "0")}`,
      };
    });
  }

  function dedupeRefs(refs) {
    const seen = new Set();
    const out = [];
    for (const r of refs) {
      if (!r || seen.has(r.ledger_id)) continue;
      seen.add(r.ledger_id);
      out.push(r);
    }
    return out;
  }

  // ---- candidate -> vendor view-model --------------------------------------
  function candidateKind(c) {
    if ("quote_channel" in c || "service_match_score" in c) return "service";
    if ("product_url" in c || "price" in c) return "product";
    if ("contacts" in c) return "contact";
    return "service";
  }

  function serviceBreakdown(c) {
    // Each axis 0..100, normalised against its point cap in the service ranker.
    const channelQ = c.quote_channel ? (CHANNEL_QUALITY[c.quote_channel.type] || 0) : 0;
    const contactPts = c.quote_channel ? (c.quote_channel.type === "contact_email" ? 15 : 10) : 0;
    const integrity = (20 + Math.max(-20, c.conflict_penalty || 0)) / 20 * 100;
    return {
      match: pct(Math.min(1, c.service_match_score || 0) * 100),
      channel: pct(channelQ * 100),
      geo: pct(Math.max(0, c.geo_score || 0) / 20 * 100),
      contact: pct(contactPts / 15 * 100),
      checklist: pct(Math.min(1, c.checklist_completeness || 0) * 100),
      integrity: pct(integrity),
    };
  }

  function genericBreakdown(c) {
    const ev = pct((c.evidence_completeness || 0) * 100);
    const geo = pct(Math.max(0, c.geo_score || 0) / 20 * 100);
    return { evidence: ev, geo, score: pct(Math.min(100, c.score || 0)), refs: pct(Math.min(1, (c.evidence_refs || []).length / 3) * 100), match: ev, integrity: ev };
  }

  function serviceScoreParts(c) {
    const channelQ = c.quote_channel ? (CHANNEL_QUALITY[c.quote_channel.type] || 0) : 0;
    return {
      match: +(Math.min(1, c.service_match_score || 0) * 25).toFixed(1),
      channel: +(channelQ * 25).toFixed(1),
      geo: +(Math.max(0, c.geo_score || 0) / 20 * 20).toFixed(1),
      contact: c.quote_channel ? (c.quote_channel.type === "contact_email" ? 15 : 10) : 0,
      checklist: +(Math.min(1, c.checklist_completeness || 0) * 10).toFixed(1),
      penalty: Math.max(-20, c.conflict_penalty || 0),
    };
  }

  function deriveTags(c, kind) {
    const tags = [];
    const country = c.country || "Global";
    tags.push({ t: SEA.includes(country) ? country + " · SEA" : country, kind: "plain" });
    const comp = c.evidence_completeness || 0;
    tags.push(comp >= 1 ? { t: "Fully evidenced", kind: "ok" }
            : comp >= 0.65 ? { t: "Evidence " + comp.toFixed(2), kind: "plain" }
            : { t: "Thin evidence", kind: "warn" });
    if (kind === "service" && c.quote_channel) tags.push({ t: c.quote_channel.type, kind: "plain" });
    if ((c.conflict_penalty || 0) < 0 || c.pricing_status === "CONFLICTING") tags.push({ t: "Conflicting", kind: "risk" });
    return tags;
  }

  function deriveWhy(c, kind, parts) {
    if (kind !== "service") {
      const why = [`Evidence completeness ${(c.evidence_completeness || 0).toFixed(2)} across ${(c.evidence_refs || []).length} ledger refs`];
      if (c.country) why.push(`Geo: ${c.country} (SEA-first boost applied)`);
      if (c.pricing_status) why.push(`Pricing status: ${c.pricing_status}`);
      return why;
    }
    const why = [
      `Service match ${c.service_match_evidence ? "evidence-backed" : "weak"} · score ${(c.service_match_score || 0).toFixed(2)} → ${parts.match} / 25 pts`,
    ];
    if (c.quote_channel) why.push(`Quote channel ${c.quote_channel.type} → ${parts.channel} / 25 pts (+${parts.contact} reliability)`);
    else why.push("No quote channel found — not RFQ-ready");
    why.push(`Geo relevance ${c.country || "unknown"} → ${parts.geo} / 20 pts`);
    why.push(`Evidence completeness ${(c.evidence_completeness || 0).toFixed(2)} across ${(c.evidence_refs || []).length} ledger refs`);
    if (parts.penalty < 0) why.push(`Conflict penalty ${parts.penalty} pts applied — revalidate before RFQ`);
    return why;
  }

  function deriveSummary(c, kind) {
    const country = c.country || "unknown region";
    const n = (c.evidence_refs || []).length;
    if (kind === "service") {
      const ch = c.quote_channel ? `quote channel via ${c.quote_channel.type}` : "no quote channel found";
      return `Service match ${c.service_match_score >= 0.5 ? "strong" : "partial"}; ${ch}. ${n} evidence ref${n === 1 ? "" : "s"}, completeness ${(c.evidence_completeness || 0).toFixed(2)}. Located in ${country}.`;
    }
    if (kind === "product") {
      const price = c.price != null ? `${c.currency || ""} ${c.price} / ${c.unit || "unit"}`.trim() : "no public price";
      return `Product candidate (${price}); pricing ${c.pricing_status}. ${n} evidence refs in ${country}.`;
    }
    const contacts = (c.contacts || []).length;
    return `Contact enrichment: ${contacts} contact${contacts === 1 ? "" : "s"} extracted; completeness ${(c.evidence_completeness || 0).toFixed(2)} in ${country}.`;
  }

  function toVendor(c, i, ledger) {
    const kind = candidateKind(c);
    const refs = dedupeRefs(c.evidence_refs || []);
    const parts = kind === "service" ? serviceScoreParts(c) : null;
    const breakdown = kind === "service" ? serviceBreakdown(c) : genericBreakdown(c);
    const ledgerById = new Map(ledger.map((e) => [e.id, e]));
    const contactVal = kind === "service"
      ? (c.quote_channel ? c.quote_channel.value : c.website)
      : kind === "contact" ? ((c.contacts || [])[0] || {}).value
      : c.website;
    return {
      id: "v" + (i + 1),
      rank: i + 1,
      kind,
      name: c.vendor_name,
      country: c.country || "—",
      website: c.website || "",
      domain: registrable(c.website),
      score: Math.round((c.score || 0) * 10) / 10,
      scoreCap: kind === "service" ? 95 : 100,
      breakdown,
      scoreParts: parts,
      reliability: pct((c.evidence_completeness || 0) * 100),
      completeness: c.evidence_completeness || 0,
      pricing: c.pricing_status || "NOT_FOUND",
      match: kind === "service"
        ? `service_match · ${c.service_match_score >= 0.5 ? "strong" : "partial"}`
        : kind + " candidate",
      contact: contactVal || "—",
      quoteType: c.quote_channel ? c.quote_channel.type : null,
      sha: (refs[0] && refs[0].snippet_hash || "").slice(0, 6) || "------",
      tags: deriveTags(c, kind),
      evidence: refs.map((r) => r.ledger_id),
      ledger: refs.map((r) => ledgerById.get(r.ledger_id)).filter(Boolean),
      summary: deriveSummary(c, kind),
      why: deriveWhy(c, kind, parts || {}),
      raw: c,
    };
  }

  // ---- reasoning trace (derived from the real result) -----------------------
  // Generic process text shown while /run is in flight; real per-phase text
  // swaps in once the result is back.
  const PHASE_PLACEHOLDER = {
    classify: "Parsing intent and procurement mode…",
    budget: "Allocating budget envelope…",
    search: "Querying SEA-first sources…",
    fetch: "Fetching candidate vendor pages…",
    extract: "Extracting quote channels and service match…",
    rank: "Scoring candidates by weighted factors…",
    draft: "Drafting RFQs for human review…",
    persist: "Committing evidence to append-only ledger…",
  };
  const PHASE_ORDER = ["classify", "budget", "search", "fetch", "extract", "rank", "draft", "persist"];

  function reasoningLine(phase, result) {
    if (!result) return PHASE_PLACEHOLDER[phase];
    const m = result.metrics || {};
    const b = result.budget || {};
    const cls = result.classification || {};
    const top = (result.validated_candidates || [])[0];
    switch (phase) {
      case "classify": return `Mode -> ${result.mode} · confidence ${(cls.confidence || 0).toFixed(2)}`;
      case "budget":   return `Budget envelope · ${b.max_search_calls ?? "?"} search · ${b.max_fetch_urls ?? "?"} fetches max`;
      case "search":   return `Search · ${m.search_calls_total ?? b.search_calls ?? 0} calls · ${m.candidates_considered ?? 0} candidates surfaced`;
      case "fetch":    return `Fetch · ${m.fetch_urls_total ?? b.fetch_urls ?? 0} URLs retrieved · SEA-first`;
      case "extract":  return `Extract · quote-channel found rate ${(m.quote_channel_found_rate ?? 0).toFixed(2)}`;
      case "rank":     return `Rank · ${m.validated_candidates_total ?? 0} validated · top score ${top ? (top.score || 0).toFixed(1) : "n/a"}`;
      case "draft":    return `Draft · ${m.rfq_drafts_total ?? 0} RFQ draft(s) · ${m.rfq_incomplete_total ?? 0} incomplete`;
      case "persist":  return `Ledger committed · ${(result.evidence_refs || []).length} evidence refs · stop: ${result.stop_reason}`;
      default:         return PHASE_PLACEHOLDER[phase] || "";
    }
  }

  // ---- signals (derived from the run) --------------------------------------
  function toSignals(result, vendors) {
    const sig = [];
    const top = vendors[0];
    if (top) sig.push({ id: "sig_top", kind: "ok", ovl: "Top match validated", title: top.name, meta: `score ${top.score} · ${top.evidence.length} refs` });
    for (const v of vendors) {
      if (v.tags.some((t) => t.kind === "risk")) {
        sig.push({ id: "sig_" + v.id, kind: "risk", ovl: "Conflicting evidence", title: `${v.name} · pricing`, meta: "Action required · revalidate" });
      }
    }
    const ps = result.pricing_status_summary || {};
    const quoteReq = ps.QUOTE_REQUIRED || ps.CONTACT_FOR_PRICING || 0;
    if (quoteReq) sig.push({ id: "sig_quote", kind: "info", ovl: "Quote-required", title: `${quoteReq} vendor(s) need an RFQ`, meta: "Drafts ready for review" });
    const pending = (result.metrics || {}).pending_reviews || 0;
    if (pending) sig.push({ id: "sig_review", kind: "warn", ovl: "Human review queued", title: `${pending} item(s) await sign-off`, meta: "HITL · open review queue" });
    if (!sig.length) sig.push({ id: "sig_done", kind: "info", ovl: "Run complete", title: result.query, meta: `stop: ${result.stop_reason}` });
    return sig.slice(0, 6);
  }

  // ---- top-level mapper -----------------------------------------------------
  function mapResult(result) {
    const ledger = toLedger(result);
    const vendors = (result.validated_candidates || []).map((c, i) => toVendor(c, i, ledger));
    return {
      result,
      vendors,
      ledger,
      signals: toSignals(result, vendors),
      classification: result.classification || {},
      metrics: result.metrics || {},
      budget: result.budget || {},
      pricingSummary: result.pricing_status_summary || {},
      rfqByVendor: indexRfqByVendor(result),
    };
  }

  function indexRfqByVendor(result) {
    const map = {};
    for (const d of result.rfq_drafts || []) {
      const name = d.vendor && d.vendor.vendor_name;
      if (name) map[name] = d;
    }
    return map;
  }

  // ---- recent runs (localStorage — real history, no fabrication) ------------
  const RECENT_KEY = "sq_recent_runs_v1";
  function recentRuns() {
    try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); }
    catch (_) { return []; }
  }
  function pushRecentRun(result) {
    const m = result.metrics || {};
    const verdict = `${m.validated_candidates_total ?? 0} validated · comp ${(m.quote_channel_found_rate ?? 0).toFixed(2)}`;
    const entry = {
      q: result.query, mode: result.mode, verdict,
      t: new Date().toLocaleString(undefined, { hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" }),
    };
    const list = [entry, ...recentRuns().filter((r) => r.q !== entry.q)].slice(0, 6);
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch (_) {}
    return list;
  }

  // Static pipeline definition (presentation only — mirrors the controller phases).
  const PIPELINE_STEPS = PHASE_ORDER.map((k) => ({ k, label: k }));

  window.SQAPI = SQAPI;
  window.SQMAP = { mapResult, reasoningLine, toSignals, toLedger, toVendor };
  window.SQDATA = { PIPELINE_STEPS, PHASE_ORDER, PHASE_PLACEHOLDER, recentRuns, pushRecentRun };
})();
