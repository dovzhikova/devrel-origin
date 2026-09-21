"""Tests for the grounding stage (quality/grounding.py).

Grounding splits the draft into sentences deterministically, selects claims
and verifies them through the typed judgment port (``Judge``), then retrieves
KB + repo candidate sources. The KB is a real TF-IDF index over a tmp
directory. No network, no real APIs: every judgment comes from ``FakeJudge``
or ``NullJudge``.
"""

from __future__ import annotations

import pytest

from devrel_origin.core.base import KnowledgeBaseSearch
from devrel_origin.quality.grounding import (
    Claim,
    GroundedClaim,
    GroundingResult,
    Source,
    _cut_flagged,
    _kb_candidates,
    _repo_facts_to_sources,
    ground_claims,
)
from devrel_origin.quality.judgments import NullJudge
from tests.quality.fakes import FakeJudge


def _kb(tmp_path):
    d = tmp_path / "kb"
    (d / "docs").mkdir(parents=True)
    (d / "docs" / "otel.md").write_text(
        "# OpenTelemetry\n\nThe agent auto-instruments applications for "
        "OpenTelemetry with zero code changes.\n"
    )
    (d / "docs" / "pricing.md").write_text("# Pricing\n\nFree tier includes 5 seats.\n")
    return KnowledgeBaseSearch(d)


class _EmptyKB:
    def search(self, query, limit=5, **kwargs):
        # kwargs absorbs _kb_candidates' content_truncate / pad_with_remaining,
        # which the real KnowledgeBaseSearch.search accepts; this fake always
        # returns no matches regardless.
        return []


# --- pure helpers ----------------------------------------------------------


def test_repo_facts_to_sources_skips_incomplete():
    src = _repo_facts_to_sources([{"ref": "", "excerpt": "x"}, {"ref": "a", "excerpt": ""}])
    assert src == []


def test_kb_candidates_only_real_matches(tmp_path):
    kb = _kb(tmp_path)
    cands = _kb_candidates(Claim(text="auto-instrument OpenTelemetry", kind="capability"), kb)
    assert cands, "expected at least one KB match"
    assert all(c.origin == "kb" for c in cands)
    assert any("otel.md" in c.ref for c in cands)


def test_cut_flagged_removes_claim_text():
    text = "Our product is fast. It cuts build time 40%. Try it."
    flagged = [
        GroundedClaim(claim=Claim(text="It cuts build time 40%.", kind="metric"), grounded=False)
    ]
    out = _cut_flagged(text, flagged)
    assert "40%" not in out
    assert "Our product is fast." in out


def test_grounding_result_to_dict_is_serializable():
    import json

    gr = GroundingResult(
        total_claims=1,
        grounded_claims=1,
        flagged=[],
        grounded=[
            GroundedClaim(
                claim=Claim(text="x", kind="fact"),
                grounded=True,
                sources=[Source(origin="kb", ref="docs/x.md", excerpt="e")],
                reason="ok",
            )
        ],
        cut_applied=False,
        text_after="x",
    )
    d = gr.to_dict()
    json.dumps(d)  # must not raise
    assert d["grounded"][0]["sources"][0]["ref"] == "docs/x.md"
    assert d["judged"] is True


# --- end-to-end (typed judgments) -------------------------------------------


@pytest.mark.asyncio
async def test_ground_claims_grounded_and_flagged(tmp_path):
    kb = _kb(tmp_path)
    # Sentence 1 matches the KB verbatim; sentence 2 has no KB or repo match,
    # so it is flagged without even consuming a judge.verify_claim call.
    text = (
        "The agent auto-instruments applications for OpenTelemetry with zero "
        "code changes. It is trusted by many teams around the world today."
    )
    judge = FakeJudge(relations=[("supports", 0.82)])

    result = await ground_claims(text=text, kb=kb, judge=judge)

    assert isinstance(result, GroundingResult)
    assert result.total_claims == 2
    assert result.grounded_claims == 1
    assert len(result.flagged) == 1
    assert result.flagged[0].reason == "No candidate sources in repo or KB."
    assert result.grounded[0].sources[0].origin == "kb"
    assert result.cut_applied is False


@pytest.mark.asyncio
async def test_ground_claims_cut_removes_unsourced(tmp_path):
    kb = _kb(tmp_path)
    text = (
        "The agent auto-instruments applications for OpenTelemetry with zero "
        "code changes. It is used by NASA for critical infrastructure systems."
    )
    judge = FakeJudge(relations=[("supports", 0.9)])

    result = await ground_claims(text=text, kb=kb, judge=judge, cut_unsourced=True)
    assert result.cut_applied is True
    assert "NASA" not in result.text_after
    assert "OpenTelemetry" in result.text_after


@pytest.mark.asyncio
async def test_ground_claims_no_claims_is_noop(tmp_path):
    kb = _kb(tmp_path)
    judge = FakeJudge()
    result = await ground_claims(text="Hello.", kb=kb, judge=judge)
    assert result.total_claims == 0
    assert result.grounded_claims == 0
    assert result.flagged == []
    assert result.text_after == "Hello."


@pytest.mark.asyncio
async def test_ground_claims_uses_repo_facts(tmp_path):
    kb = _kb(tmp_path)
    # No overlap with the KB docs, so the only candidate is the repo fact.
    text = "We recently shipped a brand new bundler cache feature to the platform today."
    repo_facts = [{"ref": "commit:deadbeef01", "excerpt": "feat: add bundler cache"}]
    judge = FakeJudge(relations=[("supports", 0.9)])

    result = await ground_claims(text=text, kb=kb, judge=judge, repo_facts=repo_facts)
    assert result.grounded_claims == 1
    assert result.grounded[0].sources[0].origin == "repo"


# --- brief's four tests ------------------------------------------------------


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
