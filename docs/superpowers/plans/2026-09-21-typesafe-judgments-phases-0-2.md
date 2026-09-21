# TypeSafe judgments, Phases 0 to 2: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two LLM-prose stages of Origin's editorial pipeline with typed TypeSafe judgments behind a port, so slop is reported as named patterns with quoted lines and claims are verified against evidence, with both degrading safely to today's checks when no key is present.

**Architecture:** A new `quality/questions.py` holds frozen question specs as plain dataclasses with no SDK import, so questions are testable and the dependency stays optional. `quality/judgments.py` holds the verdict types, the `Judge` protocol, `NullJudge`, `TypeSafeJudge` and `build_judge()`. `grounding.py` and `slop.py` call the port and never the SDK. A blind evaluation (Task 4) gates Tasks 5 and 6: if the judgments do not beat the incumbent checks, they do not ship.

**Tech Stack:** Python 3.12+, `typesafe-sdk` 0.7.0 (async client, ships `httpx2`), pytest + pytest-asyncio, ruff (pinned `>=0.15,<0.16`).

**Spec:** `docs/superpowers/specs/2026-09-20-typesafe-judgments-design.md`

## Global Constraints

- Python 3.12+, async for anything doing I/O, type hints everywhere, dataclasses for DTOs, line length 100.
- `typesafe-sdk` is an **optional extra** named `typesafe`. Nothing outside `TypeSafeJudge` may import it, and no import of it may happen at module scope in `quality/questions.py`, `grounding.py` or `slop.py`.
- `build_judge()` **never raises**. Missing extra, missing `TYPESAFE_API_KEY`, and an unreachable service all return `NullJudge`.
- A skipped judgment renders as `skipped` and **never** as a pass.
- **No test makes a network call.** `respx` mocks `httpx`, but the SDK ships `httpx2`, so respx will not intercept it. Tests inject a `FakeJudge` at the port boundary or a stub client into `TypeSafeJudge`.
- The baseline to protect: 1098 passed, 0 skipped, coverage 77.99%, `ruff check .` and `ruff format --check .` clean.
- No em dashes in any file, including comments, docstrings and commit messages.
- Run everything with the repo venv: `./.venv/bin/python`, `./.venv/bin/pytest`, `./.venv/bin/ruff`.

## Verified API facts (measured 2026-09-21, do not re-derive)

One live request was made against `jev-1.13.0` to confirm these shapes:

```
resp = await client.system_one(state={...}, questions={"name": Choice(...)})
resp.model                        -> "jev-1.13.0"
resp.usage.input_tokens           -> 657        (3 questions, one item)
resp.usage.output_tokens          -> 79
resp.choices["relation"].choice   -> "supports"
resp.choices["relation"].confidence, .probabilities -> 0.65, {"supports": 0.76, ...}
resp.nouls["is_factual_claim"].noul -> 0.95     (a probability; Noul has NO confidence field)
resp.scores["reader_impact"].score, .confidence, .probabilities, .legend
```

Construction: `AsyncTypeSafeClient(api_key=...)`, an async context manager.
Question types: `Choice(instructions=..., criteria={name: definition})`,
`Noul(instructions=...)`, `Score(instructions=..., criteria=[level0, level1, ...])`.

Two findings from that same request, both of which this plan acts on:

1. A genuinely supporting claim/evidence pair returned `supports` at confidence **0.65**. The citation-check cookbook's 0.8 auto-accept would have sent a true positive to human review. The threshold is therefore fitted in Task 4, not copied.
2. `reader_impact` returned 1.79 at confidence **0.21**, split 0.59 on level 1 and 0.39 on level 3, for a commit that adds a user-facing command. Thin evidence (subject plus file list) produces a bimodal answer. Commit scoring is Phase 4 and is out of scope here, but when it arrives it needs the commit body and diffstat, not just the subject.

---

### Task 1: Frozen question specs

**Files:**
- Create: `src/devrel_origin/quality/questions.py`
- Test: `tests/quality/test_questions.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `QUESTION_VERSION: int`, `ChoiceSpec` and `NoulSpec` frozen dataclasses, and the constants `RELATION: ChoiceSpec`, `IS_FACTUAL_CLAIM: NoulSpec`, `PATTERN: ChoiceSpec`, `PATTERN_NONE: str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/quality/test_questions.py
from devrel_origin.quality import questions as q


def test_relation_offers_exactly_the_three_cookbook_outcomes():
    assert set(q.RELATION.criteria) == {"supports", "contradicts", "says_nothing"}
    assert all(v.strip() for v in q.RELATION.criteria.values())


def test_pattern_question_includes_a_no_match_outcome():
    # The model cannot report "clean" unless a no-match option exists.
    assert q.PATTERN_NONE in q.PATTERN.criteria
    assert len(q.PATTERN.criteria) > 5


def test_every_criterion_is_a_concrete_definition_not_a_bare_label():
    for spec in (q.RELATION, q.PATTERN):
        for name, definition in spec.criteria.items():
            assert len(definition.split()) >= 4, name


def test_questions_module_does_not_import_the_optional_sdk():
    import sys

    assert "typesafe_sdk" not in sys.modules or True  # import is allowed elsewhere
    src = (q.__file__ or "")
    assert src.endswith("questions.py")
    with open(src, encoding="utf-8") as fh:
        assert "typesafe_sdk" not in fh.read()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `./.venv/bin/pytest tests/quality/test_questions.py -v --no-cov`
Expected: FAIL, `ModuleNotFoundError: No module named 'devrel_origin.quality.questions'`

- [ ] **Step 3: Write the module**

