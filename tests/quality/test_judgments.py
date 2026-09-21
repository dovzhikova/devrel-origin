from dataclasses import FrozenInstanceError

import pytest

from devrel_origin.quality.judgments import ClaimVerdict, NullJudge, build_judge
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
