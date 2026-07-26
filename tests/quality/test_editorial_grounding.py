"""Tests for the optional grounding stage wired into run_pipeline.

Grounding is OFF by default (adds latency/cost). These tests confirm the flag
gates the stage, the stage flags unsourced claims, and provenance is attached.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devrel_origin.project.paths import ProjectPaths
from devrel_origin.quality.editorial import run_pipeline


def _project(tmp_path) -> ProjectPaths:
    d = tmp_path / ".devrel"
    d.mkdir()
    (d / "voice.md").write_text("# Voice\n\nDirect, technical.\n")
    (d / "style.md").write_text("# Style\n\nSentence case headings.\n")
    (d / "slop-blocklist.md").write_text("delve\nfurthermore\n")
    kb = d / "kb" / "docs"
    kb.mkdir(parents=True)
    (kb / "otel.md").write_text(
        "# OpenTelemetry\n\nThe agent auto-instruments apps for OpenTelemetry.\n"
    )
    # A second doc so TF-IDF IDF weights are non-zero (single-doc KB scores 0).
    (kb / "pricing.md").write_text("# Pricing\n\nFree tier includes five seats.\n")
    return ProjectPaths.from_root(tmp_path)


def _client(*, extract_json: str = "[]", adjudications: list[str] | None = None):
    """Mock covering editorial stages, slop lint, persona, and grounding."""
    client = MagicMock()
    client.set_agent = MagicMock()
    client.generate_with_revision = AsyncMock(
        return_value=(
            "Clean revised text with no flagged phrases.",
            MagicMock(final_score=8, revision_rounds=0, critiques=[]),
        )
    )
    adj_iter = iter(adjudications or [])

    async def _generate(*, system_prompt, user_prompt, model, **kwargs):
        if "screening AI-written content" in system_prompt:  # slop lint
            return ""
        if "skeptical senior backend developer" in system_prompt:  # persona
            return '{"score": 8, "weak_sections": [], "feedback": "solid"}'
        if "extract discrete" in system_prompt:  # grounding extract
            return extract_json
        if "fact-checker" in system_prompt:  # grounding adjudicate
            return next(adj_iter)
        return ""

    client.generate = AsyncMock(side_effect=_generate)
    return client


@pytest.mark.asyncio
async def test_grounding_off_by_default(tmp_path):
    paths = _project(tmp_path)
    client = _client()
    result = await run_pipeline(
        initial_draft="x", content_type="tutorial", project_paths=paths, llm_client=client
    )
    stage_names = [s.name for s in result.stages]
    assert "grounding" not in stage_names
    assert result.revision_trace["grounding"] is None
    assert result.provenance["grounding_ran"] is False


@pytest.mark.asyncio
async def test_grounding_on_adds_stage_and_provenance(tmp_path):
    paths = _project(tmp_path)
    # One claim, adjudicated grounded to KB source index 0.
    client = _client(
        extract_json='[{"text": "auto-instruments for OpenTelemetry", "kind": "capability"}]',
        adjudications=['{"grounded": true, "source_indexes": [0], "reason": "kb"}'],
    )
    result = await run_pipeline(
        initial_draft="x",
        content_type="landing_page",
        project_paths=paths,
        llm_client=client,
        ground=True,
    )
    stage_names = [s.name for s in result.stages]
    assert "grounding" in stage_names
    assert result.provenance["grounding_ran"] is True
    assert result.provenance["grounded_ok"] is True
    assert result.provenance["grounding_summary"]["grounded_claims"] == 1


@pytest.mark.asyncio
async def test_unsourced_claim_flags_artifact(tmp_path):
    paths = _project(tmp_path)
    client = _client(
        extract_json='[{"text": "used by NASA", "kind": "fact"}]',
        adjudications=['{"grounded": false, "source_indexes": [], "reason": "no source"}'],
    )
    result = await run_pipeline(
        initial_draft="x",
        content_type="landing_page",
        project_paths=paths,
        llm_client=client,
        ground=True,
    )
    assert result.flagged is True
    assert result.provenance["grounded_ok"] is False
    assert result.provenance["grounding_summary"]["flagged_count"] == 1


@pytest.mark.asyncio
async def test_repo_facts_flow_into_grounding(tmp_path):
    paths = _project(tmp_path)
    client = _client(
        extract_json='[{"text": "we shipped OTel export", "kind": "capability"}]',
        adjudications=['{"grounded": true, "source_indexes": [0], "reason": "commit"}'],
    )
    result = await run_pipeline(
        initial_draft="x",
        content_type="landing_page",
        project_paths=paths,
        llm_client=client,
        ground=True,
        repo_facts=[{"ref": "commit:abc123", "excerpt": "feat: add OTel export"}],
    )
    assert result.provenance["grounding_summary"]["grounded_claims"] == 1
