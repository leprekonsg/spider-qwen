/* Optional producer adapter. Procurement logic and access checks stay on the server. */
(function () {
  "use strict";
  let currentRunId = null;
  let registration = null;
  const definitions = [
    ["get_current_run", "Read the completed procurement run currently displayed.", {}],
    ["list_candidates", "List the current run's suppliers with stable IDs, scores and evidence references.", {}],
    ["get_candidate_evidence", "Read a supplier's recorded evidence, conflicting claims and citation proofs.", {
      supplier_id: { type: "string", minLength: 1, maxLength: 128 }
    }],
    ["compare_candidates", "Compare 2 to 10 suppliers from the current completed run using their recorded claims and scores.", {
      supplier_ids: { type: "array", minItems: 2, maxItems: 10, uniqueItems: true,
        items: { type: "string", minLength: 1, maxLength: 128 } }
    }],
    ["get_rfq_draft", "Read an existing unsent RFQ draft for a supplier in the current run.", {
      supplier_id: { type: "string", minLength: 1, maxLength: 128 }
    }],
  ];

  async function register() {
    const context = document.modelContext;
    if (!currentRunId || registration || !context || typeof context.registerTool !== "function" || window.top !== window) return;
    const controller = new AbortController();
    registration = controller;
    try {
      for (const [name, description, properties] of definitions) {
        if (controller.signal.aborted) break;
        await context.registerTool({
          name, description,
          inputSchema: { type: "object", properties, required: Object.keys(properties), additionalProperties: false },
          annotations: { readOnlyHint: true, untrustedContentHint: true, consequentialHint: false },
          execute: async (args = {}, options = {}) => {
            const runId = currentRunId;
            if (!runId) throw new Error("Complete a procurement run before inspecting its results.");
            return window.SQAPI.inspect(runId, name, args, options.signal);
          },
        }, { signal: controller.signal });
      }
    } catch (_) {
      // A draft API or failed registration must not break the normal UI.
      controller.abort();
      if (registration === controller) registration = null;
    }
  }

  function clear() {
    currentRunId = null;
    if (registration) registration.abort();
    registration = null;
  }

  window.SQWebMCP = {
    setCurrentRun(runId) { clear(); currentRunId = runId; return register(); },
    clear,
  };
  window.addEventListener("pagehide", () => {
    if (registration) registration.abort();
    registration = null;
  });
  window.addEventListener("pageshow", register);
})();
