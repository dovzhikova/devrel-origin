"""Frozen question specs for the typed judgment layer.

Plain dataclasses on purpose: no SDK imports live here, so the questions stay
testable and the dependency stays optional. The TypeSafe integration converts
these into SDK question objects.

Bump QUESTION_VERSION whenever any wording below changes. Cached verdicts are
keyed on it, so a bump invalidates every stored judgment.
"""

from __future__ import annotations

from dataclasses import dataclass

QUESTION_VERSION = 2

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
        "supports": ("The evidence states the claim or directly implies that it is true"),
        "contradicts": ("The evidence states the opposite of the claim or implies it is false"),
        "says_nothing": ("The evidence does not address what the claim asserts, either way"),
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
        PATTERN_NONE: (
            "The passage states its point plainly and exhibits none of the other patterns"
        ),
        "binary_contrast": (
            "Uses a negation as a rhetorical setup for the point, such as It is not X, "
            "it is Y, or The question is not X but Y, where the negated X is a straw "
            "man added for emphasis rather than a claim anyone was making. Not this "
            "pattern: a factual or technical distinction that corrects a specific, "
            "plausible misreading or states a real limit of scope"
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
            "Ends on an aphorism or metaphor that restates the point as a mic drop "
            "rather than on a concrete fact or next action"
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
