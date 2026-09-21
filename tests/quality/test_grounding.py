"""Tests for the grounding stage (quality/grounding.py).

Grounding tries the typed judgment port (``Judge``) first: sentences are split
deterministically, then claims are selected and verified through ``Judge``.
When no typed judge is available, it falls back to Haiku (``llm_client``):
claims are extracted and adjudicated via two prompted calls per claim. With
neither backend, the stage does not run. KB + repo candidate retrieval is
shared and deterministic across both backends. The KB is a real TF-IDF index
over a tmp directory. No network, no real APIs: every judgment comes from
``FakeJudge``, ``NullJudge``, or a scripted fake ``llm_client``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devrel_origin.core.base import KnowledgeBaseSearch
from devrel_origin.quality.grounding import (
    Claim,
    GroundedClaim,
    GroundingResult,
    Source,
    _coerce_adjudication,
    _coerce_claims,
    _cut_flagged,
    _kb_candidates,
    _repo_facts_to_sources,
    ground_claims,
)
from devrel_origin.quality.judgments import UNAVAILABLE, NullJudge
from tests.quality.fakes import FakeJudge


def _haiku_client(extract_json: str, adjudications: list[str]):
    """LLM mock: first call is extraction, subsequent calls are adjudications."""
    client = MagicMock()
    adj_iter = iter(adjudications)

    async def _generate(*, system_prompt, user_prompt, model, **kwargs):
        if "extract discrete" in system_prompt:
            return extract_json
        if "fact-checker" in system_prompt:
            return next(adj_iter)
        return ""

    client.generate = AsyncMock(side_effect=_generate)
    return client


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
async def test_unavailable_verdict_is_skipped_not_flagged_and_never_cut(tmp_path):
    # A transient per-call backend failure (verdict.available is False) is a
    # third state, distinct from grounded and flagged: the claim lands in
    # `skipped`, and cut_unsourced never removes it, whatever it says.
    kb = _kb(tmp_path)
    text = "The agent auto-instruments applications for OpenTelemetry with zero code changes."
    judge = FakeJudge(relations=[(UNAVAILABLE, 0.0)])

    result = await ground_claims(text=text, kb=kb, judge=judge, cut_unsourced=True)

    assert result.grounded == []
    assert result.flagged == []
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "Judgment skipped."
    assert result.cut_applied is False
    assert result.text_after == text


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
    assert result.backend == "none"


# --- restored from the pre-Task-5 Haiku path (723d270) ----------------------


def test_coerce_claims_parses_json_list():
    raw = (
        '[{"text": "cuts build time 40%", "kind": "metric"}, '
        '{"text": "supports OTel", "kind": "capability"}]'
    )
    claims = _coerce_claims(raw)
    assert len(claims) == 2
    assert claims[0].kind == "metric"
    assert claims[1].text == "supports OTel"


def test_coerce_claims_tolerates_fences_and_junk():
    assert _coerce_claims("```json\nnot json\n```") == []
    assert _coerce_claims("total garbage") == []
    assert _coerce_claims('{"not": "a list"}') == []


def test_coerce_claims_defaults_unknown_kind_to_fact():
    claims = _coerce_claims('[{"text": "x", "kind": "weird"}]')
    assert claims[0].kind == "fact"


def test_coerce_adjudication_grounded_requires_cited_source():
    candidates = _repo_facts_to_sources([{"ref": "commit:abc", "excerpt": "shipped OTel"}])
    # Grounded but no source_indexes -> downgraded to not grounded.
    grounded, picked, _ = _coerce_adjudication(
        '{"grounded": true, "source_indexes": [], "reason": "ok"}', candidates
    )
    assert grounded is False
    assert picked == []


def test_coerce_adjudication_picks_valid_sources():
    candidates = _repo_facts_to_sources(
        [
            {"ref": "commit:abc", "excerpt": "shipped OTel"},
            {"ref": "repo_stats", "excerpt": "100 stars"},
        ]
    )
    grounded, picked, reason = _coerce_adjudication(
        '{"grounded": true, "source_indexes": [1], "reason": "stat matches"}', candidates
    )
    assert grounded is True
    assert len(picked) == 1
    assert picked[0].ref == "repo_stats"
    assert reason == "stat matches"


# --- Task 5b: Haiku fallback -------------------------------------------------


@pytest.mark.asyncio
async def test_haiku_path_runs_when_judge_unavailable(tmp_path):
    kb = _kb(tmp_path)
    extract = (
        '[{"text": "auto-instruments for OpenTelemetry", "kind": "capability"},'
        ' {"text": "used by NASA", "kind": "fact"}]'
    )
    adj = [
        '{"grounded": true, "source_indexes": [0], "reason": "kb states it"}',
        '{"grounded": false, "source_indexes": [], "reason": "no source"}',
    ]
    client = _haiku_client(extract, adj)

    result = await ground_claims(text="draft text", kb=kb, judge=NullJudge(), llm_client=client)

    assert result.backend == "haiku"
    assert result.judged is True
    assert result.total_claims == 2
    assert result.grounded_claims == 1
    assert len(result.flagged) == 1
    assert result.flagged[0].claim.text == "used by NASA"


@pytest.mark.asyncio
async def test_typed_judge_preferred_over_llm_client_when_available(tmp_path):
    kb = _kb(tmp_path)
    judge = FakeJudge(relations=[("supports", 0.9)])
    client = _haiku_client("[]", [])

    result = await ground_claims(
        text="The agent auto-instruments applications for OpenTelemetry with zero code changes.",
        kb=kb,
        judge=judge,
        llm_client=client,
    )

    assert result.backend == "typesafe"
    client.generate.assert_not_called()


@pytest.mark.asyncio
async def test_no_backend_at_all_reports_none(tmp_path):
    kb = _kb(tmp_path)
    result = await ground_claims(text="Hello there world today.", kb=kb, judge=NullJudge())
    assert result.judged is False
    assert result.backend == "none"


@pytest.mark.asyncio
async def test_haiku_unparseable_adjudication_is_skipped_not_flagged_and_still_cut_safe(tmp_path):
    kb = _kb(tmp_path)
    text = "Great tool. Used by NASA. Ships fast."
    extract = '[{"text": "Used by NASA.", "kind": "fact"}]'
    adj = ["not valid json at all"]
    client = _haiku_client(extract, adj)
    # A candidate must exist so `_adjudicate` actually calls the LLM instead
    # of short-circuiting on "no candidate sources".
    repo_facts = [{"ref": "commit:abc", "excerpt": "NASA is a reference customer"}]

    result = await ground_claims(
        text=text,
        kb=kb,
        judge=NullJudge(),
        llm_client=client,
        cut_unsourced=True,
        repo_facts=repo_facts,
    )

    assert result.flagged == []
    assert len(result.skipped) == 1
    assert result.skipped[0].claim.text == "Used by NASA."
    assert result.cut_applied is False
    assert "Used by NASA." in result.text_after
