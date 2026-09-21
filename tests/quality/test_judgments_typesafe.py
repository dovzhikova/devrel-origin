import pytest

from devrel_origin.quality.judgments_typesafe import TypeSafeJudge
from devrel_origin.quality.questions import PATTERN_NONE, PATTERN_NOULS


class _Answer:
    def __init__(self, choice, confidence):
        self.choice = choice
        self.confidence = confidence


class _Noul:
    def __init__(self, noul):
        self.noul = noul


class _Resp:
    def __init__(self, choices=None, nouls=None, usage=(657, 79)):
        self.choices = choices or {}
        self.nouls = nouls or {}
        self.model = "jev-1.13.0"

        class _U:
            def __init__(self, input_tokens, output_tokens):
                self.input_tokens = input_tokens
                self.output_tokens = output_tokens

        self.usage = _U(*usage)


class _StubClient:
    """Stands in for AsyncTypeSafeClient. No network, no SDK import."""

    def __init__(self, responses, usage_by_call=None):
        self.responses = list(responses)
        self.usage_by_call = list(usage_by_call) if usage_by_call is not None else None
        self.calls = []

    async def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        payload = self.responses.pop(0)
        usage = self.usage_by_call.pop(0) if self.usage_by_call is not None else (657, 79)
        return _Resp(usage=usage, **payload)


def _nouls_for_unit(unit_key, high_pattern, high_prob=0.9, low_prob=0.05):
    """Every pattern's Noul for one unit, one pattern high, the rest low."""
    return {
        f"{unit_key}__{pattern}": _Noul(high_prob if pattern == high_pattern else low_prob)
        for pattern in PATTERN_NOULS
    }


@pytest.mark.asyncio
async def test_verify_claim_maps_the_choice_and_confidence():
    client = _StubClient([{"choices": {"relation": _Answer("supports", 0.65)}}])
    judge = TypeSafeJudge(api_key="sk-test", client=client)

    verdict = await judge.verify_claim(claim="It adds a queue.", evidence="feat: add queue")

    assert verdict.relation == "supports"
    assert verdict.confidence == 0.65
    assert verdict.available is True
    assert verdict.backend == "typesafe"
    state, questions = client.calls[0]
    assert state["claim"] == "It adds a queue."
    assert state["evidence"] == "feat: add queue"
    assert list(questions) == ["relation"]


@pytest.mark.asyncio
async def test_argmax_pattern_and_max_confidence():
    client = _StubClient([{"nouls": _nouls_for_unit("unit_0", "colon_reveal", high_prob=0.81)}])
    judge = TypeSafeJudge(api_key="sk-test", client=client)

    verdicts = await judge.judge_patterns(units=["a"], voice="plain")

    assert len(verdicts) == 1
    assert verdicts[0].pattern == "colon_reveal"
    assert verdicts[0].confidence == 0.81
    assert verdicts[0].available is True
    assert verdicts[0].backend == "typesafe"


@pytest.mark.asyncio
async def test_probabilities_carries_every_pattern():
    client = _StubClient([{"nouls": _nouls_for_unit("unit_0", "throat_clearing")}])
    judge = TypeSafeJudge(api_key="sk-test", client=client)

    verdicts = await judge.judge_patterns(units=["a"], voice="")

    assert set(verdicts[0].probabilities) == set(PATTERN_NOULS)
    assert verdicts[0].probabilities["throat_clearing"] == 0.9
    assert verdicts[0].probabilities["colon_reveal"] == 0.05


@pytest.mark.asyncio
async def test_units_within_the_cap_share_one_request():
    nouls = {
        **_nouls_for_unit("unit_0", "binary_contrast"),
        **_nouls_for_unit("unit_1", "colon_reveal"),
    }
    client = _StubClient([{"nouls": nouls}])
    judge = TypeSafeJudge(api_key="sk-test", client=client)

    verdicts = await judge.judge_patterns(units=["a", "b"], voice="plain")

    assert len(client.calls) == 1, "two units' questions fit the default cap"
    assert [v.pattern for v in verdicts] == ["binary_contrast", "colon_reveal"]
    assert [v.unit_index for v in verdicts] == [0, 1]


