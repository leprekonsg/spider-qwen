"""T-R.1: strategy-typed reasoning trajectories + bundle models + frozen budget.

A *trajectory* is one bounded sourcing strategy that runs the same safe pipeline
(query expansion -> search -> fetch -> extract -> evidence bundle -> candidate
score). Strategies are vertical-specific (service vs electronics). A *bundle* is
the evidence/candidate result of running one trajectory, carrying the normalized
sub-scores the Process Reward Model consumes. The budget is a frozen dataclass so
the agent can never self-raise its own caps.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef


class TrajectoryStrategy(str, Enum):
    # Service procurement
    OFFICIAL_SITE_FIRST = "official_site_first"
    QUOTE_CHANNEL_FIRST = "quote_channel_first"
    DIRECTORY_CONTACT_FIRST = "directory_contact_first"
    SEA_LOCAL_PHRASING_FIRST = "sea_local_phrasing_first"
    CATEGORY_PRICING_FIRST = "category_pricing_first"
    # Electronics sourcing
    AUTHORIZED_DISTRIBUTOR_FIRST = "authorized_distributor_first"
    FFF_SUBSTITUTE_FIRST = "fff_substitute_first"
    LIFECYCLE_PCN_FIRST = "lifecycle_pcn_first"
    BROKER_LONG_TAIL_FIRST = "broker_long_tail_first"
    ARCHIVED_DATASHEET_FIRST = "archived_datasheet_first"


SERVICE_STRATEGIES: tuple[TrajectoryStrategy, ...] = (
    TrajectoryStrategy.OFFICIAL_SITE_FIRST,
    TrajectoryStrategy.QUOTE_CHANNEL_FIRST,
    TrajectoryStrategy.DIRECTORY_CONTACT_FIRST,
    TrajectoryStrategy.SEA_LOCAL_PHRASING_FIRST,
    TrajectoryStrategy.CATEGORY_PRICING_FIRST,
)
ELECTRONICS_STRATEGIES: tuple[TrajectoryStrategy, ...] = (
    TrajectoryStrategy.AUTHORIZED_DISTRIBUTOR_FIRST,
    TrajectoryStrategy.FFF_SUBSTITUTE_FIRST,
    TrajectoryStrategy.LIFECYCLE_PCN_FIRST,
    TrajectoryStrategy.BROKER_LONG_TAIL_FIRST,
    TrajectoryStrategy.ARCHIVED_DATASHEET_FIRST,
)

# Service modes use service strategies; everything part/electronics-shaped uses
# the electronics strategies (product_exact_price doubles as substitute discovery).
_SERVICE_MODES = {"service_quote_required", "contact_enrichment_only"}


def strategies_for(mode: str) -> tuple[TrajectoryStrategy, ...]:
    return SERVICE_STRATEGIES if mode in _SERVICE_MODES else ELECTRONICS_STRATEGIES


@dataclass(frozen=True)
class ReasoningBudget:
    """Hard caps on the trajectory layer. Frozen: the agent cannot raise them."""

    max_trajectories: int = 3
    max_refinement_rounds: int = 2
    max_search_calls_per_trajectory: int = 2
    max_fetch_urls_per_trajectory: int = 6

    @property
    def total_search_cap(self) -> int:
        """Discovery searches per refinement round (width x per-trajectory)."""
        return self.max_trajectories * self.max_search_calls_per_trajectory

    @property
    def total_fetch_cap(self) -> int:
        """Discovery fetches per refinement round (width x per-trajectory)."""
        return self.max_trajectories * self.max_fetch_urls_per_trajectory

    @property
    def search_ceiling(self) -> int:
        """Absolute search ceiling across all trajectories and rounds."""
        return self.total_search_cap * self.max_refinement_rounds

    @property
    def fetch_ceiling(self) -> int:
        """Absolute fetch ceiling across all trajectories and rounds."""
        return self.total_fetch_cap * self.max_refinement_rounds


class ReasoningTrajectory(BaseModel):
    schema_version: str = SCHEMA_VERSION
    trajectory_id: str = Field(default_factory=lambda: f"traj_{uuid4().hex[:12]}")
    strategy: TrajectoryStrategy
    mode: str
    seed_query: str
    queries: list[str] = Field(default_factory=list)
    round: int = 1


class BundleMetrics(BaseModel):
    """Normalized [0,1] sub-scores. Service uses the first block, electronics the
    second; unused fields stay 0 and contribute nothing under their weight."""

    # service
    service_match: float = 0.0
    quote_channel: float = 0.0
    geo: float = 0.0
    contact_reliability: float = 0.0
    checklist: float = 0.0
    evidence_diversity: float = 0.0
    # electronics
    fff_similarity: float = 0.0
    lifecycle_safety: float = 0.0
    authorized_source: float = 0.0
    stock: float = 0.0
    datasheet_evidence: float = 0.0
    risk: float = 0.0


class TrajectoryBundle(BaseModel):
    schema_version: str = SCHEMA_VERSION
    trajectory: ReasoningTrajectory
    metrics: BundleMetrics = Field(default_factory=BundleMetrics)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    candidate_count: int = 0
    disputed_count: int = 0
    searches_used: int = 0
    fetches_used: int = 0
    rounds_used: int = 1
    conflict_penalty: float = 0.0
    budget_penalty: float = 0.0
    counterfeit_penalty: float = 0.0
    reward: float | None = None
    reward_breakdown: dict[str, float] = Field(default_factory=dict)
