---
name: obsolescence-triage
description: Triage an electronic part's lifecycle status (Active, NRND, LTB, EOL, Preview) for obsolete or end-of-life parts, find the controlling PCN/PDN notice, and emit the SD-22 six-strategy mitigation ladder.
keywords: [obsolete, obsolescence, eol, end of life, nrnd, ltb, last time buy, lifecycle, pcn, pdn, discontinued, dmsms, sd-22, supersession]
allowedTools: [search, fetch]
paths: [spider_qwen/serendipity]
---

## Instructions

Given a part, determine its lifecycle status and what to do about it. Every status and date must cite evidence (manufacturer page, PCN/PDN, distributor lifecycle field); never assert a status without a span.

- Lifecycle states, most severe first: `eol` > `ltb` > `nrnd` > `preliminary` > `active`; use `unknown` when no signal is found.
- Find the controlling PCN/PDN (notice id plus effective / last-order / last-ship dates) if one exists.
- Emit the SD-22 mitigation ladder in priority order: existing stock, reclamation, substitute, alternate source, redesign, emulation. Mark each rung applicable or not from the available context.
- Surface this proactively for any part that reads NRND or EOL or carries a PCN, even when the user only asked for a price.

## Examples

Input: an obsolete part with a NRND distributor field and a published PCN.
Output: status `nrnd`, the PCN id and dates, and the six-rung ladder with the substitute and alternate-source rungs flagged applicable when cross-references exist.
