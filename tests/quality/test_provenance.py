"""Tests for the provenance trail builder + PR-style renderer."""

from __future__ import annotations

import json

from devrel_origin.quality.provenance import build_provenance, render_pr_summary

_STAGES = [
    {
        "name": "developmental_edit",
        "text_before": "old text here",
        "text_after": "new text here",
        "score": 8,
        "issues": [],
        "detail": "rounds=1",
        "duration_s": 0.2,
    },
    {
        "name": "anti_slop",
        "text_before": "new text here",
        "text_after": "new text here",
        "score": None,
        "issues": [],
        "detail": "clean",
        "duration_s": 0.1,
    },
]

_GROUNDING = {
    "total_claims": 2,
    "grounded_claims": 1,
    "flagged_count": 1,
    "cut_applied": False,
    "grounded": [
        {
            "claim": {"text": "supports OTel", "kind": "capability"},
            "grounded": True,
            "sources": [{"origin": "kb", "ref": "docs/otel.md", "excerpt": "e"}],
            "reason": "kb states it",
        }
    ],
    "flagged": [
        {
            "claim": {"text": "used by NASA", "kind": "fact"},
            "grounded": False,
            "sources": [],
            "reason": "no source",
        }
    ],
}


def test_build_provenance_stage_change_detection():
    prov = build_provenance(content_type="landing_page", stages=_STAGES, grounding=None)
    assert prov["stages"][0]["changed"] is True  # dev edit changed the text
    assert prov["stages"][1]["changed"] is False  # anti-slop was a no-op
    assert prov["grounding_ran"] is False


def test_build_provenance_grounding_ok_flag():
    ok = build_provenance(
        content_type="hero",
        stages=_STAGES,
        grounding={**_GROUNDING, "flagged_count": 0, "flagged": []},
    )
    assert ok["grounded_ok"] is True

    flagged = build_provenance(content_type="hero", stages=_STAGES, grounding=_GROUNDING)
    assert flagged["grounded_ok"] is False
    assert len(flagged["citations"]) == 1
    assert len(flagged["unsourced"]) == 1
    assert flagged["citations"][0]["sources"][0]["ref"] == "docs/otel.md"


def test_build_provenance_is_json_serializable():
    prov = build_provenance(content_type="hero", stages=_STAGES, grounding=_GROUNDING)
    json.dumps(prov)  # must not raise


def test_render_pr_summary_contains_stage_checklist_and_citations():
    prov = build_provenance(
        content_type="hero", stages=_STAGES, grounding=_GROUNDING, artifact="hero-v1"
    )
    md = render_pr_summary(prov)
    assert "## Provenance: hero-v1" in md
    assert "- [x] `developmental_edit`" in md  # changed stage checked
    assert "- [ ] `anti_slop`" in md  # unchanged stage unchecked
    assert "Grounding: FLAGGED" in md
    assert "kb:docs/otel.md" in md
    assert "used by NASA" in md  # flagged claim surfaced


def test_render_pr_summary_grounding_not_run():
    prov = build_provenance(content_type="tutorial", stages=_STAGES, grounding=None)
    md = render_pr_summary(prov)
    assert "Grounding: not run" in md
    assert "ground=True" in md


def test_render_pr_summary_pass_badge():
    prov = build_provenance(
        content_type="hero",
        stages=_STAGES,
        grounding={**_GROUNDING, "flagged_count": 0, "flagged": []},
    )
    md = render_pr_summary(prov)
    assert "Grounding: PASS" in md
