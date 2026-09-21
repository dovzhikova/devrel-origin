"""Tests for the optional grounding stage wired into run_pipeline.

Grounding is OFF by default (adds latency/cost). These tests confirm the flag
gates the stage, the stage flags unsourced claims, and provenance is attached.
The judgment backend is provided by monkeypatching ``build_judge`` (called
once inside ``run_pipeline``) with a ``FakeJudge``, so no network call is made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devrel_origin.project.paths import ProjectPaths
from devrel_origin.quality import editorial
from devrel_origin.quality.editorial import run_pipeline
from devrel_origin.quality.judgments import UNAVAILABLE
from tests.quality.fakes import FakeJudge


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


def _client():
    """LLM mock covering editorial stages, slop lint, and persona (not grounding:
    grounding now goes through the judge port, patched separately per test)."""
    client = MagicMock()
    client.set_agent = MagicMock()
    client.generate_with_revision = AsyncMock(
        return_value=(
            "Clean revised text with no flagged phrases.",
            MagicMock(final_score=8, revision_rounds=0, critiques=[]),
        )
    )

    async def _generate(*, system_prompt, user_prompt, model, **kwargs):
        if "screening AI-written content" in system_prompt:  # slop lint
            return ""
        if "skeptical senior backend developer" in system_prompt:  # persona
            return '{"score": 8, "weak_sections": [], "feedback": "solid"}'
        return ""

    client.generate = AsyncMock(side_effect=_generate)
    return client


@pytest.mark.asyncio
async def test_grounding_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(editorial, "build_judge", lambda: FakeJudge())
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
async def test_grounding_on_adds_stage_and_provenance(tmp_path, monkeypatch):
    # One claim (the pipeline's fixed final text), verified as grounded
    # against a repo fact (repo facts are candidates for every claim,
    # independent of KB overlap).
    judge = FakeJudge(relations=[("supports", 0.9)])
    monkeypatch.setattr(editorial, "build_judge", lambda: judge)
    paths = _project(tmp_path)
    client = _client()
    result = await run_pipeline(
        initial_draft="x",
        content_type="landing_page",
        project_paths=paths,
        llm_client=client,
        ground=True,
        repo_facts=[{"ref": "commit:abc123", "excerpt": "feat: add OTel export"}],
    )
    stage_names = [s.name for s in result.stages]
    assert "grounding" in stage_names
    assert result.provenance["grounding_ran"] is True
    assert result.provenance["grounded_ok"] is True
    assert result.provenance["grounding_summary"]["grounded_claims"] == 1


@pytest.mark.asyncio
async def test_unsourced_claim_flags_artifact(tmp_path, monkeypatch):
    # No candidate sources at all: flagged without consuming a relation.
    judge = FakeJudge()
    monkeypatch.setattr(editorial, "build_judge", lambda: judge)
    paths = _project(tmp_path)
    client = _client()
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
async def test_repo_facts_flow_into_grounding(tmp_path, monkeypatch):
    judge = FakeJudge(relations=[("supports", 0.9)])
    monkeypatch.setattr(editorial, "build_judge", lambda: judge)
    paths = _project(tmp_path)
    client = _client()
    result = await run_pipeline(
        initial_draft="x",
        content_type="landing_page",
        project_paths=paths,
        llm_client=client,
        ground=True,
        repo_facts=[{"ref": "commit:abc123", "excerpt": "feat: add OTel export"}],
    )
    assert result.provenance["grounding_summary"]["grounded_claims"] == 1


@pytest.mark.asyncio
async def test_grounding_skipped_without_a_judge_backend(tmp_path, monkeypatch):
    from devrel_origin.quality.judgments import NullJudge

    monkeypatch.setattr(editorial, "build_judge", lambda: NullJudge())
    paths = _project(tmp_path)
    client = _client()
    result = await run_pipeline(
        initial_draft="x",
        content_type="landing_page",
        project_paths=paths,
        llm_client=client,
        ground=True,
    )
    grounding_stage = next(s for s in result.stages if s.name == "grounding")
    assert grounding_stage.detail == "skipped: no judgment backend"
    assert result.flagged is False


@pytest.mark.asyncio
async def test_per_claim_skip_is_not_judged_never_cut_never_unsourced(tmp_path, monkeypatch):
    # A per-call unavailable verdict (transient backend failure) is distinct
    # from a stage-level skip: the stage runs (judged=True) but this one claim
    # is not judged. It must never render as "Unsourced" and must survive
    # cut_unsourced.
    judge = FakeJudge(relations=[(UNAVAILABLE, 0.0)])
    monkeypatch.setattr(editorial, "build_judge", lambda: judge)
    paths = _project(tmp_path)
    client = _client()
    result = await run_pipeline(
        initial_draft="x",
        content_type="landing_page",
        project_paths=paths,
        llm_client=client,
        ground=True,
        cut_unsourced=True,
        repo_facts=[{"ref": "commit:abc123", "excerpt": "feat: add OTel export"}],
    )
    grounding_stage = next(s for s in result.stages if s.name == "grounding")
    assert any(i.startswith("Not judged:") for i in grounding_stage.issues)
    assert not any(i.startswith("Unsourced:") for i in grounding_stage.issues)
    assert result.provenance["grounding_summary"]["skipped_count"] == 1
    assert result.provenance["grounded_ok"] is False
    # The claim (the pipeline's fixed final text) must survive despite
    # cut_unsourced=True: a skip is not evidence the claim is wrong.
    assert "Clean revised text with no flagged phrases." in result.final_text