@pytest.mark.asyncio
async def test_a_units_questions_never_span_two_requests_and_indices_stay_global():
    # Per-pattern Nouls means one unit alone (10 questions) already exceeds a
    # cap of 15: each unit must land in its own request, never split.
    client = _StubClient(
        [
            {"nouls": _nouls_for_unit("unit_0", "binary_contrast")},
            {"nouls": _nouls_for_unit("unit_1", "colon_reveal")},
            {"nouls": _nouls_for_unit("unit_2", "throat_clearing")},
        ]
    )
    judge = TypeSafeJudge(api_key="sk-test", client=client, max_questions_per_request=15)

    verdicts = await judge.judge_patterns(units=["a", "b", "c"], voice="")

    assert len(client.calls) == 3
    assert [v.unit_index for v in verdicts] == [0, 1, 2]
    assert [v.pattern for v in verdicts] == ["binary_contrast", "colon_reveal", "throat_clearing"]
    for state, questions in client.calls:
        unit_keys = [k for k in state if k != "voice"]
        assert len(unit_keys) == 1, "a unit's questions never span two requests"


@pytest.mark.asyncio
async def test_a_service_failure_degrades_to_unavailable_not_an_exception():
    class _Boom:
        async def system_one(self, state, questions, **kwargs):
            raise RuntimeError("503 from the service")

    judge = TypeSafeJudge(api_key="sk-test", client=_Boom())

    verdict = await judge.verify_claim(claim="c", evidence="e")
    assert verdict.available is False
    assert verdict.relation != "supports"

    verdicts = await judge.judge_patterns(units=["a", "b"], voice="")
    assert [v.available for v in verdicts] == [False, False]
    assert all(v.pattern == PATTERN_NONE for v in verdicts)
    assert all(v.probabilities is None for v in verdicts)


@pytest.mark.asyncio
async def test_a_failed_request_degrades_only_its_own_units():
    class _PartialFailClient:
        def __init__(self):
            self.calls = 0

        async def system_one(self, state, questions, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _Resp(nouls=_nouls_for_unit("unit_0", "binary_contrast"))
            raise RuntimeError("503 from the service")

    judge = TypeSafeJudge(
        api_key="sk-test", client=_PartialFailClient(), max_questions_per_request=15
    )

    verdicts = await judge.judge_patterns(units=["a", "b"], voice="")

    assert verdicts[0].available is True
    assert verdicts[0].pattern == "binary_contrast"
    assert verdicts[1].available is False
    assert verdicts[1].pattern == PATTERN_NONE
    assert verdicts[1].probabilities is None


@pytest.mark.asyncio
async def test_last_usage_sums_tokens_across_chunked_requests_then_resets_on_next_call():
    client = _StubClient(
        [
            {"nouls": _nouls_for_unit("unit_0", "binary_contrast")},
            {"nouls": _nouls_for_unit("unit_1", "colon_reveal")},
            {"choices": {"relation": _Answer("supports", 0.5)}},
        ],
        usage_by_call=[(100, 10), (50, 5), (7, 3)],
    )
    judge = TypeSafeJudge(api_key="sk-test", client=client, max_questions_per_request=15)

    await judge.judge_patterns(units=["a", "b"], voice="")
    assert judge.last_usage == {"input_tokens": 150, "output_tokens": 15}

    # A subsequent public call resets the accumulator rather than adding to it.
    await judge.verify_claim(claim="c", evidence="e")
    assert judge.last_usage == {"input_tokens": 7, "output_tokens": 3}


@pytest.mark.asyncio
async def test_verify_claim_uses_the_owned_client_when_none_is_injected(monkeypatch):
    class _FakeOwnedClient:
        instances: list["_FakeOwnedClient"] = []

        def __init__(self, *, api_key):
            self.api_key = api_key
            self.entered = False
            self.exited = False
            self.system_one_called = False
            _FakeOwnedClient.instances.append(self)

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.exited = True
            return False

        async def system_one(self, state, questions, **kwargs):
            self.system_one_called = True
            return _Resp(choices={"relation": _Answer("supports", 0.9)})

    monkeypatch.setattr("typesafe_sdk.AsyncTypeSafeClient", _FakeOwnedClient)

    judge = TypeSafeJudge(api_key="sk-test")  # no client injected: the owned path

    verdict = await judge.verify_claim(claim="c", evidence="e")

    assert len(_FakeOwnedClient.instances) == 1
    fake = _FakeOwnedClient.instances[0]
    assert fake.api_key == "sk-test"
    assert fake.entered is True
    assert fake.exited is True
    assert fake.system_one_called is True
    assert verdict.relation == "supports"
    assert verdict.available is True
