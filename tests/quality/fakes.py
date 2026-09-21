"""Test doubles for the judgment port. Used by the grounding and slop tests."""

from devrel_origin.quality.judgments import ClaimVerdict, PatternVerdict
from devrel_origin.quality.questions import PATTERN_NONE


class FakeJudge:
    """Scripted judge. `relations` and `patterns` are consumed in order."""

    available = True
    backend = "fake"

    def __init__(self, relations=None, patterns=None, claim_probs=None):
        self.relations = list(relations or [])
        self.patterns = dict(patterns or {})
        self.claim_probs = claim_probs  # None means "every sentence is a claim"
        self.claims_seen: list[tuple[str, str]] = []
        self.units_seen: list[str] = []

    async def select_claims(self, *, sentences: list[str]) -> list[float]:
        if self.claim_probs is None:
            return [1.0 for _ in sentences]
        return list(self.claim_probs)

    async def verify_claim(self, *, claim: str, evidence: str) -> ClaimVerdict:
        self.claims_seen.append((claim, evidence))
        relation, confidence = self.relations.pop(0) if self.relations else ("says_nothing", 0.9)
        return ClaimVerdict(
            relation=relation, confidence=confidence, available=True, backend=self.backend
        )

    async def judge_patterns(self, *, units: list[str], voice: str) -> list[PatternVerdict]:
        self.units_seen = list(units)
        out = []
        for i, _ in enumerate(units):
            pattern, confidence = self.patterns.get(i, (PATTERN_NONE, 0.95))
            out.append(
                PatternVerdict(
                    unit_index=i,
                    pattern=pattern,
                    confidence=confidence,
                    available=True,
                    backend=self.backend,
                )
            )
        return out
