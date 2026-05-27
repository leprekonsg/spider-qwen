"""Confidence decay and staleness for semantic facts.

Simple half-life decay on age since last verification. A fact past the stale
age threshold is flagged `stale` (it is not deleted; revalidation can refresh).
"""

from __future__ import annotations

from datetime import datetime, timezone

from .semantic import SemanticFact

DEFAULT_HALF_LIFE_DAYS = 90.0
DEFAULT_STALE_DAYS = 180.0


def _age_days(iso_ts: str) -> float:
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400.0)


def apply_decay(fact: SemanticFact, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """Return the age-decayed confidence (0.5 ** (age / half_life))."""
    age = _age_days(fact.last_verified_at)
    return round(fact.confidence * (0.5 ** (age / half_life_days)), 4)


def is_stale(fact: SemanticFact, stale_days: float = DEFAULT_STALE_DAYS) -> bool:
    return _age_days(fact.last_verified_at) >= stale_days
