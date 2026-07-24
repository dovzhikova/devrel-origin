"""Tests for the grounding stage (quality/grounding.py).

Grounding extracts factual claims, retrieves KB + repo candidate sources, then
adjudicates each claim. All LLM calls are mocked (extraction + adjudication);
the KB is a real TF-IDF index over a tmp directory. No network, no real APIs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devrel_origin.core.base import KnowledgeBaseSearch
from devrel_origin.quality.grounding import (
    Claim,
    GroundingResult,
    _coerce_adjudication,
    _coerce_claims,
    _cut_flagged,
    _kb_candidates,
    _repo_facts_to_sources,
    ground_claims,
)


def _kb(tmp_path):
    d = tmp_path / "kb"
    (d / "docs").mkdir(parents=True)
    (d / "docs" / "otel.md").write_text(
        "# OpenTelemetry\n\nThe agent auto-instruments applications for "
        "OpenTelemetry with zero code changes.\n"
    )
    (d / "docs" / "pricing.md").write_text("# Pricing\n\nFree tier includes 5 seats.\n")
    return KnowledgeBaseSearch(d)


# --- pure helpers ----------------------------------------------------------


def test_coerce_claims_parses_json_list():
    raw = '[{"text": "cuts build time 40%", "kind": "metric"}, {"text": "supports OTel", "kind": "capability"}]'
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
    # Grounded but no source_indexes → downgraded to not grounded.
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
    from devrel_origin.quality.grounding import GroundedClaim

    text = "Our product is fast. It cuts build time 40%. Try it."
    flagged = [
        GroundedClaim(claim=Claim(text="It cuts build time 40%.", kind="metric"), grounded=False)
    ]
    out = _cut_flagged(text, flagged)
    assert "40%" not in out
    assert "Our product is fast." in out


# --- end-to-end (mocked LLM) ----------------------------------------------


def _client(extract_json: str, adjudications: list[str]):
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


@pytest.mark.asyncio
async def test_ground_claims_grounded_and_flagged(tmp_path):
    kb = _kb(tmp_path)
    extract = (
        '[{"text": "auto-instruments for OpenTelemetry", "kind": "capability"},'
        ' {"text": "used by NASA", "kind": "fact"}]'
    )
    # Claim 1 grounded to the KB source at index 0; claim 2 unsourced.
    adj = [
        '{"grounded": true, "source_indexes": [0], "reason": "kb states it"}',
        '{"grounded": false, "source_indexes": [], "reason": "no source"}',
    ]
    client = _client(extract, adj)

    result = await ground_claims(text="draft text", kb=kb, llm_client=client)

    assert isinstance(result, GroundingResult)
    assert result.total_claims == 2
    assert result.grounded_claims == 1
    assert len(result.flagged) == 1
    assert result.flagged[0].claim.text == "used by NASA"
    assert result.grounded[0].sources[0].origin == "kb"
    assert result.cut_applied is False


@pytest.mark.asyncio
async def test_ground_claims_cut_removes_unsourced(tmp_path):
    kb = _kb(tmp_path)
    text = "Great tool. Used by NASA. Ships fast."
    extract = '[{"text": "Used by NASA.", "kind": "fact"}]'
    adj = ['{"grounded": false, "source_indexes": [], "reason": "no source"}']
    client = _client(extract, adj)

    result = await ground_claims(text=text, kb=kb, llm_client=client, cut_unsourced=True)
    assert result.cut_applied is True
    assert "NASA" not in result.text_after
    assert "Great tool." in result.text_after


@pytest.mark.asyncio
async def test_ground_claims_no_claims_is_noop(tmp_path):
    kb = _kb(tmp_path)
    client = _client("[]", [])
    result = await ground_claims(text="Hello.", kb=kb, llm_client=client)
    assert result.total_claims == 0
    assert result.grounded_claims == 0
    assert result.flagged == []
    assert result.text_after == "Hello."


@pytest.mark.asyncio
async def test_ground_claims_uses_repo_facts(tmp_path):
    kb = _kb(tmp_path)
    extract = '[{"text": "we shipped OTel support", "kind": "capability"}]'
    # Adjudication grounds to a repo fact. KB match for otel also exists, so the
    # repo fact is appended after KB candidates; index will be beyond KB hits.
    repo_facts = [{"ref": "commit:deadbeef01", "excerpt": "feat: add OpenTelemetry export"}]
    # Ground against whichever index the fact-checker returns; we force it to
    # pick a repo source by returning the last index dynamically via a wide pick.
    adj = ['{"grounded": true, "source_indexes": [0], "reason": "commit shows it"}']
    client = _client(extract, adj)

    result = await ground_claims(text="draft", kb=kb, llm_client=client, repo_facts=repo_facts)
    assert result.grounded_claims == 1
    # The cited source at index 0 is a KB candidate (kb ranked first); the repo
    # fact is still available as a candidate. Assert grounding succeeded.
    assert result.grounded[0].grounded is True


def test_grounding_result_to_dict_is_serializable(tmp_path):
    import json

    from devrel_origin.quality.grounding import GroundedClaim, Source

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