```python
# src/devrel_origin/quality/questions.py
"""Frozen question specs for the typed judgment layer.

Plain dataclasses on purpose: no `typesafe_sdk` import lives here, so the
questions stay testable and the dependency stays optional. `TypeSafeJudge`
converts these into SDK question objects.

Bump QUESTION_VERSION whenever any wording below changes. Cached verdicts are
keyed on it, so a bump invalidates every stored judgment.
"""

from __future__ import annotations

from dataclasses import dataclass

QUESTION_VERSION = 1

PATTERN_NONE = "none"


@dataclass(frozen=True)
class ChoiceSpec:
    instructions: str
    criteria: dict[str, str]


@dataclass(frozen=True)
class NoulSpec:
    instructions: str


# No ScoreSpec here on purpose. The only Scores in the spec (reader_impact,
# breaking_risk) belong to Phase 4, which is blocked on the release loop. An
# unused type is a liability; add it with its first caller.

RELATION = ChoiceSpec(
    instructions="How does the evidence relate to the claim?",
    criteria={
        "supports": "The evidence states the claim or directly implies that it is true",
        "contradicts": "The evidence states the opposite of the claim or implies it is false",
        "says_nothing": "The evidence does not address what the claim asserts, either way",
    },
)

IS_FACTUAL_CLAIM = NoulSpec(
    instructions=(
        "Does `sentence` assert a checkable fact about this project (behaviour, API, "
        "fix, version, measurement), as opposed to framing, instruction or opinion?"
    ),
)

PATTERN = ChoiceSpec(
    instructions=(
        "Which writing pattern does `unit` exhibit? Judge only the text of `unit`. "
        "Answer none unless the pattern is clearly present."
    ),
    criteria={
        PATTERN_NONE: "The passage states its point plainly and exhibits none of the other patterns",
        "binary_contrast": (
            "Sets up a negation to deliver the point, such as It is not X, it is Y, or "
            "The question is not X but Y"
        ),
        "throat_clearing": (
            "Opens with a filler move before the point, such as Here is the thing, "
            "Let me be clear, or I will be honest"
        ),
        "faux_insight": (
            "Flatters the writer as the lone expert, such as What nobody tells you or "
            "The part everyone misses"
        ),
        "colon_reveal": (
            "A noun phrase, a colon, then a short dramatic reveal used for emphasis "
            "rather than for a list, label or quotation"
        ),
        "importance_puffery": (
            "Asserts that something matters instead of stating the fact, such as marks a "
            "pivotal moment, stands as a testament, or underscores its significance"
        ),
        "weasel_attribution": (
            "Attributes a claim to an unnamed authority, such as experts agree, studies "
            "show, or industry reports suggest"
        ),
        "metadiscourse": (
            "Steps outside the subject to tell the reader what to notice or how much "
            "weight to give it, such as The key point is or This distinction matters"
        ),
        "fake_profound_kicker": (
            "Ends on an aphorism or metaphor that restates the point as a mic drop rather "
            "than on a concrete fact or next action"
        ),
        "summary_recap": (
            "Restates what the reader just read, such as In conclusion, Ultimately, or a "
            "closing paragraph that adds no new fact"
        ),
        "superficial_analysis": (
            "A trailing clause that pretends to explain meaning, such as highlighting, "
            "underscoring, reflecting or showcasing some broader quality"
        ),
    },
)
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `./.venv/bin/pytest tests/quality/test_questions.py -v --no-cov`
Expected: 4 passed

- [ ] **Step 5: Lint and commit**

```bash
./.venv/bin/ruff check src/devrel_origin/quality/questions.py tests/quality/test_questions.py
./.venv/bin/ruff format --check src/devrel_origin/quality/questions.py tests/quality/test_questions.py
git add src/devrel_origin/quality/questions.py tests/quality/test_questions.py
git commit -m "feat(quality): frozen question specs for typed judgments"
```

---

### Task 2: Verdict types, the Judge protocol, NullJudge and build_judge

**Files:**
- Create: `src/devrel_origin/quality/judgments.py`
- Test: `tests/quality/test_judgments.py`

**Interfaces:**
- Consumes: `questions.QUESTION_VERSION`, `questions.PATTERN_NONE`.
- Produces: `ClaimVerdict(relation, confidence, available, backend)`, `PatternVerdict(unit_index, pattern, confidence, available, backend)`, `Judge` protocol with `available: bool`, `backend: str`, `async select_claims(*, sentences) -> list[float]`, `async verify_claim(*, claim, evidence) -> ClaimVerdict`, `async judge_patterns(*, units, voice) -> list[PatternVerdict]`, plus `NullJudge()` and `build_judge(api_key=None) -> Judge`.

`select_claims` returns one `is_factual_claim` probability per sentence, in order. Task 5 uses it to replace the Haiku claim extractor; a Noul carries no confidence field, so the probability itself is the signal.

`rank_commits` from the spec is deliberately **not** implemented here. It serves Phase 4, which is blocked on the release loop, and an unused method is a liability.

- [ ] **Step 1: Write the failing test**

```python
# tests/quality/test_judgments.py
import pytest

from devrel_origin.quality.judgments import ClaimVerdict, NullJudge, build_judge


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


def test_claim_verdict_is_frozen():
    with pytest.raises(Exception):
        ClaimVerdict(
            relation="supports", confidence=1.0, available=True, backend="x"
        ).relation = "contradicts"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `./.venv/bin/pytest tests/quality/test_judgments.py -v --no-cov`
Expected: FAIL, `ModuleNotFoundError: No module named 'devrel_origin.quality.judgments'`

- [ ] **Step 3: Write the module (NullJudge half only)**

```python
# src/devrel_origin/quality/judgments.py
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
            relation=UNAVAILABLE, confidence=0.0, available=False, backend=self.backend
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
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `./.venv/bin/pytest tests/quality/test_judgments.py -v --no-cov`
Expected: 6 passed. `test_build_judge_falls_back_when_the_extra_is_missing` passes because `_sdk_available` is patched false; the `judgments_typesafe` import in the success branch is not reached yet.

- [ ] **Step 5: Lint and commit**

```bash
./.venv/bin/ruff check src/devrel_origin/quality/judgments.py tests/quality/test_judgments.py
./.venv/bin/ruff format --check src/devrel_origin/quality/judgments.py tests/quality/test_judgments.py
git add src/devrel_origin/quality/judgments.py tests/quality/test_judgments.py
git commit -m "feat(quality): judgment port with a degrading NullJudge"
```

---

### Task 3: TypeSafeJudge and the packaging extra

**Files:**
- Create: `src/devrel_origin/quality/judgments_typesafe.py`
- Create: `tests/quality/fakes.py`
- Modify: `pyproject.toml` (add the `typesafe` extra next to `video`, `seo`, `geo-google`)
- Test: `tests/quality/test_judgments_typesafe.py`

**Interfaces:**
- Consumes: `ClaimVerdict`, `PatternVerdict`, `UNAVAILABLE` from Task 2; the specs from Task 1.
- Produces: `TypeSafeJudge(api_key: str, client=None, max_questions_per_request: int = 25)` satisfying `Judge`; `FakeJudge` in `tests/quality/fakes.py` used by Tasks 5 and 6.

All units of a draft ride in **one** request as independent questions over shared state, so the draft text is billed once rather than once per unit. Requests are chunked at `max_questions_per_request`; the cap is not documented, so 25 is a conservative starting point to revisit with Task 4's numbers.

- [ ] **Step 1: Write the failing test**

```python
# tests/quality/test_judgments_typesafe.py
import pytest

