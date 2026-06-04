---
name: sea-rfq-draft
description: Produce an evidence-backed RFQ draft for a SEA buyer in market-neutral English, filling only template slots that are grounded in evidence, emitting language metadata, and never submitting or sending anything.
keywords: [rfq, request for quotation, draft, quote, sea, southeast asia, singapore, procurement, template, vendor outreach, evidence-backed]
allowedTools: [search, fetch]
paths: [spider_qwen/rfq]
---

## Instructions

Draft an RFQ a buyer can review and send themselves. The draft is never submitted or sent (the audit log refuses `rfq_sent` / `email_send`); produce text only.

- Fill only the template slots backed by evidence (vendor, quote channel, service/part scope). Every filled slot must cite a ledger_id; leave a slot blank with an assumption note rather than guessing.
- Exclude any fact whose status is `disputed` — it must not appear in the draft.
- Hard-stop to `incomplete` when there is no evidenced quote channel or checklist completeness is below the policy threshold; say so in the assumptions.
- Tone: SEA-market-neutral professional English — short, direct, polite. Do not assume a country dialect; emit a language-metadata tag (e.g. `en-SG-neutral`) instead.
- Always carry the draft-only disclaimer and the inputs checklist.

## Examples

Input: a validated Singapore cleaning vendor with an evidenced contact-email quote channel and a complete checklist.
Output: a `complete` RFQ draft addressed to the vendor, scope/pricing-basis/lead-time questions, the inputs checklist, `language: en-SG-neutral`, the draft-only disclaimer, and evidence_refs for every filled slot — ready for the buyer to send, sent by nobody.
