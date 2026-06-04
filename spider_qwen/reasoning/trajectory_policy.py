"""T-R.1: activation gate for the reasoning trajectory layer.

The expensive multi-trajectory path only activates for high-value modes when the
classifier is not already confident (complex / low-confidence / high-value
queries). Simple one-shot contact extraction never activates it. The config
carries the frozen budget so callers cannot widen the caps ad hoc.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .trajectory import ReasoningBudget

# High-value verticals worth exploring with multiple trajectories.
_DEFAULT_ACTIVATE_MODES = frozenset(
    {"service_quote_required", "product_exact_price", "revalidation", "electronics_substitution"}
)


@dataclass(frozen=True)
class ReasoningConfig:
    enabled: bool = True
    classifier_confidence_below: float = 0.75
    activate_modes: frozenset[str] = _DEFAULT_ACTIVATE_MODES
    budget: ReasoningBudget = field(default_factory=ReasoningBudget)


def should_activate(mode: str, classifier_confidence: float, config: ReasoningConfig | None = None) -> bool:
    config = config or ReasoningConfig()
    return (
        config.enabled
        and mode in config.activate_modes
        and classifier_confidence < config.classifier_confidence_below
    )
