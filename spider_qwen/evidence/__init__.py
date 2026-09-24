"""Evidence ledger: every downstream output references a ledger entry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import EvidenceItem, EvidenceRef, sha256_hex
from .ledger import EvidenceLedger
from .bundles import EvidenceBundle

if TYPE_CHECKING:
    from .verifier import EvidenceVerificationResult, verify_ledger

__all__ = [
    "EvidenceItem",
    "EvidenceRef",
    "EvidenceLedger",
    "EvidenceBundle",
    "EvidenceVerificationResult",
    "verify_ledger",
    "sha256_hex",
]


def __getattr__(name: str) -> Any:
    # verifier imports verification.atomic, which imports evidence.models;
    # loading it eagerly here makes `import spider_qwen.verification.atomic`
    # fail with a circular import.
    if name in ("EvidenceVerificationResult", "verify_ledger"):
        from . import verifier

        return getattr(verifier, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
