from dataclasses import FrozenInstanceError

import pytest

from devrel_origin.quality.judgments import (
    ClaimVerdict,
    NullJudge,
    PatternVerdict,
    build_judge,
    with_cost_sink,
)
from devrel_origin.quality.questions import PATTERN_NONE


@pytest.mark.asyncio
async def test_null_judge_reports_unavailable_and_never_a_pass():
    judge = NullJudge()
    assert judge.available is False
    assert judge.backend == "none"

    verdict = await judge.verify_claim(claim="anything", evidence="anything")
    assert verdict.available is False
    # The degradation contract: absent judgment must never read as support.
    assert verdict.relation != "supports"
    assert verdict.confidence == 0.0


@pytest.mark.asyncio
async def test_null_judge_returns_one_unavailable_verdict_per_unit():
    verdicts = await NullJudge().judge_patterns(units=["a", "b", "c"], voice="")
    assert [v.unit_index for v in verdicts] == [0, 1, 2]
    assert all(v.available is False for v in verdicts)
    assert all(v.pattern == PATTERN_NONE for v in verdicts)


@pytest.mark.asyncio
async def test_null_judge_selects_no_claims():
    # Zero, not one: an unjudged sentence must not enter the grounding loop.
    assert await NullJudge().select_claims(sentences=["a.", "b."]) == [0.0, 0.0]


def test_build_judge_falls_back_without_a_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert build_judge().backend == "none"


def test_build_judge_falls_back_when_the_extra_is_missing(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test")
    monkeypatch.setattr("devrel_origin.quality.judgments._sdk_available", lambda: False)
    assert build_judge().backend == "none"


def test_build_judge_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("import exploded")

    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test")
    monkeypatch.setattr("devrel_origin.quality.judgments._sdk_available", boom)
    assert build_judge().backend == "none"


def test_build_judge_falls_back_when_the_backend_constructor_raises(monkeypatch):
    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("client construction exploded")

    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test")
    monkeypatch.setattr("devrel_origin.quality.judgments._sdk_available", lambda: True)
    monkeypatch.setattr("devrel_origin.quality.judgments_typesafe.TypeSafeJudge", _Boom)
    assert build_judge().backend == "none"


def test_claim_verdict_is_frozen():
    with pytest.raises(FrozenInstanceError):
        ClaimVerdict(
            relation="supports", confidence=1.0, available=True, backend="x"
        ).relation = "contradicts"


class _FakeUsageJudge:
    """A judge whose `last_usage` changes per call, like TypeSafeJudge's."""

    available = True
    backend = "typesafe"

    def __init__(self, usages):
        self._usages = list(usages)
        self.last_usage = None

    async def select_claims(self, *, sentences: list[str]) -> list[float]:
        self.last_usage = self._usages.pop(0)
        return [0.0 for _ in sentences]

    async def verify_claim(self, *, claim: str, evidence: str) -> ClaimVerdict:
        self.last_usage = self._usages.pop(0)
        return ClaimVerdict(
            relation="supports", confidence=1.0, available=True, backend=self.backend
        )

    async def judge_patterns(self, *, units: list[str], voice: str) -> list[PatternVerdict]:
        self.last_usage = self._usages.pop(0)
        return [
            PatternVerdict(
                unit_index=i,
                pattern=PATTERN_NONE,
                confidence=0.0,
                available=True,
                backend=self.backend,
            )
            for i, _ in enumerate(units)
        ]


@pytest.mark.asyncio
async def test_with_cost_sink_emits_once_per_call_with_that_calls_usage():
    calls = []

    async def sink(agent, model, usage):
        calls.append((agent, model, usage))

    judge = _FakeUsageJudge(
        [
            {"input_tokens": 100, "output_tokens": 10},
            {"input_tokens": 200, "output_tokens": 20},
            {"input_tokens": 300, "output_tokens": 30},
        ]
    )
    wrapped = with_cost_sink(judge, sink)

    await wrapped.select_claims(sentences=["a."])
    await wrapped.verify_claim(claim="x", evidence="y")
    await wrapped.judge_patterns(units=["u"], voice="v")

    assert len(calls) == 3
    assert calls[0] == ("quality", "typesafe:jev", {"input_tokens": 100, "output_tokens": 10})
    assert calls[1] == ("quality", "typesafe:jev", {"input_tokens": 200, "output_tokens": 20})
    assert calls[2] == ("quality", "typesafe:jev", {"input_tokens": 300, "output_tokens": 30})


@pytest.mark.asyncio
async def test_with_cost_sink_emits_nothing_for_null_judge():
    calls = []

    async def sink(agent, model, usage):
        calls.append((agent, model, usage))

    wrapped = with_cost_sink(NullJudge(), sink)

    await wrapped.verify_claim(claim="x", evidence="y")
    await wrapped.select_claims(sentences=["a."])
    await wrapped.judge_patterns(units=["u"], voice="v")

    assert calls == []


@pytest.mark.asyncio
async def test_with_cost_sink_survives_a_raising_sink():
    async def sink(agent, model, usage):
        raise RuntimeError("sink exploded")

    judge = _FakeUsageJudge([{"input_tokens": 5, "output_tokens": 1}])
    wrapped = with_cost_sink(judge, sink)

    verdict = await wrapped.verify_claim(claim="x", evidence="y")

    assert verdict.available is True
    assert verdict.relation == "supports"


def test_with_cost_sink_exposes_available_and_backend():
    async def sink(agent, model, usage):
        pass

    typesafe_wrapped = with_cost_sink(_FakeUsageJudge([]), sink)
    assert typesafe_wrapped.available is True
    assert typesafe_wrapped.backend == "typesafe"

    null_wrapped = with_cost_sink(NullJudge(), sink)
    assert null_wrapped.available is False
    assert null_wrapped.backend == "none"
