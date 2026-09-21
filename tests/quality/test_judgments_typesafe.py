import pytest

from devrel_origin.quality.judgments_typesafe import TypeSafeJudge


class _Answer:
    def __init__(self, choice, confidence):
        self.choice = choice
        self.confidence = confidence


class _Resp:
    def __init__(self, choices, usage=(657, 79)):
        self.choices = choices
        self.model = "jev-1.13.0"

        class _U:
            def __init__(self, input_tokens, output_tokens):
                self.input_tokens = input_tokens
                self.output_tokens = output_tokens

        self.usage = _U(*usage)


class _StubClient:
    """Stands in for AsyncTypeSafeClient. No network, no SDK import."""

    def __init__(self, answers_by_call, usage_by_call=None):
        self.answers_by_call = list(answers_by_call)
        self.usage_by_call = list(usage_by_call) if usage_by_call is not None else None
        self.calls = []

    async def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        choices = self.answers_by_call.pop(0)
        if self.usage_by_call is not None:
            return _Resp(choices, usage=self.usage_by_call.pop(0))
        return _Resp(choices)


@pytest.mark.asyncio
async def test_verify_claim_maps_the_choice_and_confidence():
    client = _StubClient([{"relation": _Answer("supports", 0.65)}])
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
async def test_all_units_ride_in_one_request():
    answers = {f"unit_{i}": _Answer("none", 0.9) for i in range(3)}
    answers["unit_1"] = _Answer("colon_reveal", 0.81)
    client = _StubClient([answers])
    judge = TypeSafeJudge(api_key="sk-test", client=client)

    verdicts = await judge.judge_patterns(units=["a", "b", "c"], voice="plain")

    assert len(client.calls) == 1, "one request per draft, not one per unit"
    assert [v.pattern for v in verdicts] == ["none", "colon_reveal", "none"]
    assert verdicts[1].confidence == 0.81
    assert all(v.available for v in verdicts)


@pytest.mark.asyncio
async def test_units_are_chunked_when_over_the_cap():
    client = _StubClient(
        [
            {f"unit_{i}": _Answer("none", 0.9) for i in range(2)},
            {f"unit_{i}": _Answer("none", 0.9) for i in range(2, 3)},
        ]
    )
    judge = TypeSafeJudge(api_key="sk-test", client=client, max_questions_per_request=2)

    verdicts = await judge.judge_patterns(units=["a", "b", "c"], voice="")

    assert len(client.calls) == 2
    assert [v.unit_index for v in verdicts] == [0, 1, 2]


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


@pytest.mark.asyncio
async def test_last_usage_sums_tokens_across_chunked_requests_then_resets_on_next_call():
    client = _StubClient(
        [
            {f"unit_{i}": _Answer("none", 0.9) for i in range(2)},
            {f"unit_{i}": _Answer("none", 0.9) for i in range(2, 3)},
            {"relation": _Answer("supports", 0.5)},
        ],
        usage_by_call=[(100, 10), (50, 5), (7, 3)],
    )
    judge = TypeSafeJudge(api_key="sk-test", client=client, max_questions_per_request=2)

    await judge.judge_patterns(units=["a", "b", "c"], voice="")
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
            return _Resp({"relation": _Answer("supports", 0.9)})

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