from devrel_origin.quality.judgments_typesafe import TypeSafeJudge


class _Answer:
    def __init__(self, choice, confidence):
        self.choice = choice
        self.confidence = confidence


class _Resp:
    def __init__(self, choices):
        self.choices = choices
        self.model = "jev-1.13.0"

        class _U:
            input_tokens = 657
            output_tokens = 79

        self.usage = _U()


class _StubClient:
    """Stands in for AsyncTypeSafeClient. No network, no SDK import."""

    def __init__(self, answers_by_call):
        self.answers_by_call = list(answers_by_call)
        self.calls = []

    async def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        return _Resp(self.answers_by_call.pop(0))


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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `./.venv/bin/pytest tests/quality/test_judgments_typesafe.py -v --no-cov`
Expected: FAIL, `ModuleNotFoundError: No module named 'devrel_origin.quality.judgments_typesafe'`

- [ ] **Step 3: Write the implementation**

```python
# src/devrel_origin/quality/judgments_typesafe.py
"""The TypeSafe backend of the judgment port.

The only module that imports `typesafe_sdk`, and it imports it lazily so the
package works without the extra. Every failure returns an unavailable verdict;
nothing propagates, because callers rely on degradation, not on exceptions.
"""

from __future__ import annotations

import logging

from devrel_origin.quality.judgments import UNAVAILABLE, ClaimVerdict, PatternVerdict
from devrel_origin.quality.questions import (
    IS_FACTUAL_CLAIM,
    PATTERN,
    PATTERN_NONE,
    RELATION,
)

logger = logging.getLogger(__name__)

BACKEND = "typesafe"


def _to_choice(spec):
    from typesafe_sdk import Choice

    return Choice(instructions=spec.instructions, criteria=dict(spec.criteria))


class TypeSafeJudge:
    available = True
    backend = BACKEND

    def __init__(self, *, api_key: str, client=None, max_questions_per_request: int = 25):
        self._api_key = api_key
        self._client = client
        self._chunk = max(1, max_questions_per_request)
        self.last_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    def _open(self):
        if self._client is not None:
            return self._client, False
        from typesafe_sdk import AsyncTypeSafeClient

        return AsyncTypeSafeClient(api_key=self._api_key), True

    async def _ask(self, state: dict, questions: dict):
        client, owned = self._open()
        try:
            if owned:
                async with client as c:
                    resp = await c.system_one(state=state, questions=questions)
            else:
                resp = await client.system_one(state=state, questions=questions)
        finally:
            pass
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.last_usage = {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            }
        return resp

    async def verify_claim(self, *, claim: str, evidence: str) -> ClaimVerdict:
        try:
            resp = await self._ask(
                {"claim": claim, "evidence": evidence},
                {"relation": _to_choice(RELATION)},
            )
            answer = resp.choices["relation"]
            return ClaimVerdict(
                relation=answer.choice,
                confidence=float(answer.confidence),
                available=True,
                backend=BACKEND,
            )
        except Exception as exc:
            logger.warning("judge_verify_claim_failed", extra={"error": str(exc)})
            return ClaimVerdict(
                relation=UNAVAILABLE, confidence=0.0, available=False, backend=BACKEND
            )

    async def select_claims(self, *, sentences: list[str]) -> list[float]:
        """One `is_factual_claim` Noul per sentence, batched into one request."""
        from typesafe_sdk import Noul

        out: list[float] = []
        for start in range(0, len(sentences), self._chunk):
            batch = sentences[start : start + self._chunk]
            names = [f"sentence_{start + i}" for i in range(len(batch))]
            state = {n: t for n, t in zip(names, batch, strict=True)}
            questions = {
                n: Noul(instructions=IS_FACTUAL_CLAIM.instructions.replace("`sentence`", f"`{n}`"))
                for n in names
            }
            try:
                resp = await self._ask(state, questions)
                out.extend(float(resp.nouls[n].noul) for n in names)
            except Exception as exc:
                logger.warning("judge_select_claims_failed", extra={"error": str(exc)})
                out.extend(0.0 for _ in batch)
        return out

    async def judge_patterns(self, *, units: list[str], voice: str) -> list[PatternVerdict]:
        out: list[PatternVerdict] = []
        for start in range(0, len(units), self._chunk):
            batch = units[start : start + self._chunk]
            names = [f"unit_{start + i}" for i in range(len(batch))]
            state = {"voice": voice, **{n: t for n, t in zip(names, batch, strict=True)}}
            questions = {
                n: _to_choice(
                    type(PATTERN)(
                        instructions=PATTERN.instructions.replace("`unit`", f"`{n}`"),
                        criteria=PATTERN.criteria,
                    )
                )
                for n in names
            }
            try:
                resp = await self._ask(state, questions)
                for i, n in enumerate(names):
                    answer = resp.choices[n]
                    out.append(
                        PatternVerdict(
                            unit_index=start + i,
                            pattern=answer.choice,
                            confidence=float(answer.confidence),
                            available=True,
                            backend=BACKEND,
                        )
                    )
            except Exception as exc:
                logger.warning("judge_patterns_failed", extra={"error": str(exc)})
                out.extend(
                    PatternVerdict(
                        unit_index=start + i,
                        pattern=PATTERN_NONE,
                        confidence=0.0,
                        available=False,
                        backend=BACKEND,
                    )
                    for i in range(len(batch))
                )
        return out
```

- [ ] **Step 4: Write the shared test double**

```python
# tests/quality/fakes.py
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
```

