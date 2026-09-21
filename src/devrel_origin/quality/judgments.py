"""The judgment port.

Stages call a `Judge`; only `TypeSafeJudge` knows the SDK exists. `build_judge`
never raises: without the extra or the key it returns `NullJudge`, whose
verdicts say `available=False` and are never treated as a pass.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

from devrel_origin.quality.questions import PATTERN_NONE

logger = logging.getLogger(__name__)

UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ClaimVerdict:
    relation: str
    confidence: float
    available: bool
    backend: str


@dataclass(frozen=True)
class PatternVerdict:
    unit_index: int
    pattern: str
    confidence: float
    available: bool
    backend: str


class Judge(Protocol):
    available: bool
    backend: str

    async def select_claims(self, *, sentences: list[str]) -> list[float]: ...

    async def verify_claim(self, *, claim: str, evidence: str) -> ClaimVerdict: ...

    async def judge_patterns(self, *, units: list[str], voice: str) -> list[PatternVerdict]: ...


class NullJudge:
    """No judgment available. Returns `unavailable`, never a verdict."""

    available = False
    backend = "none"

    async def select_claims(self, *, sentences: list[str]) -> list[float]:
        return [0.0 for _ in sentences]

    async def verify_claim(self, *, claim: str, evidence: str) -> ClaimVerdict:
        return ClaimVerdict(
            relation=UNAVAILABLE,
            confidence=0.0,
            available=False,
            backend=self.backend,
        )

    async def judge_patterns(self, *, units: list[str], voice: str) -> list[PatternVerdict]:
        return [
            PatternVerdict(
                unit_index=i,
                pattern=PATTERN_NONE,
                confidence=0.0,
                available=False,
                backend=self.backend,
            )
            for i, _ in enumerate(units)
        ]


def _sdk_available() -> bool:
    try:
        import typesafe_sdk  # noqa: F401
    except Exception:
        return False
    return True


def build_judge(api_key: str | None = None) -> Judge:
    """Pick a backend. Never raises: any failure degrades to `NullJudge`."""
    try:
        key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        if not key:
            logger.debug("judge_unavailable", extra={"reason": "no_key"})
            return NullJudge()
        if not _sdk_available():
            logger.debug("judge_unavailable", extra={"reason": "extra_not_installed"})
            return NullJudge()
        from devrel_origin.quality.judgments_typesafe import TypeSafeJudge

        return TypeSafeJudge(api_key=key)
    except Exception as exc:  # degradation is the contract, so nothing escapes
        logger.warning("judge_build_failed", extra={"error": str(exc)})
        return NullJudge()
