"""Tests for the 8-stage editorial pipeline orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from devrel_origin.project.paths import ProjectPaths
from devrel_origin.quality.editorial import (
    AbortLoud,
    EditorialResult,
    run_pipeline,
)


def _project(tmp_path) -> ProjectPaths:
    """Build a .devrel/ with voice/style/slop files for the pipeline to read."""
    d = tmp_path / ".devrel"
    d.mkdir()
    (d / "voice.md").write_text("# Voice\n\nDirect, technical.\n")
    (d / "style.md").write_text("# Style\n\nSentence case headings.\n")
    (d / "slop-blocklist.md").write_text("delve\nfurthermore\nin conclusion\n")
    return ProjectPaths.from_root(tmp_path)


def _mock_client_for_clean_run():
    """A mock LLMClient that returns clean text at every stage."""
    client = MagicMock()
    # Editorial stages: generate_with_revision returns (text, trace) tuple.
    client.generate_with_revision = AsyncMock(
        return_value=(
            "Clean revised text without any flagged phrases.",
            MagicMock(final_score=8, revision_rounds=0, critiques=[]),
        )
    )

    # Slop LLM lint: empty (no LLM-detected slop).
    # Persona: high score, no weak sections.
    # Force-rewrite: not called on a clean run.
    async def _generate(*, system_prompt, user_prompt, model, **kwargs):
        if "screening AI-written content" in system_prompt:  # llm_lint
            return ""
        if "skeptical senior backend developer" in system_prompt:  # persona
            return '{"score": 8, "weak_sections": [], "feedback": "solid"}'
        if "rewrite editor" in system_prompt:  # force_rewrite (shouldn't fire)
            return "rewritten"
        return ""

    client.generate = AsyncMock(side_effect=_generate)
    client.set_agent = MagicMock()
    return client


@pytest.mark.asyncio
async def test_clean_run_produces_8_stage_result(tmp_path):
    paths = _project(tmp_path)
    client = _mock_client_for_clean_run()

    result = await run_pipeline(
        initial_draft="A clear sharp opening sentence about the product.",
        content_type="tutorial",
        project_paths=paths,
        llm_client=client,
    )

    assert isinstance(result, EditorialResult)
    assert result.flagged is False
    # 5 stages produce StageResults: developmental, line, copy, slop, persona, readability, audit
    # (8 stages in spec; stage 1 is generate, which is the input here, so we record 7 stages)
    stage_names = [s.name for s in result.stages]
    assert "developmental_edit" in stage_names
    assert "line_edit" in stage_names
    assert "copy_edit" in stage_names
    assert "anti_slop" in stage_names
    assert "persona" in stage_names
    assert "readability" in stage_names
    # Brand audit is run by Sentinel — represented as 'brand_audit' if invoked, else absent.
    # See test below for opt-in audit case.


@pytest.mark.asyncio
async def test_editorial_stages_call_generate_with_revision_with_min_score_7(tmp_path):
    paths = _project(tmp_path)
    client = _mock_client_for_clean_run()
    await run_pipeline(
        initial_draft="x", content_type="tutorial", project_paths=paths, llm_client=client
    )
    # All three editorial passes should use min_score=7, max_rounds=2
    for call in client.generate_with_revision.await_args_list:
        kwargs = call.kwargs
        assert kwargs.get("min_score") == 7
        assert kwargs.get("max_rounds") == 2


@pytest.mark.asyncio
async def test_slop_hit_triggers_force_rewrite(tmp_path):
    paths = _project(tmp_path)
    # Editorial returns text with slop. Force-rewrite returns clean text.
    client = MagicMock()
    client.set_agent = MagicMock()
    client.generate_with_revision = AsyncMock(
        return_value=(
            "This delves into the topic. Furthermore, look at this.",
            MagicMock(final_score=8, revision_rounds=0, critiques=[]),
        )
    )
    rewrite_text = "This explores the topic. Look at this."

    async def _generate(*, system_prompt, user_prompt, model, **kwargs):
        if "screening AI-written content" in system_prompt:
            return ""
        if "skeptical senior backend developer" in system_prompt:
            return '{"score": 8, "weak_sections": [], "feedback": "ok"}'
        if "rewrite editor" in system_prompt:
            return rewrite_text
        return ""

    client.generate = AsyncMock(side_effect=_generate)

    result = await run_pipeline(
        initial_draft="x", content_type="tutorial", project_paths=paths, llm_client=client
    )
    # The final text must be the post-rewrite text, no slop.
    assert "delve" not in result.final_text.lower()
    slop_stage = next(s for s in result.stages if s.name == "anti_slop")
    assert "rewrite_applied" in (slop_stage.detail or "")


@pytest.mark.asyncio
async def test_slop_persists_after_rewrite_aborts_loud(tmp_path):
    paths = _project(tmp_path)
    # Editorial returns text with slop. Rewrite ALSO contains slop.
    client = MagicMock()
    client.set_agent = MagicMock()
    client.generate_with_revision = AsyncMock(
        return_value=(
            "delve and furthermore.",
            MagicMock(final_score=8, revision_rounds=0, critiques=[]),
        )
    )

    async def _generate(*, system_prompt, user_prompt, model, **kwargs):
        if "screening AI-written content" in system_prompt:
            return ""
        if "skeptical senior backend developer" in system_prompt:
            return '{"score": 8, "weak_sections": [], "feedback": "ok"}'
        if "rewrite editor" in system_prompt:
            return "delve still here."  # rewrite still has slop
        return ""

    client.generate = AsyncMock(side_effect=_generate)

    with pytest.raises(AbortLoud) as exc_info:
        await run_pipeline(
            initial_draft="x",
            content_type="tutorial",
            project_paths=paths,
            llm_client=client,
        )
    assert "delve" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_low_persona_score_returns_to_copy_edit_once(tmp_path):
    paths = _project(tmp_path)
    client = MagicMock()
    client.set_agent = MagicMock()
    # Stages 2 (developmental), 3 (line), 4 (copy) — first pass.
    # Then stage 4 fires AGAIN after persona fails. So 4 calls total expected.
    client.generate_with_revision = AsyncMock(
        side_effect=[
            ("v1 dev", MagicMock(final_score=8, revision_rounds=0, critiques=[])),
            ("v1 line", MagicMock(final_score=8, revision_rounds=0, critiques=[])),
            ("v1 copy", MagicMock(final_score=8, revision_rounds=0, critiques=[])),
            ("v2 copy", MagicMock(final_score=8, revision_rounds=0, critiques=[])),
        ]
    )
    persona_calls = iter(
        [
            '{"score": 4, "weak_sections": ["bad intro"], "feedback": "weak"}',
            '{"score": 8, "weak_sections": [], "feedback": "fixed"}',
        ]
    )

    async def _generate(*, system_prompt, user_prompt, model, **kwargs):
        if "screening AI-written content" in system_prompt:
            return ""
        if "skeptical senior backend developer" in system_prompt:
            return next(persona_calls)
        return ""

    client.generate = AsyncMock(side_effect=_generate)

    result = await run_pipeline(
        initial_draft="x", content_type="tutorial", project_paths=paths, llm_client=client
    )
    # First persona pass fails; copy edit re-runs once; second persona passes.
    assert client.generate_with_revision.await_count == 4
    persona_stages = [s for s in result.stages if s.name == "persona"]
    assert len(persona_stages) == 2  # both attempts logged
    assert result.flagged is False  # second persona passed


@pytest.mark.asyncio
async def test_persona_fails_twice_aborts_loud(tmp_path):
    paths = _project(tmp_path)
    client = MagicMock()
    client.set_agent = MagicMock()
    client.generate_with_revision = AsyncMock(
        side_effect=[
            ("v1 dev", MagicMock(final_score=8, revision_rounds=0, critiques=[])),
            ("v1 line", MagicMock(final_score=8, revision_rounds=0, critiques=[])),
            ("v1 copy", MagicMock(final_score=8, revision_rounds=0, critiques=[])),
            ("v2 copy", MagicMock(final_score=8, revision_rounds=0, critiques=[])),
        ]
    )

    async def _generate(*, system_prompt, user_prompt, model, **kwargs):
        if "screening AI-written content" in system_prompt:
            return ""
        if "skeptical senior backend developer" in system_prompt:
            return '{"score": 4, "weak_sections": ["x"], "feedback": "still weak"}'
        return ""

    client.generate = AsyncMock(side_effect=_generate)

    from devrel_origin.quality.editorial import AbortLoud

    with pytest.raises(AbortLoud, match="Persona gate failed after repair"):
        await run_pipeline(
            initial_draft="x",
            content_type="tutorial",
            project_paths=paths,
            llm_client=client,
        )


@pytest.mark.asyncio
async def test_revision_trace_is_serializable(tmp_path):
    paths = _project(tmp_path)
    client = _mock_client_for_clean_run()
    result = await run_pipeline(
        initial_draft="x", content_type="tutorial", project_paths=paths, llm_client=client
    )
    import json

    serialized = json.dumps(result.revision_trace)
    parsed = json.loads(serialized)
    assert "stages" in parsed
    assert "content_type" in parsed


@pytest.mark.asyncio
async def test_run_pipeline_grounding_falls_back_to_haiku_and_names_it_in_the_summary(
    tmp_path, monkeypatch
):
    # No TYPESAFE_API_KEY, so build_judge() returns NullJudge and grounding
    # must fall back to the llm_client Task 5b restores.
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    paths = _project(tmp_path)
    client = _mock_client_for_clean_run()

    async def _generate(*, system_prompt, user_prompt, model, **kwargs):
        if "screening AI-written content" in system_prompt:
            return ""
        if "skeptical senior backend developer" in system_prompt:
            return '{"score": 8, "weak_sections": [], "feedback": "solid"}'
        if "rewrite editor" in system_prompt:
            return "rewritten"
        if "extract discrete" in system_prompt:
            return '[{"text": "It ships a ranked next-action queue.", "kind": "capability"}]'
        if "fact-checker" in system_prompt:
            return '{"grounded": true, "source_indexes": [0], "reason": "commit shows it"}'
        return ""

    client.generate = AsyncMock(side_effect=_generate)

    result = await run_pipeline(
        initial_draft="A clear sharp opening sentence about the product. "
        "It ships a ranked next-action queue.",
        content_type="tutorial",
        project_paths=paths,
        llm_client=client,
        ground=True,
        repo_facts=[{"ref": "d25ca04", "excerpt": "feat: ranked next-action queue"}],
    )

    grounding_dict = result.revision_trace["grounding"]
    assert grounding_dict["backend"] == "haiku"
    grounding_stage = next(s for s in result.stages if s.name == "grounding")
    assert "judged_by=haiku" in grounding_stage.detail

    from devrel_origin.quality.provenance import render_pr_summary

    summary = render_pr_summary(result.provenance)
    assert "judged by haiku" in summary
