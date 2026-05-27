# Ranking

Rankers are intentionally transparent weighted sums. Scores are bounded by
normalizing each feature before applying weights.

## Product ranker

- `price_quality`: 30
- `moq availability`: 15
- `vendor/contact completeness`: 20
- `geo relevance`: 15
- `evidence completeness`: 20

Missing values score `0` for that feature. `CONFLICTING` pricing is retained as
a visible status but receives low price quality.

## Service ranker

- `service_match_quality`: 25
- `quote_channel_quality`: 25
- `geo_relevance`: 20
- `contact_reliability`: 15
- `checklist_completeness`: 10
- `conflict_penalty`: up to `-20`

No evidenced quote channel means the service candidate cannot produce a polished
RFQ. A recalled memory quote channel is first re-recorded into the current run
ledger as `semantic_memory` evidence before it can affect ranking.

## Contact ranker

Contact candidates prefer higher-confidence contacts, domain match, SEA
relevance, and evidence completeness. Named-person contacts are privacy-gated by
policy.
