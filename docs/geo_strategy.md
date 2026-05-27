# Geo Strategy

SEA-first, global fallback. **SEA-first is a ranking boost, not a hard filter.**
(`ranking/geo_strategy.py`)

## Default region

SEA boost countries: Singapore, Malaysia, Indonesia, Thailand, Vietnam,
Philippines, Brunei, Cambodia, Laos, Myanmar. Fallback: global.

## Query templates

Local procurement phrasing first:

```
{service} Singapore request quotation
{service} Singapore for quotation
{service} Singapore RFQ
{service} Singapore contact sales
{service} Malaysia quotation
{service} Southeast Asia vendor
```

Global templates (`{x} vendor`, `{x} request quotation`, …) are used only as a
fallback.

## Fallback rule

If no SEA candidate validates after the budgeted search/fetch attempts, the
controller re-gathers with the global templates (budget permitting), then
re-ranks the merged, deduped candidate set.

## Geo relevance scoring

| Signal | Score |
|---|--:|
| exact target-country match | +20 |
| SEA regional match | +12 |
| serves region (mentioned in page text) | +6 |
| unknown geography | 0 |
| conflicting geography | −10 |

The raw geo score feeds each ranker's geo-relevance component (normalized to its
weight). Target country is detected from the query or passed via `--country`.
