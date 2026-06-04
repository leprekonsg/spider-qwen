---
name: mpn-canonicalize
description: Extract and normalize part identifiers (MPN, CPN, NSN, CAGE, GTIN, HS code) from text or datasheets into a strict JSON schema; resolve hyphen and suffix variants.
keywords: [mpn, manufacturer part number, cpn, nsn, cage, gtin, hs code, canonicalize, normalize, identifier, datasheet, part number]
allowedTools: [fetch]
paths: [spider_qwen/extraction, spider_qwen/graph]
---

## Instructions

Identify every part identifier in the input and emit one canonical record per part. Do not invent identifiers; only emit values present in the source text, each with the exact span it came from.

- Canonical MPN: uppercase, keep the manufacturer's significant separators, and move packaging or reel/tape/quantity suffixes into a separate `variant` field (e.g. `-T`, `-TR`, `-REEL`, `(50)`).
- Record the identifier type explicitly: one of `mpn`, `cpn`, `nsn`, `cage`, `gtin`, `hs`.
- Group hyphen and suffix variants of one base part under a single `base_mpn`, listing each variant with its own evidence span.
- Leave a field empty rather than guessing. Unknown is a valid answer.

Output JSON only:
`{ "parts": [ { "base_mpn": str, "type": str, "value": str, "variant": str, "span": str } ] }`

## Examples

Input: "Hirose DF13-6P-1.25DSA (cut tape: DF13-6P-1.25DSA(50))"
Output: `base_mpn` `DF13-6P-1.25DSA`, type `mpn`; the cut-tape line is the same base with `variant` `(50)`, each carrying its source span.

Input: "NSN 5935-01-573-2002, CAGE 0ABC1"
Output: two records, types `nsn` and `cage`, values copied verbatim from the text.
