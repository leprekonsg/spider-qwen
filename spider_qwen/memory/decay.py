"""Confidence decay and staleness for semantic facts (MemoryBank / Ebbinghaus).

Forgetting follows ``strength(t) = strength0 * exp(-t / S)``. The stability ``S``
is not constant: it grows on re-access (each corroborating observation makes the
fact more durable, the spaced-repetition effect) and is halved per contradiction
(disputed facts decay faster). At zero reinforcements and zero contradictions
``S = half_life / ln 2``, so ``exp(-t/S)`` exactly reproduces the simple
``0.5 ** (t / half_life)`` half-life curve. A fact past the stale-age threshold
is flagged ``stale`` (never deleted; revalidation can refresh it).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .semantic import SemanticFact

DEFAULT_HALF_LIFE_DAYS = 90.0
DEFAULT_STALE_DAYS = 180.0
# Each re-access grows stability by this fraction of the base stability.
REINFORCE_GROWTH = 0.5


def _age_days(iso_ts: str) -> float:
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400.0)


def memory_stability_days(fact: SemanticFact, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """MemoryBank stability ``S`` (days) for the fact.

    Base ``S = half_life / ln 2`` so the unreinforced curve matches a plain
    half-life. Re-access (``reinforcement_count``) grows ``S``; each contradiction
    (one ``disputed_alternative``) halves it.
    """
    base = half_life_days / math.log(2.0)
    reinforced = base * (1.0 + REINFORCE_GROWTH * max(0, fact.reinforcement_count))
    disputes = len(fact.disputed_alternatives)
    return reinforced / (2.0 ** disputes)


def apply_decay(fact: SemanticFact, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """Return the age-decayed confidence under MemoryBank forgetting."""
    age = _age_days(fact.last_verified_at)
    stability = memory_stability_days(fact, half_life_days)
    return round(fact.confidence * math.exp(-age / stability), 4)


def is_stale(fact: SemanticFact, stale_days: float = DEFAULT_STALE_DAYS) -> bool:
    return _age_days(fact.last_verified_at) >= stale_days
