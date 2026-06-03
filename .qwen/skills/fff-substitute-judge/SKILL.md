---
name: fff-substitute-judge
description: Judge Form-Fit-Function equivalence between two parts as a drop-in replacement, scoring package, electrical, environmental, and qualification criteria with per-criterion evidence.
keywords: [fff, form fit function, substitute, replacement, drop-in, equivalent, cross reference, alternate, interchangeable, second source, datasheet]
allowedTools: [fetch]
paths: [spider_qwen/modes, spider_qwen/verification]
---

## Instructions

Compare a candidate substitute against the original and return a verdict, never a bare yes or no. Ground every criterion in datasheet evidence; if a datasheet is missing, the verdict cannot be `drop_in`.

- Verdict is one of `drop_in`, `electrical_equiv`, `requires_redesign`, `not_equivalent`.
- Score each axis with its evidence span: package and footprint (form), pinout and mounting (fit), electrical parameters and function, environmental and temperature grade, and qualification (AEC-Q, RoHS, and similar).
- `drop_in` requires datasheet evidence, an FFF match on all axes, and an active lifecycle; otherwise downgrade to `requires_redesign` or `not_equivalent`.
- A plausible match with no datasheet evidence is `requires_redesign`, not a recommendation.

## Examples

Input: the original versus a pin-compatible candidate with a matching datasheet and an active lifecycle.
Output: verdict `drop_in` with per-axis evidence spans.

Input: a same-function candidate with no datasheet.
Output: verdict `requires_redesign`; state that engineering review is required.
