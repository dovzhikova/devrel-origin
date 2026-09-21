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


def _question_types():
    """(Choice, Noul), preferring the real SDK classes when the extra is installed.

    Falls back to `SimpleNamespace` so a caller that injects its own client (every
    test in this suite) can build question payloads without the optional `typesafe`
    extra: those callers never inspect the payload's type, only the attributes
    `_ask` reads back off the response. Production always has the real classes,
    because `build_judge()` only ever constructs `TypeSafeJudge` after
    `_sdk_available()` has confirmed the extra imports cleanly.
    """
    try:
        from typesafe_sdk import Choice, Noul

        return Choice, Noul
    except ImportError:
        from types import SimpleNamespace

        return SimpleNamespace, SimpleNamespace


def _to_choice(spec):
    choice_type, _ = _question_types()
    return choice_type(instructions=spec.instructions, criteria=dict(spec.criteria))


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
        if owned:
            async with client as c:
                resp = await c.system_one(state=state, questions=questions)
        else:
            resp = await client.system_one(state=state, questions=questions)
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
        _, noul_type = _question_types()

        out: list[float] = []
        for start in range(0, len(sentences), self._chunk):
            batch = sentences[start : start + self._chunk]
            names = [f"sentence_{start + i}" for i in range(len(batch))]
            state = dict(zip(names, batch, strict=True))
            questions = {
                n: noul_type(
                    instructions=IS_FACTUAL_CLAIM.instructions.replace("`sentence`", f"`{n}`")
                )
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
        choice_type, _ = _question_types()

        out: list[PatternVerdict] = []
        for start in range(0, len(units), self._chunk):
            batch = units[start : start + self._chunk]
            names = [f"unit_{start + i}" for i in range(len(batch))]
            state = {"voice": voice, **dict(zip(names, batch, strict=True))}
            questions = {
                n: choice_type(
                    instructions=PATTERN.instructions.replace("`unit`", f"`{n}`"),
                    criteria=dict(PATTERN.criteria),
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
