/* Spider-Qwen web client — talks to the FastAPI controller (/run, /classify)
 * and maps the deterministic RunResult into the workspace view-model.
 *
 * Every UI value traces back to a real backend field. Derived visuals (the
 * 6-spoke radial, "why this rank") are computed from the actual service-ranker
 * decomposition — backend score_components — never invented narrative.
 *
 * Plain ES (no JSX) so it loads before Babel compiles the components. */

(function () {
  "use strict";

  // ---- HTTP -----------------------------------------------------------------
  async function requestJSON(path, body, signal) {
    const res = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch (_) { detail = await res.text(); }
      throw new Error(`${path} failed (${res.status})${detail ? ": " + detail : ""}`);
    }
    return res.json();
  }

  const SQAPI = {
    classify: (query) => requestJSON("/classify", { query, offline: true }),
    run: (query, opts = {}) =>
      requestJSON("/run", { query, mode: opts.mode || "auto", country: opts.country || null, offline: opts.offline !== false }),
    config: () => requestJSON("/config"),
    start: (query, opts = {}) => requestJSON("/runs", {
      query, mode: opts.mode || "auto", country: opts.country || null,
      profile: opts.profile, idempotency_key: opts.idempotencyKey,
      requirements: opts.requirements || [], requirements_confirmed: opts.requirementsConfirmed || false,
      supplier_sources: opts.supplierSources || {},
    }),
    status: (id) => requestJSON(`/runs/${encodeURIComponent(id)}`),
    result: (id) => requestJSON(`/runs/${encodeURIComponent(id)}/result`),
    events: (id, after = 0) => requestJSON(`/runs/${encodeURIComponent(id)}/events?after=${after}`),
    cancel: (id) => requestJSON(`/runs/${encodeURIComponent(id)}/cancel`, {}),
    inspect: (id, operation, args, signal) => requestJSON(
      `/runs/${encodeURIComponent(id)}/inspect/${encodeURIComponent(operation)}`, args, signal),
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
    const refs = dedupeRefs([...(result.evidence_refs || []),
      ...(result.withheld_candidates || []).flatMap(c => c.evidence_refs || [])]);
    // Honest status: "proven" only when the backend shipped an RFC 6962
    // inclusion proof for that ledger_id; otherwise the row is "recorded".
    const proven = new Set((result.citation_proofs || []).map((p) => p.ledger_id));
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
        status: proven.has(r.ledger_id) ? "proven" : "recorded",
        sha: (r.snippet_hash || "").slice(0, 6) || "------",
        t: `${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`,
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
    const parts = c.score_components || {};
    return {
      suitability: pct((parts.suitability || 0) / 55 * 100),
      contactability: pct((parts.contactability || 0) / 25 * 100),
      evidence: pct((parts.evidence_quality || 0) / 20 * 100),
    };
  }

  function genericBreakdown(c) {
    const ev = pct((c.evidence_completeness || 0) * 100);
    const geo = pct(Math.max(0, c.geo_score || 0) / 20 * 100);
    return { evidence: ev, geo, score: pct(Math.min(100, c.score || 0)), refs: pct(Math.min(1, (c.evidence_refs || []).length / 3) * 100), match: ev, integrity: ev };
  }

  function serviceScoreParts(c) {
    return c.score_components || {};
  }

  function deriveTags(c, kind) {
    const tags = [];
    const stage = c.readiness && c.readiness.stage;
    const stageLabels = { discovered: "Discovered", evidence_checked: "Evidence-checked", review_ready: "Review-ready" };
    if (stageLabels[stage]) tags.push({ t: stageLabels[stage], kind: stage === "discovered" ? "plain" : "ok" });
    const country = c.country || "Global";
    tags.push({ t: SEA.includes(country) ? country + " · SEA" : country, kind: "plain" });
    const comp = c.evidence_completeness || 0;
    tags.push(comp >= 1 ? { t: "All fields sourced", kind: "ok" }
            : comp >= 0.65 ? { t: "Evidence " + comp.toFixed(2), kind: "plain" }
            : { t: "Thin evidence", kind: "warn" });
    if (kind === "service" && c.quote_channel) tags.push({ t: c.quote_channel.type, kind: "plain" });
    if ((c.conflict_penalty || 0) < 0 || c.pricing_status === "CONFLICTING") tags.push({ t: "Conflicting", kind: "risk" });
    return tags;
  }

  function deriveWhy(c, kind, parts) {
    if (kind !== "service") return [
      `Evidence completeness ${(c.evidence_completeness || 0).toFixed(2)} across ${(c.evidence_refs || []).length} ledger refs`,
      `Geography: ${c.country || "unknown"}`,
    ];
    const why = [
      `Suitability ${parts.suitability ?? "unavailable"} / 55 pts · evidenced service, geography and checklist`,
      `Contactability ${parts.contactability ?? "unavailable"} / 25 pts`,
      `Evidence quality ${parts.evidence_quality ?? "unavailable"} / 20 pts across ${(c.evidence_refs || []).length} refs`,
    ];
    if (!c.quote_channel) why.push("No quotation channel recorded");
    if ((c.conflict_penalty || 0) < 0) why.push(`Conflict penalty ${c.conflict_penalty} pts · review competing claims`);
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

  function candidateKey(candidate, fallback = "") {
    const supplier = candidate && (candidate.supplier_id || candidate.vendor_name) || fallback;
    const offering = candidate && candidate.offering_id;
    return offering ? `${supplier}::${offering}` : supplier;
  }

  function toVendor(c, i, ledger, trustByVendor) {
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
      id: candidateKey(c, "v" + (i + 1)),
      supplierId: c.supplier_id || "",
      offeringId: c.offering_id || "",
      rank: i + 1,
      kind,
      name: c.vendor_name,
      country: c.country || "—",
      website: c.website || "",
      domain: hostOf(c.website),
      score: Math.round((c.score || 0) * 10) / 10,
      scoreCap: 100,
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
      trust: (trustByVendor && trustByVendor[candidateKey(c)]) || null,
      raw: c,
    };
  }

  // ---- qwen seams (honesty surface) -----------------------------------------
  // Summarise qwen_paths into a truthful label: which seams ran live vs mocked,
  // and which model. An offline run says "offline · mocks", never "qwen live".
  function qwenSummary(qp) {
    if (!qp || !Object.keys(qp).length) return { label: "seams · unknown", live: 0, mocked: 0, model: "", offline: true, seams: [] };
    const seams = Object.entries(qp)
      .filter(([k, v]) => k !== "offline" && v && typeof v === "object")
      .map(([k, v]) => ({ name: k, enabled: !!v.enabled, mock: !!v.mock, model: v.model || "" }));
    const enabled = seams.filter((s) => s.enabled);
    const live = enabled.filter((s) => !s.mock);
    const model = (live.find((s) => s.model) || enabled.find((s) => s.model) || {}).model || "";
    const label = !enabled.length
      ? "offline · deterministic"
      : qp.offline || !live.length
      ? `offline · ${enabled.length} mock seam${enabled.length === 1 ? "" : "s"}`
      : `${live.length} live seam${live.length === 1 ? "" : "s"} · ${model || "qwen"}`;
    return { label, live: live.length, mocked: enabled.length - live.length, model, offline: !!qp.offline, seams };
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
  const PHASE_ORDER = ["classify", "budget", "search", "fetch", "extract", "consolidate", "rank", "verify", "draft", "persist"];

  function runEventLine(event, startedAt) {
    const detail = event.detail || {};
    const phases = { discovery: "search", retrieval: "fetch", extraction: "extract",
      verification: "verify", consolidation: "consolidate", ranking: "rank", rfq: "draft" };
    const sourcePhase = event.phase || detail.phase;
    const phase = phases[sourcePhase] || (PHASE_ORDER.includes(sourcePhase) ? sourcePhase : "run");
    const timestamp = Date.parse(event.created_at);
    return { phase,
      t: Number.isFinite(timestamp) ? Math.max(0, timestamp - startedAt) : 0,
      text: typeof event.message === "string" ? event.message : event.kind || "Run updated" };
  }

  function reasoningLine(phase, result) {
    if (!result) return PHASE_PLACEHOLDER[phase];
    const m = result.metrics || {};
    const b = result.budget || {};
    const cls = result.classification || {};
    const rsn = result.reasoning || {};
    const crag = rsn.crag || {};
    const fr = m.frontier || {};
    const top = (result.validated_candidates || [])[0];
    switch (phase) {
      case "classify": return `Mode -> ${result.mode} · confidence ${(cls.confidence || 0).toFixed(2)}`;
      case "budget":   return `Budget envelope · ${b.max_search_calls ?? "?"} search · ${b.max_fetch_urls ?? "?"} fetches max`;
      case "search": {
        const base = `Search · ${m.search_calls_total ?? b.search_calls ?? 0} calls`;
        const cragBit = crag.verdict ? ` · CRAG ${crag.verdict} (${(crag.confidence ?? 0).toFixed(2)})` : "";
        const pivots = (rsn.corrective_queries || []).length + (rsn.replan_queries || []).length;
        return base + cragBit + (pivots ? ` · ${pivots} pivot quer${pivots === 1 ? "y" : "ies"}` : "");
      }
      case "fetch": {
        const rec = m.fetch_fallback_recovered || 0;
        return `Fetch · ${m.fetch_urls_total ?? b.fetch_urls ?? 0} URLs retrieved`
          + (rec ? ` · ${rec} recovered via fallback` : "") + " · SEA-first";
      }
      case "extract": {
        const ent = fr.entity_url_leads_fetched || 0;
        return `Extract · quote-channel found rate ${(m.quote_channel_found_rate ?? 0).toFixed(2)}`
          + (fr.enabled ? ` · frontier ${fr.rounds ?? 0} round(s)${ent ? `, ${ent} entity lead(s) drained` : ""}` : "");
      }
      case "rank":     return `Rank · ${m.validated_candidates_total ?? 0} validated · top score ${top ? (top.score || 0).toFixed(1) : "n/a"}`;
      case "draft":    return `Draft · ${m.rfq_drafts_total ?? 0} RFQ draft(s) · ${m.rfq_incomplete_total ?? 0} incomplete`;
      case "persist": {
        const proofs = (result.citation_proofs || []).length;
        const lat = (m.latency_seconds || {}).total;
        return `Ledger committed · ${(result.evidence_refs || []).length} evidence refs`
          + (proofs ? ` · ${proofs} inclusion proof(s)` : "")
          + (lat != null ? ` · ${lat.toFixed(1)}s` : "") + ` · stop: ${result.stop_reason}`;
      }
      default:         return PHASE_PLACEHOLDER[phase] || "";
    }
  }

  // Real discovery trace lines (result.reasoning), appended to the hunting
  // stream once the run lands — replaces invented narrative with the actual
  // initial queries, CRAG verdict, and replan pivots.
  function traceLines(result) {
    if (!result || !result.reasoning) return [];
    const rsn = result.reasoning;
    const lines = [];
    for (const q of (rsn.initial_queries || []).slice(0, 3)) {
      lines.push({ phase: "search", text: `query · "${q}"` });
    }
    if (rsn.crag && rsn.crag.verdict) {
      lines.push({ phase: "search", text: `CRAG verdict · ${rsn.crag.verdict} · mean relevance ${(rsn.crag.mean_relevance ?? 0).toFixed(2)} — ${rsn.crag.rationale || ""}` });
    }
    for (const q of (rsn.corrective_queries || []).slice(0, 2)) {
      lines.push({ phase: "search", text: `corrective pivot · "${q}"` });
    }
    for (const q of (rsn.replan_queries || []).slice(0, 2)) {
      lines.push({ phase: "rank", text: `replan pivot · "${q}"` });
    }
    if (rsn.query_rewriter) {
      lines.push({ phase: "search", text: `query rewriter · ${rsn.query_rewriter}` });
    }
    return lines;
  }

  // ---- signals (derived from the run) --------------------------------------
  function toSignals(result, vendors) {
    const sig = [];
    const top = vendors[0];
    if (top) sig.push({ id: "sig_top", kind: "ok", ovl: "Top ranked supplier", title: top.name, meta: `score ${top.score} · ${top.evidence.length} refs` });
    for (const c of result.withheld_candidates || []) {
      sig.push({ id: "withheld_" + candidateKey(c), kind: "risk", ovl: "Supplier withheld",
        title: c.vendor_name, meta: `Conflicting claims: ${(c.conflicting_fields || []).join(", ")}` });
    }
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
    const trustByVendor = {};
    for (const t of result.trust_verdicts || []) {
      if (t && (t.supplier_id || t.vendor_name)) trustByVendor[candidateKey(t)] = t;
    }
    const vendors = (result.validated_candidates || []).map((c, i) => toVendor(c, i, ledger, trustByVendor));
    const m = result.metrics || {};
    return {
      result,
      vendors,
      withheldVendors: (result.withheld_candidates || []).map((c, i) => toVendor(c, i, ledger, trustByVendor)),
      ledger,
      signals: toSignals(result, vendors),
      classification: result.classification || {},
      metrics: m,
      budget: result.budget || {},
      pricingSummary: result.pricing_status_summary || {},
      rfqByVendor: indexRfqByVendor(result),
      qwen: qwenSummary(result.qwen_paths),
      reasoning: result.reasoning || null,
      frontier: m.frontier || { enabled: false },
      latency: m.latency_seconds || null,
      fetchOutcomes: m.fetch_outcomes || {},
      fallbackRecovered: m.fetch_fallback_recovered || 0,
      proofs: (result.citation_proofs || []).length,
    };
  }

  function indexRfqByVendor(result) {
    const map = {};
    for (const d of result.rfq_drafts || []) {
      const vendor = d.vendor || {};
      if (vendor.supplier_id) map[candidateKey(vendor)] = d;
      else {
        const matches = (result.validated_candidates || []).filter(c =>
          c.vendor_name === vendor.vendor_name && c.website === vendor.website);
        if (matches.length === 1 && matches[0].supplier_id) map[candidateKey(matches[0])] = d;
      }
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
  window.SQMAP = { mapResult, reasoningLine, traceLines, toSignals, toLedger, toVendor, qwenSummary, runEventLine };
  window.SQDATA = { PIPELINE_STEPS, PHASE_ORDER, PHASE_PLACEHOLDER, recentRuns, pushRecentRun };
})();
