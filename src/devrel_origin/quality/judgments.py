"""The judgment port.

Stages call a `Judge`; only `TypeSafeJudge` knows the SDK exists. `build_judge`
never raises: without the extra or the key it returns `NullJudge`, whose
verdicts say `available=False` and are never treated as a pass.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from devrel_origin.quality.questions import PATTERN_NONE

logger = logging.getLogger(__name__)

UNAVAILABLE = "unavailable"

# The model label judgment spend is recorded under in the cost sink. TypeSafe's
# System One backend has one model, `jev`; unlike `core/llm.py` models, it is
# never in `MODEL_COSTS`, so `devrel cost` reports it as unpriced, not $0.00.
JUDGMENT_MODEL = "typesafe:jev"

CostSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]


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
    probabilities: dict[str, float] | None = None


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


class _CostSinkJudge:
    """Wraps a `Judge`, emitting each public call's usage to a cost sink.

    `last_usage` is the wrapped judge's most recent PUBLIC call's tokens,
    reset per call, so reading it after every method call never double
    counts. `NullJudge` carries no `last_usage`, so nothing is emitted for
    it. A sink failure is caught and logged: judgment spend reaching the
    cost database must never be the reason a judgment fails.
    """

    def __init__(self, judge: Judge, sink: CostSink) -> None:
        self._judge = judge
        self._sink = sink

    @property
    def available(self) -> bool:
        return self._judge.available

    @property
    def backend(self) -> str:
        return self._judge.backend

    async def _emit(self) -> None:
        usage = getattr(self._judge, "last_usage", None)
        if not usage:
            return
        if not any(usage.get(key, 0) for key in ("input_tokens", "output_tokens")):
            return
        try:
            await self._sink("quality", JUDGMENT_MODEL, usage)
        except Exception as exc:
            logger.warning("judgment_cost_sink_failed", extra={"error": str(exc)})

    async def select_claims(self, *, sentences: list[str]) -> list[float]:
        result = await self._judge.select_claims(sentences=sentences)
        await self._emit()
        return result

    async def verify_claim(self, *, claim: str, evidence: str) -> ClaimVerdict:
        result = await self._judge.verify_claim(claim=claim, evidence=evidence)
        await self._emit()
        return result

    async def judge_patterns(self, *, units: list[str], voice: str) -> list[PatternVerdict]:
        result = await self._judge.judge_patterns(units=units, voice=voice)
        await self._emit()
        return result


def with_cost_sink(judge: Judge, sink: CostSink) -> Judge:
    """Wrap `judge` so every public call's usage reaches `sink`.

    `sink` is `async (agent, model, usage) -> None`, the shape built by
    `project.cost_sink.make_sqlite_sink`. Nothing is emitted for a judge with
    no `last_usage` (`NullJudge`) or whose `last_usage` carries no tokens.
    """
    return _CostSinkJudge(judge, sink)