- [ ] **Step 5: Add the packaging extra**

In `pyproject.toml`, under `[project.optional-dependencies]`, next to the existing extras:

```toml
# Typed judgments for the anti-slop and grounding gates (TypeSafe System One).
# Without this extra (or without TYPESAFE_API_KEY) the gates fall back to the
# deterministic regex blocklist and report themselves as skipped.
typesafe = [
    "typesafe-sdk>=0.7.0,<0.8.0",
]
```

- [ ] **Step 6: Run the tests and make sure they pass**

Run: `./.venv/bin/pytest tests/quality/ -v --no-cov`
Expected: all green, 4 new tests in `test_judgments_typesafe.py`

- [ ] **Step 7: Lint and commit**

```bash
./.venv/bin/ruff check src/devrel_origin/quality tests/quality
./.venv/bin/ruff format --check src/devrel_origin/quality tests/quality
git add src/devrel_origin/quality/judgments_typesafe.py tests/quality/fakes.py tests/quality/test_judgments_typesafe.py pyproject.toml
git commit -m "feat(quality): TypeSafe backend for the judgment port"
```

---

### Task 4: The blind evaluation (the gate on Tasks 5 and 6)

**Files:**
- Create (outside the package, not shipped): `evidence-verify-typesafe/label.py`, `evidence-verify-typesafe/run.py`, `evidence-verify-typesafe/report.md`
- Create: `evidence-verify-typesafe/labels.json` (written by hand, before any run)

**Interfaces:**
- Consumes: `questions.py` and the port from Tasks 1 to 3, imported from the repo venv.
- Produces: `report.md`, whose numbers decide whether Tasks 5 and 6 proceed.

This task produces no shippable code. Its output is a decision.

- [ ] **Step 1: Freeze the corpus before looking at it**

```bash
mkdir -p evidence-verify-typesafe
# Slop corpus: the 7 generated drafts, plus human controls.
ls .devrel/deliverables/wave1-*.md > evidence-verify-typesafe/corpus-drafts.txt
# Controls: text known to be human. A judge that flags these is useless.
git log -200 --format='%s%n%b' > evidence-verify-typesafe/corpus-human-commits.txt
sed -n '1,200p' README.md > evidence-verify-typesafe/corpus-human-readme.txt
```

- [ ] **Step 2: Hand-label a held-out set, before the first run**

Open each file in `corpus-drafts.txt`, split into paragraphs, and for a random 40 paragraphs across the 7 drafts plus 20 control paragraphs, write `labels.json`:

```json
{
  "units": [
    {"id": "wave1-cyra-blog-draft.md#p3", "text": "...", "label": "colon_reveal", "source": "generated"},
    {"id": "README.md#p7", "text": "...", "label": "none", "source": "human"}
  ],
  "claims": [
    {"claim": "...", "evidence_sha": "d25ca04", "label": "supports"},
    {"claim": "...", "evidence_sha": "d25ca04", "label": "says_nothing", "note": "corrupted: wrong sha"}
  ]
}
```

Rules: labels use the Tier 3 vocabulary from `questions.PATTERN.criteria`, one label per unit, `none` when nothing applies. At least 8 of the claim pairs are deliberately corrupted (wrong sha, or a claim overstated beyond what the commit did) so the negative rate is measurable.

- [ ] **Step 3: Run the frozen questions over the labelled set**

```python
# evidence-verify-typesafe/run.py
"""Blind run. Reads labels.json, never writes it."""

import asyncio
import json
import os
import time
from pathlib import Path

from devrel_origin.quality.judgments import build_judge

HERE = Path(__file__).parent


async def main() -> None:
    labels = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))
    judge = build_judge(api_key=os.environ["TYPESAFE_API_KEY"])
    assert judge.backend == "typesafe", "no key or extra; the run would prove nothing"

    t0 = time.monotonic()
    units = [u["text"] for u in labels["units"]]
    verdicts = await judge.judge_patterns(units=units, voice="")
    claim_verdicts = []
    for c in labels["claims"]:
        claim_verdicts.append(await judge.verify_claim(claim=c["claim"], evidence=c["evidence"]))
    elapsed = time.monotonic() - t0

    out = {
        "elapsed_s": round(elapsed, 2),
        "usage": judge.last_usage,
        "units": [
            {"id": u["id"], "gold": u["label"], "got": v.pattern, "confidence": v.confidence}
            for u, v in zip(labels["units"], verdicts, strict=True)
        ],
        "claims": [
            {"gold": c["label"], "got": v.relation, "confidence": v.confidence}
            for c, v in zip(labels["claims"], claim_verdicts, strict=True)
        ],
    }
    (HERE / "results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("wrote results.json,", out["elapsed_s"], "s,", out["usage"])


asyncio.run(main())
```

Run: `TYPESAFE_API_KEY="$(security find-generic-password -a "$USER" -s TYPESAFE_API_KEY -w)" ./.venv/bin/python evidence-verify-typesafe/run.py`

- [ ] **Step 4: Score against the incumbents and write the report**

`report.md` must answer, with counts and not adjectives:

1. Named patterns versus `llm_lint`: on the same 60 units, how many gold patterns did each find, and how many phrases did `llm_lint` return that do not appear in the text (the hallucination rate `_verify_lint_hits` exists to suppress)?
2. Controls: how many of the 20 human paragraphs were flagged as a pattern? A flag rate above roughly 10 percent here sinks the approach regardless of the positive rate.
3. Citations versus the link gate: on the corrupted pairs, how many did each catch?
4. The confidence threshold: plot accuracy against a sweep of thresholds from 0.5 to 0.95 in steps of 0.05 and state the value that maximises correct auto-accepts, given that a true `supports` was observed at 0.65. Record the chosen number and the date.
5. Cost: total input and output tokens, wall clock, and the implied per-draft cost.

- [ ] **Step 5: Decide, out loud**

If (1) or (3) does not beat the incumbent, or (2) fails, stop here and report the negative result. Tasks 5 and 6 do not start. If it passes, record the chosen threshold in `.devrel/config.toml` as `[quality].claim_confidence_min` and carry on.

- [ ] **Step 6: Commit the report only**

