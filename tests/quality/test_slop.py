"""Tests for slop blocklist matching, LLM lint, and force-rewrite."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devrel_origin.quality.judgments import PatternVerdict
from devrel_origin.quality.questions import PATTERN_THRESHOLDS
from devrel_origin.quality.slop import (
    PatternHit,
    SlopHit,
    find_patterns,
    find_slop,
    force_rewrite,
    llm_lint,
    parse_blocklist,
    split_units,
)


def test_parse_blocklist_strips_comments_and_blanks():
    md = """# Anti-slop blocklist

## Hedge words
delve
furthermore

## CTAs
learn more
get started today
"""
    out = parse_blocklist(md)
    assert out == ["delve", "furthermore", "learn more", "get started today"]


def test_parse_blocklist_lowercases():
    out = parse_blocklist("Delve\nFURTHERMORE\n")
    assert out == ["delve", "furthermore"]


def test_find_slop_word_boundary_match():
    text = "We delve into the topic, furthermore the tapestry unfolds."
    hits = find_slop(text, ["delve", "furthermore", "tapestry"])
    assert {h.phrase for h in hits} == {"delve", "furthermore", "tapestry"}


def test_find_slop_case_insensitive():
    text = "DELVE into this. Furthermore."
    hits = find_slop(text, ["delve", "furthermore"])
    assert len(hits) == 2


def test_find_slop_does_not_match_substrings():
    """`delve` should not match `delivery` or `develop`."""
    text = "We develop and delivery great things."
    hits = find_slop(text, ["delve"])
    assert hits == []


def test_find_slop_handles_multi_word_phrases():
    text = "Get started today with our platform."
    hits = find_slop(text, ["get started today"])
    assert len(hits) == 1
    assert hits[0].phrase == "get started today"


def test_find_slop_empty_when_no_matches():
    assert find_slop("Direct, sharp, no fluff.", ["delve", "tapestry"]) == []


def test_the_shipped_template_contains_no_prose_entries():
    from pathlib import Path

    md = Path("src/devrel_origin/project/templates/slop-blocklist.md").read_text(
        encoding="utf-8"
    )
    entries = parse_blocklist(md)
    assert entries, "template parsed to nothing"
    long_entries = [e for e in entries if len(e.split()) > 6]
    assert long_entries == [], f"prose ingested as blocklist entries: {long_entries}"


def test_very_and_really_stay_in_tier_one_since_tier_two_was_never_built():
    # Tier 2 (context-dependent intensifiers judged, not matched) was never
    # built, so removing these would weaken the gate rather than sharpen it.
    from pathlib import Path

    md = Path("src/devrel_origin/project/templates/slop-blocklist.md").read_text(
        encoding="utf-8"
    )
    entries = set(parse_blocklist(md))
    assert {"very", "really"} <= entries


def test_tier_one_additions_from_no_ai_slop_are_present():
    from pathlib import Path

    md = Path("src/devrel_origin/project/templates/slop-blocklist.md").read_text(
        encoding="utf-8"
    )
    entries = set(parse_blocklist(md))
    added = {
        "foster",
        "leverage",
        "utilize",
        "facilitate",
        "streamline",
        "robust",
        "cutting-edge",
        "paradigm shift",
        "game changer",
        "realm",
        "beacon",
        "multifaceted",
        "meticulous",
        "intricate",
        "paramount",
        "transformative",
        "elevate",
        "embark",
        "supercharge",
        "harness",
        "ever-evolving",
    }
    assert added <= entries


def test_mit_credit_line_present():
    from pathlib import Path

    md = Path("src/devrel_origin/project/templates/slop-blocklist.md").read_text(
        encoding="utf-8"
    )
    assert "petergyang/no-ai-slop" in md
    assert "MIT" in md


@pytest.mark.asyncio
async def test_llm_lint_calls_haiku_and_parses_phrases():
    client = MagicMock()
    client.generate = AsyncMock(return_value="phrase one\nphrase two\n")
    # Phrases must appear in the source text (post-fix verification step).
    out = await llm_lint("draft uses phrase one and also phrase two", "voice prose", client)
    assert out == ["phrase one", "phrase two"]
    # Verify it called with model="haiku" for cost.
    call_kwargs = client.generate.await_args.kwargs
    assert call_kwargs.get("model") == "haiku"


@pytest.mark.asyncio
async def test_llm_lint_returns_empty_on_empty_response():
    client = MagicMock()
    client.generate = AsyncMock(return_value="")
    assert await llm_lint("draft", "voice", client) == []


@pytest.mark.asyncio
async def test_llm_lint_filters_blank_lines_and_bullets():
    client = MagicMock()
    client.generate = AsyncMock(return_value="- phrase one\n  \n* phrase two\n#commented\n")
    out = await llm_lint("the draft has phrase one and phrase two in it", "voice", client)
    assert out == ["phrase one", "phrase two"]


@pytest.mark.asyncio
async def test_llm_lint_drops_hallucinated_phrases():
    """Haiku regression seen in 2026-05-08 dogfood: returned 'replace this
    blockquote' / 'replace with' against a draft that contained neither.
    Hallucinated phrases must be filtered before they reach force_rewrite."""
    client = MagicMock()
    client.generate = AsyncMock(
        return_value=(
            "in essence\n"
            "replace this blockquote\n"  # hallucination
            "moving forward\n"
            "replace with\n"  # hallucination
        )
    )
    text = "In essence, this is fine. Moving forward, we ship."
    out = await llm_lint(text, "voice", client)
    assert "in essence" in out
    assert "moving forward" in out
    assert "replace this blockquote" not in out
    assert "replace with" not in out


@pytest.mark.asyncio
async def test_llm_lint_case_insensitive_match():
    """Phrases match case-insensitively against the source text."""
    client = MagicMock()
    client.generate = AsyncMock(return_value="In Essence\nMOVING FORWARD\n")
    text = "in essence we ship; moving forward we iterate."
    out = await llm_lint(text, "voice", client)
    # Phrases are normalized to lowercase by _normalize_lint_lines.
    assert "in essence" in out
    assert "moving forward" in out


@pytest.mark.asyncio
async def test_llm_lint_returns_empty_when_all_hallucinated():
    """If Haiku returns only hallucinations, output is empty (not the lint set)."""
    client = MagicMock()
    client.generate = AsyncMock(return_value="ghost phrase one\nghost phrase two\n")
    out = await llm_lint("a clean draft with no flagged content", "voice", client)
    assert out == []


@pytest.mark.asyncio
async def test_force_rewrite_passes_hits_to_llm_and_returns_text():
    client = MagicMock()
    client.generate = AsyncMock(return_value="the rewritten text")
    hits = [SlopHit(phrase="delve", start=0, end=5)]
    out = await force_rewrite("delve into x", hits, ["extra-slop"], "voice", client)
    assert out == "the rewritten text"
    user_prompt = client.generate.await_args.kwargs["user_prompt"]
    # Must list every flagged item in the rewrite prompt.
    assert "delve" in user_prompt
    assert "extra-slop" in user_prompt


def test_units_are_paragraphs_so_the_quoted_line_is_locatable():
    text = "First para line one.\nStill first.\n\nSecond para."
    assert split_units(text) == ["First para line one.\nStill first.", "Second para."]


def test_find_patterns_flags_a_unit_when_any_pattern_clears_its_own_threshold():
    verdicts = [
        PatternVerdict(
            unit_index=0,
            pattern="faux_insight",
            confidence=0.42,
            available=True,
            backend="fake",
            probabilities={"faux_insight": 0.42},
        ),
        PatternVerdict(
            unit_index=1,
            pattern="importance_puffery",
            confidence=0.42,
            available=True,
            backend="fake",
            probabilities={"importance_puffery": 0.42},
        ),
    ]
    # faux_insight's threshold is 0.30 (clears at 0.42); importance_puffery's
    # is 0.90 (0.42 does not clear it).
    hits = find_patterns("a\n\nb", verdicts, PATTERN_THRESHOLDS)
    assert [h.unit_index for h in hits] == [0]


def test_find_patterns_names_the_highest_probability_pattern_that_cleared():
    verdicts = [
        PatternVerdict(
            unit_index=0,
            pattern="importance_puffery",
            confidence=0.85,
            available=True,
            backend="fake",
            probabilities={"importance_puffery": 0.85, "faux_insight": 0.75},
        ),
    ]
    # importance_puffery has the higher raw probability (0.85) but its
    # threshold is 0.90, so it never clears; faux_insight (0.75 >= 0.30)
    # does, and is the only candidate, so it names the hit.
    hits = find_patterns("a", verdicts, PATTERN_THRESHOLDS)
    assert len(hits) == 1
    assert hits[0].pattern == "faux_insight"
    assert hits[0].confidence == 0.75


def test_find_patterns_an_unavailable_verdict_is_never_a_hit_and_never_a_pass():
    verdicts = [
        PatternVerdict(
            unit_index=0, pattern="none", confidence=0.0, available=False, backend="none"
        )
    ]
    assert find_patterns("a", verdicts, PATTERN_THRESHOLDS) == []


def test_find_patterns_a_verdict_with_no_probabilities_is_never_a_hit():
    # NullJudge and a scripted "clean" verdict never carry probabilities.
    verdicts = [
        PatternVerdict(unit_index=0, pattern="none", confidence=0.0, available=True, backend="x")
    ]
    assert find_patterns("a", verdicts, PATTERN_THRESHOLDS) == []


def test_find_patterns_every_hit_carries_the_text_it_is_about():
    verdicts = [
        PatternVerdict(
            unit_index=0,
            pattern="faux_insight",
            confidence=0.9,
            available=True,
            backend="fake",
            probabilities={"faux_insight": 0.9},
        )
    ]
    hits = find_patterns("What nobody tells you: it ships.", verdicts, PATTERN_THRESHOLDS)
    assert hits[0].unit_text == "What nobody tells you: it ships."
    # The model never returned this string; code located it. That is the point.


@pytest.mark.asyncio
async def test_force_rewrite_with_pattern_hits_quotes_the_flagged_passage():
    client = MagicMock()
    client.generate = AsyncMock(return_value="the rewritten text")
    regex_hits = [SlopHit(phrase="delve", start=0, end=5)]
    pattern_hits = [
        PatternHit(
            unit_index=0,
            unit_text="What nobody tells you: it ships.",
            pattern="faux_insight",
            confidence=0.9,
        )
    ]
    out = await force_rewrite("delve into x", regex_hits, pattern_hits, "voice", client)
    assert out == "the rewritten text"
    user_prompt = client.generate.await_args.kwargs["user_prompt"]
    assert "delve" in user_prompt
    assert "faux_insight" in user_prompt
    assert "What nobody tells you: it ships." in user_prompt
