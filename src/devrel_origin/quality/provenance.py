"""Provenance trail: a machine-readable record of what the pipeline did.

Every high-value artifact carries a provenance trail answering three questions
an editor (or an auditor) would ask before shipping:

1. Which stages ran, and what did each change?
2. Which factual claims were grounded, and in what sources?
3. Which claims could not be sourced (and were they cut)?

``build_provenance`` produces a JSON-serializable dict attached to the
deliverable. ``render_pr_summary`` turns that dict into a PR-style Markdown
summary (checklist + per-claim citations + a compact stage diff) so a human can
review the guarantee at a glance.

This module is pure: no I/O, no LLM. It reads the ``EditorialResult`` /
``GroundingResult`` dataclasses the pipeline already produced.
"""

from __future__ import annotations

import difflib
from typing import Any


def _stage_changed(before: str, after: str) -> bool:
    return before.strip() != after.strip()


def _short_diff(before: str, after: str, max_lines: int = 12) -> str:
    """A compact unified diff between two stage texts. Empty if unchanged."""
    if not _stage_changed(before, after):
        return ""
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        lineterm="",
        n=1,
    )
    lines = list(diff)[2:]  # drop the '---'/'+++' header pair
    lines = [ln for ln in lines if ln and ln[0] in "+-@"]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... (+{len(lines) - max_lines} more)"]
    return "\n".join(lines)


def build_provenance(
    *,
    content_type: str,
    stages: list[dict[str, Any]],
    grounding: dict[str, Any] | None = None,
    artifact: str = "",
) -> dict[str, Any]:
    """Assemble the machine-readable provenance trail.

    Args:
        content_type: The artifact's content type.
        stages: The ``revision_trace["stages"]`` list (asdict of StageResult).
        grounding: ``GroundingResult.to_dict()`` output, or None if grounding
            did not run for this artifact.
        artifact: Optional artifact name / filename.

    Returns:
        A JSON-serializable dict. Shape is stable so downstream tools (CI gates,
        dashboards) can rely on it.
    """
    stage_records: list[dict[str, Any]] = []
    for s in stages:
        before = s.get("text_before", "")
        after = s.get("text_after", "")
        stage_records.append(
            {
                "name": s.get("name", ""),
                "changed": _stage_changed(before, after),
                "score": s.get("score"),
                "issues": s.get("issues", []),
                "detail": s.get("detail", ""),
                "duration_s": s.get("duration_s"),
            }
        )

    grounded_ok = grounding is not None and grounding.get("flagged_count", 0) == 0

    citations: list[dict[str, Any]] = []
    unsourced: list[dict[str, Any]] = []
    if grounding:
        for gc in grounding.get("grounded", []):
            claim = gc.get("claim", {})
            citations.append(
                {
                    "claim": claim.get("text", ""),
                    "kind": claim.get("kind", ""),
                    "sources": [
                        {"origin": src.get("origin"), "ref": src.get("ref")}
                        for src in gc.get("sources", [])
                    ],
                }
            )
        for gc in grounding.get("flagged", []):
            claim = gc.get("claim", {})
            unsourced.append(
                {
                    "claim": claim.get("text", ""),
                    "kind": claim.get("kind", ""),
                    "reason": gc.get("reason", ""),
                }
            )

    return {
        "artifact": artifact,
        "content_type": content_type,
        "stages": stage_records,
        "grounding_ran": grounding is not None,
        "grounded_ok": grounded_ok,
        "grounding_summary": {
            "total_claims": grounding.get("total_claims", 0) if grounding else 0,
            "grounded_claims": grounding.get("grounded_claims", 0) if grounding else 0,
            "flagged_count": grounding.get("flagged_count", 0) if grounding else 0,
            "cut_applied": grounding.get("cut_applied", False) if grounding else False,
        },
        "citations": citations,
        "unsourced": unsourced,
    }


def render_pr_summary(provenance: dict[str, Any]) -> str:
    """Render a PR-style Markdown summary of the provenance trail."""
    lines: list[str] = []
    artifact = provenance.get("artifact") or "artifact"
    lines.append(f"## Provenance: {artifact}")
    lines.append("")
    lines.append(f"Content type: `{provenance.get('content_type', '')}`")
    lines.append("")

    # Stage checklist.
    lines.append("### Stages")
    for s in provenance.get("stages", []):
        mark = "x" if s.get("changed") else " "
        score = s.get("score")
        score_txt = f" (score {score})" if score is not None else ""
        detail = s.get("detail", "")
        detail_txt = f": {detail}" if detail else ""
        lines.append(f"- [{mark}] `{s.get('name', '')}`{score_txt}{detail_txt}")
    lines.append("")

    # Grounding guarantee.
    if provenance.get("grounding_ran"):
        gs = provenance.get("grounding_summary", {})
        badge = "PASS" if provenance.get("grounded_ok") else "FLAGGED"
        lines.append(f"### Grounding: {badge}")
        lines.append(
            f"{gs.get('grounded_claims', 0)}/{gs.get('total_claims', 0)} claims sourced"
            + (f", {gs.get('flagged_count', 0)} unsourced" if gs.get("flagged_count") else "")
            + (" (cut)" if gs.get("cut_applied") else "")
        )
        lines.append("")

        citations = provenance.get("citations", [])
        if citations:
            lines.append("#### Grounded claims")
            for c in citations:
                refs = ", ".join(
                    f"{src.get('origin')}:{src.get('ref')}" for src in c.get("sources", [])
                )
                lines.append(f"- {c.get('claim', '')}  \n  ↳ {refs}")
            lines.append("")

        unsourced = provenance.get("unsourced", [])
        if unsourced:
            lines.append("#### Unsourced claims (flagged)")
            for u in unsourced:
                reason = u.get("reason", "")
                reason_txt = f" ({reason})" if reason else ""
                lines.append(f"- {u.get('claim', '')}{reason_txt}")
            lines.append("")
    else:
        lines.append("### Grounding: not run")
        lines.append("Enable with `ground=True` for hero / CTA artifacts.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