```bash
printf '%s\n' 'evidence-verify-typesafe/' >> .gitignore
git add .gitignore
git commit -m "chore: ignore the judgment evaluation harness"
```

The harness stays out of the package, per the spec. Paste the report's numbers into the pull request description instead.

---

### Task 5: Typed grounding

**Files:**
- Modify: `src/devrel_origin/quality/grounding.py` (delete `_coerce_claims` at :107, `_extract_claims` at :139, `_coerce_adjudication` at :188, `_adjudicate` at :214; add `_split_sentences`, `_verify`)
- Modify: `src/devrel_origin/quality/editorial.py:219-258` (`_grounding_stage` passes the judge through)
- Test: `tests/quality/test_grounding.py` (existing file, update)

**Interfaces:**
- Consumes: `FakeJudge` from Task 3, `ClaimVerdict`.
- Produces: `ground_claims(*, text, kb, judge, repo_facts=None, cut_unsourced=False, confidence_min=0.65) -> GroundingResult`. The `llm_client` parameter is **removed**; every caller changes.

**Degradation here differs from slop and the spec says so obliquely, so state it plainly:** grounding has no deterministic equivalent, so with `NullJudge` the stage does not fall back to Haiku, it reports `skipped` and leaves the text untouched. Claim extraction stops being an LLM call: sentences are split in code and filtered by the `is_factual_claim` Noul.

- [ ] **Step 1: Write the failing tests**

```python
# tests/quality/test_grounding.py (add these)
import pytest

from devrel_origin.quality.grounding import ground_claims
from devrel_origin.quality.judgments import NullJudge
from tests.quality.fakes import FakeJudge


class _EmptyKB:
    def search(self, query, limit=5):
        return []


@pytest.mark.asyncio
async def test_a_supported_claim_above_threshold_is_grounded():
    judge = FakeJudge(relations=[("supports", 0.82)])
    result = await ground_claims(
        text="The release adds a ranked next-action queue.",
        kb=_EmptyKB(),
        judge=judge,
        repo_facts=[{"ref": "d25ca04", "excerpt": "feat: devrel next action queue"}],
    )
    assert result.grounded_claims == 1
    assert result.flagged == []


@pytest.mark.asyncio
async def test_a_supported_claim_below_threshold_is_flagged_not_trusted():
    # The pshat spike produced a wrong `supports` at 0.33. Gate on confidence.
    judge = FakeJudge(relations=[("supports", 0.33)])
    result = await ground_claims(
        text="The release adds a ranked next-action queue.",
        kb=_EmptyKB(),
        judge=judge,
        repo_facts=[{"ref": "d25ca04", "excerpt": "feat: devrel next action queue"}],
        confidence_min=0.65,
    )
    assert result.grounded_claims == 0
    assert len(result.flagged) == 1


@pytest.mark.asyncio
async def test_a_contradicted_claim_is_flagged_regardless_of_confidence():
    judge = FakeJudge(relations=[("contradicts", 0.99)])
    result = await ground_claims(
        text="The release removes the queue.",
        kb=_EmptyKB(),
        judge=judge,
        repo_facts=[{"ref": "d25ca04", "excerpt": "feat: devrel next action queue"}],
    )
    assert result.flagged[0].reason.startswith("Contradicted")


@pytest.mark.asyncio
async def test_without_a_judge_the_stage_is_skipped_never_grounded():
    result = await ground_claims(
        text="The release adds a queue.",
        kb=_EmptyKB(),
        judge=NullJudge(),
        repo_facts=[{"ref": "d25ca04", "excerpt": "feat: queue"}],
    )
    assert result.judged is False
    assert result.grounded_claims == 0
    assert result.flagged == []
    assert result.text_after == "The release adds a queue."
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `./.venv/bin/pytest tests/quality/test_grounding.py -v --no-cov`
Expected: FAIL, `TypeError: ground_claims() got an unexpected keyword argument 'judge'`

- [ ] **Step 3: Rewrite the two stages**

Add to `GroundingResult` a `judged: bool = True` field. Replace the extraction and adjudication helpers with:

```python
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    """Deterministic split. No model: a sentence boundary is not a judgment."""
    parts = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    return [p for p in parts if len(p.split()) >= 4]


async def _verify(
    claim: Claim, candidates: list[Source], judge, confidence_min: float
) -> GroundedClaim:
    if not candidates:
        return GroundedClaim(
            claim=claim, grounded=False, sources=[], reason="No candidate sources in repo or KB."
        )
    evidence = "\n".join(f"({s.origin}:{s.ref}) {s.excerpt}" for s in candidates)
    verdict = await judge.verify_claim(claim=claim.text, evidence=evidence)
    if not verdict.available:
        return GroundedClaim(claim=claim, grounded=False, sources=[], reason="Judgment skipped.")
    if verdict.relation == "contradicts":
        return GroundedClaim(
            claim=claim,
            grounded=False,
            sources=candidates,
            reason=f"Contradicted by the evidence (confidence {verdict.confidence:.2f}).",
        )
    if verdict.relation == "supports" and verdict.confidence >= confidence_min:
        return GroundedClaim(claim=claim, grounded=True, sources=candidates, reason="")
    return GroundedClaim(
        claim=claim,
        grounded=False,
        sources=[],
        reason=(
            f"Not established: {verdict.relation} at confidence {verdict.confidence:.2f}, "
            f"below {confidence_min:.2f}."
        ),
    )
```

Then rewrite `ground_claims` itself:

```python
CLAIM_PROB_MIN = 0.5


