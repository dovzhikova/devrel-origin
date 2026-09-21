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
    PATTERN_NONE,
    PATTERN_NOULS,
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
        self._reset_usage()

    def _reset_usage(self) -> None:
        """`last_usage` covers one public call, summed across its chunked requests.

        Each public method resets this at its start; `_ask` adds into it, never
        overwrites it. It is not a lifetime total: a caller reads it after every
        public call to emit to a cost sink, and a running total would double count.
        """
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
            self.last_usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self.last_usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        return resp

    async def verify_claim(self, *, claim: str, evidence: str) -> ClaimVerdict:
        self._reset_usage()
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

    async def select_claims(self, *, sentences: list[str]) -> list[float] | None:
        """One `is_factual_claim` Noul per sentence, batched into one request.

        Returns ``None`` when any chunked request failed, including a partial
        failure after earlier chunks succeeded. A padded 0.0 per failed
        sentence would be indistinguishable from a real "not a claim"
        judgment, and a caller (``grounding.ground_claims``) must be able to
        tell "we determined this" from "we never asked" so it can fall back
        instead of silently scoring zero claims.
        """
        self._reset_usage()
        from typesafe_sdk import Noul

        out: list[float] = []
        for start in range(0, len(sentences), self._chunk):
            batch = sentences[start : start + self._chunk]
            names = [f"sentence_{start + i}" for i in range(len(batch))]
            state = dict(zip(names, batch, strict=True))
            questions = {
                n: Noul(instructions=IS_FACTUAL_CLAIM.instructions.replace("`sentence`", f"`{n}`"))
                for n in names
            }
            try:
                resp = await self._ask(state, questions)
                out.extend(float(resp.nouls[n].noul) for n in names)
            except Exception as exc:
                logger.warning("judge_select_claims_failed", extra={"error": str(exc)})
                return None
        return out

    async def judge_patterns(self, *, units: list[str], voice: str) -> list[PatternVerdict]:
        """One Noul per pattern per unit: a unit can exhibit more than one.

        Whole units are packed per request, never split: a unit's own
        question count (one per PATTERN_NOULS entry) sets how many units fit
        under `max_questions_per_request`, at least one unit per request.
        """
        self._reset_usage()
        from typesafe_sdk import Noul

        pattern_keys = list(PATTERN_NOULS)
        questions_per_unit = len(pattern_keys)
        units_per_request = max(1, self._chunk // questions_per_unit)

        out: list[PatternVerdict] = []
        for start in range(0, len(units), units_per_request):
            batch = units[start : start + units_per_request]
            names = [f"unit_{start + i}" for i in range(len(batch))]
            state = {"voice": voice, **dict(zip(names, batch, strict=True))}
            questions = {
                f"{n}__{pattern}": Noul(
                    instructions=PATTERN_NOULS[pattern].instructions.replace("`unit`", f"`{n}`")
                )
                for n in names
                for pattern in pattern_keys
            }
            try:
                resp = await self._ask(state, questions)
                for i, n in enumerate(names):
                    probabilities = {
                        pattern: float(resp.nouls[f"{n}__{pattern}"].noul)
                        for pattern in pattern_keys
                    }
                    best_pattern = max(probabilities, key=probabilities.get)
                    out.append(
                        PatternVerdict(
                            unit_index=start + i,
                            pattern=best_pattern,
                            confidence=probabilities[best_pattern],
                            available=True,
                            backend=BACKEND,
                            probabilities=probabilities,
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
