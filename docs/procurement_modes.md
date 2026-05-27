# Procurement Modes

Defined in `modes/contracts.py`; routing in `modes/router.py`.

## product_exact_price
Products with expected public pricing (chairs, paper, keyboards).
**Success:** ≥ `min_validated_candidates`, each with a numeric price or precise
proxy, each price evidenced. Ranker weights: price 30, MOQ 15, vendor/contact 20,
geo 15, evidence 20.

## service_quote_required
Services where price is quote-only (cleaning, maintenance, security, pest
control, managed IT). **v1 boundary: RFQ draft only.**
**Success:** ≥1 evidenced quote channel, service-match evidence, checklist
completeness ≥ threshold, RFQ draft created. Ranker weights: service-match 25,
quote-channel 25, geo 20, contact 15, checklist 10, conflict penalty −20 max.

## contact_enrichment_only
You have vendors, need contacts. Output: contacts with `confidence`,
`privacy_class`, `evidence_ref`, and `validation_signals` (domain_match,
marketplace_excluded, cross_source_count). Promotion to semantic memory: 2+
independent sources, or 1 high-confidence source + domain ownership.

## revalidation
Refresh a stale semantic fact. **Manual only in v1** (`memory/revalidation.py`);
contradicting evidence marks the fact `disputed` rather than overwriting.

## Pricing ontology (`extraction/pricing.py`)

| Text | Status |
|---|---|
| `$10 per unit` | EXACT_PRICE |
| `$10–$20 per unit` | PRICE_RANGE |
| `From $99/month` | STARTING_FROM |
| `Download our rate card` | RATE_CARD_FOUND |
| `Request a quotation` | QUOTE_REQUIRED |
| `Contact sales for pricing` | CONTACT_FOR_PRICING |
| no pricing/quote evidence | NOT_FOUND |
| two incompatible prices | CONFLICTING |

Missing price is only a failure in `product_exact_price`.