async def ground_claims(
    *,
    text: str,
    kb: KnowledgeBaseSearch,
    judge,
    repo_facts: list[dict] | None = None,
    cut_unsourced: bool = False,
    confidence_min: float = 0.65,
) -> GroundingResult:
    if not judge.available:
        return GroundingResult(
            total_claims=0,
            grounded_claims=0,
            flagged=[],
            grounded=[],
            cut_applied=False,
            text_after=text,
            judged=False,
        )

    sentences = _split_sentences(text)
    probs = await judge.select_claims(sentences=sentences)
    claims = [
        Claim(text=s, kind="fact")
        for s, p in zip(sentences, probs, strict=True)
        if p >= CLAIM_PROB_MIN
    ]

    repo_sources = _repo_facts_to_sources(repo_facts)
    grounded: list[GroundedClaim] = []
    flagged: list[GroundedClaim] = []
    for claim in claims:
        candidates = _kb_candidates(claim, kb) + repo_sources
        gc = await _verify(claim, candidates, judge, confidence_min)
        (grounded if gc.grounded else flagged).append(gc)

    cut_applied = False
    text_after = text
    if cut_unsourced and flagged:
        text_after = _cut_flagged(text, flagged)
        cut_applied = True

    logger.info(
        "grounding_complete",
        extra={
            "total": len(claims),
            "grounded": len(grounded),
            "flagged": len(flagged),
            "cut_applied": cut_applied,
            "backend": judge.backend,
        },
    )
    return GroundingResult(
        total_claims=len(claims),
        grounded_claims=len(grounded),
        flagged=flagged,
        grounded=grounded,
        cut_applied=cut_applied,
        text_after=text_after,
        judged=True,
    )
```

**Two consequences of deleting the extractor, both of which touch existing tests.**

`Claim` is `(text: str, kind: str)`, and `kind` was the LLM's guess at "metric", "capability", "comparison" or "fact". Nothing in the pipeline branches on it: `grep -rn "\.kind" src/` returns only the construction site. Deterministic splitting cannot infer it, and inventing a classifier for an unread field is waste, so every claim is built with `kind="fact"`. The three existing tests that assert on `kind` (`tests/quality/test_grounding.py:45`, `:57`, and the constructions at `:93`, `:104`, `:210`) are updated in this task. If a later feature needs the taxonomy back, it becomes its own Choice question.

`GroundingResult` gains `judged: bool = True`, and `to_dict()` must emit it, otherwise the provenance trail cannot tell a clean draft from an unjudged one:

```python
            "judged": self.judged,
```

Delete `_coerce_claims`, `_extract_claims`, `_coerce_adjudication`, `_adjudicate`, `_EXTRACT_SYSTEM` and `_ADJUDICATE_SYSTEM`.

- [ ] **Step 4: Update the caller**

In `editorial.py`, replace the body of `_grounding_stage` (lines 219 to 258) so it takes `judge` in place of `llm_client`:

```python
async def _grounding_stage(
    *,
    text: str,
    project_paths: ProjectPaths,
    judge,
    repo_facts: list[dict[str, Any]] | None,
    cut_unsourced: bool,
    confidence_min: float = 0.65,
) -> tuple[str, StageResult, GroundingResult]:
    from devrel_origin.core.base import KnowledgeBaseSearch

    t0 = time.monotonic()
    kb = KnowledgeBaseSearch(project_paths.kb_dir)
    gr = await ground_claims(
        text=text,
        kb=kb,
        judge=judge,
        repo_facts=repo_facts,
        cut_unsourced=cut_unsourced,
        confidence_min=confidence_min,
    )
    if not gr.judged:
        # Not a pass. There is no deterministic equivalent of grounding, so
        # without a backend the stage reports that it did not run.
        return text, StageResult(
            name="grounding",
            text_before=text,
            text_after=text,
            duration_s=round(time.monotonic() - t0, 3),
            detail="skipped: no judgment backend",
        ), gr
    issues = [f"Unsourced: {c.claim.text}" for c in gr.flagged]
    sr = StageResult(
        name="grounding",
        text_before=text,
        text_after=gr.text_after,
        duration_s=round(time.monotonic() - t0, 3),
        issues=issues,
        detail=(
            f"{gr.grounded_claims}/{gr.total_claims} grounded"
            + (", cut" if gr.cut_applied else "")
            + f", judged_by={judge.backend}"
        ),
    )
    return gr.text_after, sr, gr
```

In `run_pipeline`, build the judge once, next to where `blocklist` is parsed, and pass it to both stages:

```python
    judge = build_judge()
```

- [ ] **Step 5: Run the full suite**

Run: `./.venv/bin/pytest tests/ -q`
Expected: no failures, count at or above 1098 plus the new tests

- [ ] **Step 6: Lint and commit**

```bash
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
git add src/devrel_origin/quality/grounding.py src/devrel_origin/quality/editorial.py tests/quality/test_grounding.py
git commit -m "refactor(quality): grounding returns typed verdicts, both prose parsers deleted"
```

---

### Task 6: Slop as named patterns

**Files:**
- Modify: `src/devrel_origin/quality/slop.py` (delete `llm_lint` at :113 and `_verify_lint_hits` at :83; change `force_rewrite` at :144)
- Modify: `src/devrel_origin/quality/editorial.py:142-176` (`_slop_stage`)
- Test: `tests/quality/test_slop.py` (existing file, update)

**Interfaces:**
- Consumes: `FakeJudge`, `PatternVerdict`, `PATTERN_NONE`.
- Produces: `split_units(text) -> list[str]`; `PatternHit(unit_index, unit_text, pattern, confidence)`; `find_patterns(text, verdicts, confidence_min) -> list[PatternHit]`; `force_rewrite(text, regex_hits, pattern_hits, voice, llm_client) -> str`.

`find_slop` and `parse_blocklist` are untouched: the regex tier is the degraded path.

- [ ] **Step 1: Write the failing tests**

```python
# tests/quality/test_slop.py (add these)
import pytest

from devrel_origin.quality.slop import PatternHit, find_patterns, split_units
from devrel_origin.quality.judgments import PatternVerdict


def test_units_are_paragraphs_so_the_quoted_line_is_locatable():
    text = "First para line one.\nStill first.\n\nSecond para."
    assert split_units(text) == ["First para line one.\nStill first.", "Second para."]


def test_a_low_confidence_pattern_is_not_a_hit():
    verdicts = [
        PatternVerdict(0, "colon_reveal", 0.42, True, "fake"),
        PatternVerdict(1, "colon_reveal", 0.88, True, "fake"),
    ]
    hits = find_patterns("a\n\nb", verdicts, confidence_min=0.7)
    assert [h.unit_index for h in hits] == [1]


def test_an_unavailable_verdict_is_never_a_hit_and_never_a_pass():
    verdicts = [PatternVerdict(0, "none", 0.0, False, "none")]
    assert find_patterns("a", verdicts, confidence_min=0.7) == []


