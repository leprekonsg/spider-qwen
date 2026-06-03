---
name: counterfeit-risk-check
description: Cross-check a broker or distributor source against ERAI and GIDEP advisories and its authorization tier to render a counterfeit risk badge with the required incoming-inspection standard.
keywords: [counterfeit, risk, broker, erai, gidep, authorized, franchised, distributor, as6081, as6171, far, inspection, suspect, gray market]
allowedTools: [search, fetch]
paths: [spider_qwen/serendipity, spider_qwen/governance]
---

## Instructions

Assess counterfeit risk for a sourcing option and return a badge with its justification. Do not clear a source you cannot place; unknown sources are amber, not green.

- Badge: `red` if the source appears on an ERAI or GIDEP advisory; `green` if it is the manufacturer or a franchised / authorized distributor; `amber` for any unauthorized or unknown broker.
- For amber or red, require AS6081 or AS6171 incoming inspection and cite FAR 52.246-26 (contractor counterfeit-part detection and avoidance).
- Normalize names (case and spacing) before matching the advisory fixtures.
- Carry the evidence span for every advisory hit and every authorization claim.

## Examples

Input: a broker present on the ERAI fixture.
Output: badge `red`, the advisory span, AS6171 inspection required, FAR 52.246-26 cited.

Input: a franchised distributor.
Output: badge `green` with the authorization evidence.
