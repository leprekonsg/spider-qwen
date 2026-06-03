"""Run metrics (spec section 13) + the T-7.3 cost router dashboard."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION


class RouteDecision(BaseModel):
    """One cost-router decision: which model tier a logical step is routed to.

    ``escalated`` is True when the high_risk_procurement tag bumped a step above
    its base tier (e.g. a routine ``decision`` from flash to max).
    """

    task: str
    tier: str  # "flash" | "max"
    role: str  # the Policy model role backing the tier
    model: str
    escalated: bool = False


class ModelCost(BaseModel):
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0


class CostReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    total_usd: float = 0.0
    tokens_total: int = 0
    tinyfish_calls: int = 0
    usd_saved_vs_all_max: float = 0.0
    by_model: list[ModelCost] = Field(default_factory=list)
    routing: list[RouteDecision] = Field(default_factory=list)


def _cost(input_tokens: int, output_tokens: int, price: dict[str, float]) -> float:
    """USD for a token count given a {input, output} per-1K-token price."""
    return input_tokens / 1000 * price.get("input", 0.0) + output_tokens / 1000 * price.get("output", 0.0)


class CostMeter:
    """Accumulate per-model LLM token usage and render a CostReport.

    Offline runs call no model, so the meter stays empty (zero cost); the report
    still carries the TinyFish call count and the routing plan.
    """

    def __init__(self) -> None:
        self._by_model: dict[str, list[int]] = {}  # model -> [calls, input, output]

    def record(self, model: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        acc = self._by_model.setdefault(model, [0, 0, 0])
        acc[0] += 1
        acc[1] += input_tokens
        acc[2] += output_tokens

    def report(
        self,
        pricing: dict[str, dict[str, float]],
        *,
        max_model: str,
        tinyfish_calls: int = 0,
        routing: list[RouteDecision] | tuple[RouteDecision, ...] = (),
    ) -> CostReport:
        max_price = pricing.get(max_model, {})
        total = all_max = 0.0
        tokens = 0
        by_model: list[ModelCost] = []
        for model, (calls, in_tok, out_tok) in sorted(self._by_model.items()):
            usd = _cost(in_tok, out_tok, pricing.get(model, {}))
            total += usd
            all_max += _cost(in_tok, out_tok, max_price)
            tokens += in_tok + out_tok
            by_model.append(
                ModelCost(model=model, calls=calls, input_tokens=in_tok, output_tokens=out_tok, usd=round(usd, 6))
            )
        return CostReport(
            total_usd=round(total, 6),
            tokens_total=tokens,
            tinyfish_calls=tinyfish_calls,
            usd_saved_vs_all_max=round(max(0.0, all_max - total), 6),
            by_model=by_model,
            routing=list(routing),
        )


class Metrics(BaseModel):
    search_calls_total: int = 0
    fetch_urls_total: int = 0
    validated_candidates_total: int = 0
    rfq_drafts_total: int = 0
    rfq_incomplete_total: int = 0
    held_for_review: int = 0
    quote_channel_found: int = 0
    candidates_considered: int = 0
    contact_precision_estimate: float = 0.0
    avg_runtime_seconds: float = 0.0
    budget_exhausted: bool = False
    cost: CostReport | None = None  # T-7.3 cost router dashboard

    @property
    def quote_channel_found_rate(self) -> float:
        if self.candidates_considered == 0:
            return 0.0
        return round(self.quote_channel_found / self.candidates_considered, 3)
