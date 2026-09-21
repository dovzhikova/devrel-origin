"""Test doubles for the judgment port. Used by the grounding and slop tests."""

from devrel_origin.quality.judgments import UNAVAILABLE, ClaimVerdict, PatternVerdict
from devrel_origin.quality.questions import PATTERN_NONE


class FakeJudge:
    """Scripted judge. `relations` and `patterns` are consumed in order."""

    available = True
    backend = "fake"

    def __init__(self, relations=None, patterns=None, claim_probs=None, usage=None):
        self.relations = list(relations or [])
        self.patterns = dict(patterns or {})
        self.claim_probs = claim_probs  # None means "every sentence is a claim"
        self.claims_seen: list[tuple[str, str]] = []
        self.units_seen: list[str] = []
        # `usage`, when given, is reported as `last_usage` after every public
        # call, like TypeSafeJudge's per-call reset. Left unset (no
        # `last_usage` attribute at all) when `usage` is None, so a plain
        # FakeJudge behaves exactly as before for tests that don't care.
        self._usage = dict(usage) if usage is not None else None
        if self._usage is not None:
            self.last_usage: dict[str, int] | None = None

    def _report_usage(self) -> None:
        if self._usage is not None:
            self.last_usage = dict(self._usage)

    async def select_claims(self, *, sentences: list[str]) -> list[float]:
        if self.claim_probs is None:
            result = [1.0 for _ in sentences]
        else:
            result = list(self.claim_probs)
        self._report_usage()
        return result

    async def verify_claim(self, *, claim: str, evidence: str) -> ClaimVerdict:
        self.claims_seen.append((claim, evidence))
        relation, confidence = self.relations.pop(0) if self.relations else ("says_nothing", 0.9)
        # A scripted relation of UNAVAILABLE simulates a per-call backend
        # failure (transient), distinct from the class-level `available`.
        available = relation != UNAVAILABLE
        self._report_usage()
        return ClaimVerdict(
            relation=relation, confidence=confidence, available=available, backend=self.backend
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
        self._report_usage()
        return out