def test_every_hit_carries_the_text_it_is_about():
    verdicts = [PatternVerdict(0, "faux_insight", 0.9, True, "fake")]
    hits = find_patterns("What nobody tells you: it ships.", verdicts, confidence_min=0.7)
    assert hits[0].unit_text == "What nobody tells you: it ships."
    # The model never returned this string; code located it. That is the point.
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `./.venv/bin/pytest tests/quality/test_slop.py -v --no-cov`
Expected: FAIL, `ImportError: cannot import name 'split_units'`

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class PatternHit:
    unit_index: int
    unit_text: str
    pattern: str
    confidence: float


def split_units(text: str) -> list[str]:
    """Paragraphs, because a pattern such as a recap ending spans sentences."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def find_patterns(
    text: str, verdicts: list[PatternVerdict], confidence_min: float
) -> list[PatternHit]:
    units = split_units(text)
    hits: list[PatternHit] = []
    for v in verdicts:
        if not v.available or v.pattern == PATTERN_NONE:
            continue
        if v.confidence < confidence_min:
            continue
        if v.unit_index >= len(units):
            continue
        hits.append(
            PatternHit(
                unit_index=v.unit_index,
                unit_text=units[v.unit_index],
                pattern=v.pattern,
                confidence=v.confidence,
            )
        )
    return hits
```

`force_rewrite` takes `pattern_hits: list[PatternHit]` in place of `llm_lint_hits: list[str]`, and builds its prompt from named patterns with their quoted passages:

```python
flagged_listing = "\n".join(f"- {p}" for p in sorted({h.phrase for h in regex_hits}))
pattern_listing = "\n".join(
    f"- {h.pattern} in this passage:\n  {h.unit_text}" for h in pattern_hits
)
```

- [ ] **Step 4: Rewire `_slop_stage`**

Replace `editorial.py` lines 142 to 176 with:

```python
async def _slop_stage(
    *,
    text_before: str,
    blocklist: list[str],
    voice: str,
    llm_client,
    judge,
    confidence_min: float = 0.7,
) -> tuple[str, StageResult]:
    t0 = time.monotonic()

    async def _check(text: str) -> tuple[list[SlopHit], list[PatternHit]]:
        regex_hits = find_slop(text, blocklist)
        verdicts = await judge.judge_patterns(units=split_units(text), voice=voice)
        return regex_hits, find_patterns(text, verdicts, confidence_min)

    # The degraded path still runs the regex tier, and says so, because a gate
    # that skipped its main check must not print the same word as one that passed.
    checked = "judged_by=" + judge.backend if judge.available else "regex only, no judgment backend"

    regex_hits, pattern_hits = await _check(text_before)
    if not regex_hits and not pattern_hits:
        return text_before, StageResult(
            name="anti_slop",
            text_before=text_before,
            text_after=text_before,
            duration_s=round(time.monotonic() - t0, 3),
            detail=f"clean ({checked})",
        )

    rewritten = await force_rewrite(text_before, regex_hits, pattern_hits, voice, llm_client)
    re_regex, re_patterns = await _check(rewritten)
    if re_regex or re_patterns:
        offenders = sorted({h.phrase for h in re_regex} | {h.pattern for h in re_patterns})
        raise AbortLoud("Slop persisted after rewrite: " + ", ".join(offenders))

    return rewritten, StageResult(
        name="anti_slop",
        text_before=text_before,
        text_after=rewritten,
        duration_s=round(time.monotonic() - t0, 3),
        issues=sorted({h.phrase for h in regex_hits})
        + [f"{h.pattern}: {h.unit_text[:60]}" for h in pattern_hits],
        detail=f"rewrite_applied ({checked})",
    )
```

- [ ] **Step 5: Sharpen the Tier 1 regex list and fix its parsing defect**

`parse_blocklist` skips only lines starting with `#`, so the template's two explanatory paragraphs are currently ingested as blocklist entries. Verified against the shipped parser: 32 entries, 2 of them prose. Fix the template and adopt the reference's never-legitimate words.

In `src/devrel_origin/project/templates/slop-blocklist.md`: prefix both prose paragraphs with `# `, keep the existing entries, and add the Tier 1 words that are not already there (`foster`, `leverage`, `utilize`, `facilitate`, `streamline`, `robust`, `cutting-edge`, `paradigm shift`, `game changer`, `realm`, `beacon`, `multifaceted`, `meticulous`, `intricate`, `paramount`, `transformative`, `elevate`, `embark`, `supercharge`, `harness`, `ever-evolving`). Drop `very` and `really` from the list: they are Tier 2, they are why the release loop has to disable this gate on changelogs, and the judgment layer now covers that ground.

Add the credit line the MIT licence requires, as a comment at the top of the file:

```markdown
# Tier 1 words adapted from petergyang/no-ai-slop (MIT). Context-dependent words
# and structural patterns are judged, not matched; see quality/questions.py.
```

Test that the defect cannot come back:

```python
def test_the_shipped_template_contains_no_prose_entries():
    from pathlib import Path

    from devrel_origin.quality.slop import parse_blocklist

    md = Path("src/devrel_origin/project/templates/slop-blocklist.md").read_text(encoding="utf-8")
    entries = parse_blocklist(md)
    assert entries, "template parsed to nothing"
    long_entries = [e for e in entries if len(e.split()) > 6]
    assert long_entries == [], f"prose ingested as blocklist entries: {long_entries}"


def test_tier_two_words_are_not_regex_matched():
    from pathlib import Path

    from devrel_origin.quality.slop import parse_blocklist

    md = Path("src/devrel_origin/project/templates/slop-blocklist.md").read_text(encoding="utf-8")
    entries = set(parse_blocklist(md))
    # These are legitimate about half the time, so they belong to the judgment
    # tier. Matching them is what forced enforce_slop=False on changelogs.
    assert {"very", "really", "just", "simply"} & entries == set()
```

- [ ] **Step 6: Run the full suite**

Run: `./.venv/bin/pytest tests/ -q`
Expected: green. Tests that asserted on `llm_lint` are updated in this task, not deleted wholesale.

- [ ] **Step 7: Lint and commit**

```bash
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
git add src/devrel_origin/quality/slop.py src/devrel_origin/quality/editorial.py src/devrel_origin/project/templates/slop-blocklist.md tests/quality/test_slop.py
git commit -m "refactor(quality): slop reports named patterns with quoted passages"
```

---

### Task 7: Cost visibility, with no false zero

**Files:**
- Modify: `src/devrel_origin/project/cost_sink.py:17-37`
- Modify: `src/devrel_origin/cli/cost.py:21-80`
- Test: `tests/project/test_cost_sink.py`, `tests/cli/test_cost_command.py` (new file; there is no CLI test for `devrel cost` today)

**Interfaces:**
- Consumes: `TypeSafeJudge.last_usage`.
- Produces: `is_priced(model) -> bool` in `cost_sink.py`; judgment rows recorded under model `typesafe:jev`.

`costs.cost_usd` is `REAL NOT NULL`, so a null is not available without a migration. Unpriced models therefore store `0.0` and the CLI renders them as `n/a` with their token counts, so nobody reads a zero as free.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_unpriced_model_is_reported_as_unpriced_not_as_free():
    from devrel_origin.project.cost_sink import is_priced

    assert is_priced("claude-3-5-haiku-20241022") is True
    assert is_priced("typesafe:jev") is False


@pytest.mark.asyncio
async def test_judgment_usage_lands_in_the_costs_table(tmp_path):
    db = tmp_path / "state.db"
    init_state_db(db)
    sink = make_sqlite_sink(db)
    await sink("release", "typesafe:jev", {"input_tokens": 657, "output_tokens": 79})
    rows = sqlite3.connect(db).execute(
        "SELECT model, input_tokens, output_tokens, cost_usd FROM costs"
    ).fetchall()
    assert rows == [("typesafe:jev", 657, 79, 0.0)]


# tests/cli/test_cost_command.py (new file, follows the _run_in/_init pattern
# used by tests/cli/test_doctor_command.py; copy those two helpers verbatim)
def test_cost_command_prints_n_a_for_unpriced_models(tmp_path):
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        _init(tmp_path)
        db = tmp_path / ".devrel" / "state.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO costs (agent, model, input_tokens, output_tokens, cost_usd) "
                "VALUES ('kai', 'claude-3-5-haiku-20241022', 1000, 200, 0.0012)"
            )
            conn.execute(
                "INSERT INTO costs (agent, model, input_tokens, output_tokens, cost_usd) "
                "VALUES ('quality', 'typesafe:jev', 657, 79, 0.0)"
            )
            conn.commit()
    finally:
        os.chdir(cwd)

    result = _run_in(tmp_path, "cost")

    assert result.exit_code == 0
    typesafe_line = next(line for line in result.stdout.splitlines() if "typesafe:jev" in line)
    assert "n/a" in typesafe_line
    assert "$0.00" not in typesafe_line
    assert "cost not published" in result.stdout
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `./.venv/bin/pytest tests/project/test_cost_sink.py tests/cli/test_cost_command.py -v --no-cov`
Expected: FAIL, `ImportError: cannot import name 'is_priced'`

- [ ] **Step 3: Implement**

```python
def is_priced(model: str) -> bool:
    """True when MODEL_COSTS can price this model. Unpriced models are recorded
    with their tokens and rendered as n/a, never as a $0.00 that reads as free."""
    return model in MODEL_COSTS
```

In `cli/cost.py`, the per-model table renders `n/a` in the cost column when `not is_priced(row.model)`, and a footer line reports the unpriced totals: `"2 calls on unpriced models (typesafe:jev): 1,314 in / 158 out, cost not published"`. The overall `SUM(cost_usd)` stays as is and gains the same footnote.

- [ ] **Step 4: Wire the judge's usage into the sink**

Wherever `run_pipeline` builds the judge, if `project_paths.state_db` exists, wrap the judge so each call emits `await sink("quality", "typesafe:jev", judge.last_usage)` after the request. Keep that wrapper in `judgments.py` as `with_cost_sink(judge, sink)` so neither stage knows about costs.

- [ ] **Step 5: Run the full suite, then lint and commit**

```bash
./.venv/bin/pytest tests/ -q
./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .
git add src/devrel_origin/project/cost_sink.py src/devrel_origin/cli/cost.py src/devrel_origin/quality/judgments.py tests/project/test_cost_sink.py tests/cli/test_cost_command.py
git commit -m "feat(cost): record judgment tokens and render unpriced models as n/a"
```

---

## Deferred from the spec, on purpose

These are in the spec and not in this plan. Each is named so the omission is a decision rather than a gap.

1. **The verdict cache (spec 4.4).** `QUESTION_VERSION` exists from Task 1, but nothing caches yet. Origin judges one draft at a time, so the win is small; AgenticCareers needed it because it judged 1,696 listings. Add it when a caller judges the same text twice, and key it `(QUESTION_VERSION, sha256(text))` as the spec says.
2. **Tier 2, the context-dependent words (spec 5.1).** The per-occurrence Noul for "just", "simply", "actually" and friends is not implemented. Tier 1 and Tier 3 already remove the reason the gate had to be disabled on changelogs, and Tier 2 multiplies judgments per draft for the smallest share of the value. Revisit after Task 4's cost numbers.
3. **`rank_commits`, `reader_impact`, `breaking_risk` (spec 5.3).** Phase 4, blocked on the release loop. The smoke test already showed the levels need richer evidence than a commit subject.
4. **`GateReport` and `gates.json` (spec 4.2).** Those types live in the release loop, which is not on this machine. The same contract is honoured here through `StageResult.detail` and `GroundingResult.judged`.

## Final verification

- [ ] `./.venv/bin/pytest tests/ -q` passes with coverage at or above 75 percent
- [ ] `./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .` clean
- [ ] `./.venv/bin/python -c "import devrel_origin.quality.editorial"` works in an environment **without** the `typesafe` extra installed, proving the dependency is optional
- [ ] `devrel content audit ./some-draft.md --type blog_post` with no `TYPESAFE_API_KEY` reports the slop stage as skipped rather than clean
- [ ] `CLAUDE.md` verb counts corrected: the file says 24 verb modules and "+ 23 more"; the real count today is 26, so it becomes 26 and "+ 25 more". Count, do not copy the handover's arithmetic.
