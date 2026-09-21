"""8-stage editorial pipeline orchestrator.

Stage flow:
  1. Generate (caller's responsibility — initial_draft is the input)
  2. Developmental edit  — generate_with_revision (Sonnet, min_score=7, max_rounds=2)
  3. Line edit           — generate_with_revision (Sonnet, min_score=7, max_rounds=2)
  4. Copy edit           — generate_with_revision (Sonnet, min_score=7, max_rounds=2)
  5. Anti-slop           — regex + LLM lint; on hit, one targeted rewrite;
                            on second failure, AbortLoud
  6. Persona             — Haiku score 1-10 + weak sections
  7. Readability         — pure-Python FRE/sentence-stats/jargon check
  → If 6 or 7 fail: re-run stage 4 once with the failed rubric, then
    re-run 5/6/7 once. Second persona failure aborts loudly.
  8. Brand audit         — Sentinel (caller's responsibility; orchestrator
                            does not invoke Sentinel because it lives in
                            core/sentinel.py and would create a quality→core
                            dependency. The agent that calls run_pipeline
                            invokes Sentinel separately.)

Returns EditorialResult with the final text, every stage's StageResult,
and a JSON-serializable revision_trace.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from devrel_origin.project.paths import ProjectPaths
from devrel_origin.quality.grounding import GroundingResult, ground_claims
from devrel_origin.quality.judgments import Judge, build_judge, with_cost_sink
from devrel_origin.quality.persona import test_against_persona
from devrel_origin.quality.provenance import build_provenance
from devrel_origin.quality.questions import PATTERN_THRESHOLDS
from devrel_origin.quality.readability import check_against_target, compute_readability
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
from devrel_origin.quality.style import get_targets, load_style
from devrel_origin.quality.voice import load_voice

logger = logging.getLogger(__name__)


class AbortLoud(Exception):
    """Raised when the slop pipeline cannot clear flagged phrases after one
    targeted rewrite. Callers should let this propagate; the message lists
    the offending phrases for diagnosis."""


@dataclass
class StageResult:
    name: str
    text_before: str
    text_after: str
    duration_s: float
    score: int | None = None
    issues: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class EditorialResult:
    final_text: str
    stages: list[StageResult]
    flagged: bool
    revision_trace: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)


_DEV_EDIT_SYSTEM = """You are a developmental editor. Improve the draft for:
- structure (does the opening hook? does it close cleanly?)
- argument (is each section earning its place?)
- specificity (is anything generic or hand-wavy?)

Preserve the project voice strictly. Return only the revised content.
"""

_LINE_EDIT_SYSTEM = """You are a line editor. Improve the draft for:
- sentence rhythm (vary length; avoid monotone)
- voice fidelity (match the voice contract precisely)
- word choice (specific, concrete, never vague)

Preserve structure and meaning. Return only the revised content.
"""

_COPY_EDIT_SYSTEM = """You are a copy editor. Improve the draft for:
- grammar, punctuation, agreement
- code blocks (correct syntax, language tags, working examples)
- consistency (capitalization, terminology, tense)

Make minimal changes; preserve voice. Return only the revised content.
"""


def _make_user(text: str, voice: str, style: str, content_type: str, extra: str = "") -> str:
    parts = [
        f"Content type: {content_type}",
        "",
        "Voice contract:",
        voice or "(none yet)",
        "",
        "House style:",
        style or "(none yet)",
        "",
    ]
    if extra:
        parts.extend(["Additional notes:", extra, ""])
    parts.extend(["Draft:", text])
    return "\n".join(parts)


async def _editorial_stage(
    *,
    name: str,
    system: str,
    text_before: str,
    voice: str,
    style: str,
    content_type: str,
    llm_client,
    extra: str = "",
) -> tuple[str, StageResult]:
    t0 = time.monotonic()
    user = _make_user(text_before, voice, style, content_type, extra)
    revised, trace = await llm_client.generate_with_revision(
        system_prompt=system,
        user_prompt=user,
        min_score=7,
        max_rounds=2,
    )
    final_score = getattr(trace, "final_score", None)
    rounds = getattr(trace, "revision_rounds", 0)
    return revised, StageResult(
        name=name,
        text_before=text_before,
        text_after=revised,
        duration_s=round(time.monotonic() - t0, 3),
        score=final_score,
        detail=f"rounds={rounds}",
    )


async def _slop_stage(
    *,
    text_before: str,
    blocklist: list[str],
    voice: str,
    llm_client,
    judge: Judge,
) -> tuple[str, StageResult]:
    """Regex tier plus either the typed pattern judge or llm_lint.

    `judge.available` picks the backend: with one, the stage runs the typed
    per-pattern path (calibrated thresholds, quoted passages). Without one,
    it falls back to the llm_lint path exactly as it behaved before the
    typed judge existed, so a project without the extra sees no regression.

    A judgment call can also fail once a judge is available: `judge_patterns`
    catches its own request errors and returns `available=False` verdicts for
    the affected units (see `judgments_typesafe.py`), which is silent by
    design at that layer. This stage treats that as a third state, distinct
    from "clean" and from "hit," mirroring the grounding stage's "not judged":

    - Every unit unavailable is a total typed-judge failure. That is not
      "zero patterns found"; the stage falls back to the llm_lint path
      exactly as when no judge is available, and `detail` names `llm_lint`
      as what ran.
    - Some units unavailable, none of the judged units flag a real hit: the
      stage never reports "clean" (an unjudged unit is not evidence of
      anything). `detail` states how many units were not judged, and each
      appears in `issues` as `"Not judged: <passage>"`. It also never
      triggers a rewrite on its own: only a real hit (regex or a pattern
      that cleared its threshold) does that. The same rule applies to the
      post-rewrite recheck.
    """
    t0 = time.monotonic()

    async def _llm_check(text: str) -> tuple[list[SlopHit], list[str]]:
        return find_slop(text, blocklist), await llm_lint(text, voice, llm_client)

    async def _typed_check(
        text: str,
    ) -> tuple[list[SlopHit], list[PatternHit], list[int], list[str]]:
        regex_hits = find_slop(text, blocklist)
        units = split_units(text)
        verdicts = await judge.judge_patterns(units=units, voice=voice)
        pattern_hits = find_patterns(text, verdicts, PATTERN_THRESHOLDS)
        unjudged = sorted(v.unit_index for v in verdicts if not v.available)
        return regex_hits, pattern_hits, unjudged, units

    def _all_unjudged(unjudged: list[int], units: list[str]) -> bool:
        return bool(units) and len(unjudged) == len(units)

    def _unjudged_issues(unjudged: list[int], units: list[str]) -> list[str]:
        return [f"Not judged: {units[i][:60]}" for i in unjudged]

    async def _llm_lint_stage(reason: str | None) -> tuple[str, StageResult]:
        """The whole stage on the llm_lint path. `reason` is appended to
        `detail` (e.g. "llm_lint" when a typed judge failed outright);
        `None` reproduces the exact plain "clean"/"rewrite_applied" strings
        from before the typed judge existed, for the no-judge-at-all case."""
        regex_hits, lint_hits = await _llm_check(text_before)
        suffix = f" ({reason})" if reason else ""
        if not regex_hits and not lint_hits:
            return text_before, StageResult(
                name="anti_slop",
                text_before=text_before,
                text_after=text_before,
                duration_s=round(time.monotonic() - t0, 3),
                detail=f"clean{suffix}",
            )
        rewritten = await force_rewrite(text_before, regex_hits, lint_hits, voice, llm_client)
        re_regex, re_lint = await _llm_check(rewritten)
        if re_regex or re_lint:
            offenders = sorted({h.phrase for h in re_regex} | set(re_lint))
            raise AbortLoud("Slop persisted after rewrite: " + ", ".join(offenders))
        return rewritten, StageResult(
            name="anti_slop",
            text_before=text_before,
            text_after=rewritten,
            duration_s=round(time.monotonic() - t0, 3),
            issues=sorted({h.phrase for h in regex_hits} | set(lint_hits)),
            detail=f"rewrite_applied{suffix}",
        )

    if not judge.available:
        return await _llm_lint_stage(None)

    regex_hits, pattern_hits, unjudged, units = await _typed_check(text_before)
    if _all_unjudged(unjudged, units):
        logger.warning(
            "slop_typed_judge_failed_all_units",
            extra={"backend": judge.backend, "unit_count": len(units)},
        )
        return await _llm_lint_stage("llm_lint")

    if not regex_hits and not pattern_hits:
        if unjudged:
            return text_before, StageResult(
                name="anti_slop",
                text_before=text_before,
                text_after=text_before,
                duration_s=round(time.monotonic() - t0, 3),
                issues=_unjudged_issues(unjudged, units),
                detail=(
                    f"{len(unjudged)} of {len(units)} units not judged (judged_by={judge.backend})"
                ),
            )
        return text_before, StageResult(
            name="anti_slop",
            text_before=text_before,
            text_after=text_before,
            duration_s=round(time.monotonic() - t0, 3),
            detail=f"clean (judged_by={judge.backend})",
        )

    rewritten = await force_rewrite(text_before, regex_hits, pattern_hits, voice, llm_client)
    re_regex, re_pattern_hits, re_unjudged, re_units = await _typed_check(rewritten)

    if _all_unjudged(re_unjudged, re_units):
        # The recheck itself cannot be verified via the typed judge; fall
        # back to llm_lint for this one check rather than silently trusting
        # the rewrite is clean.
        logger.warning(
            "slop_typed_judge_failed_all_units_on_recheck",
            extra={"backend": judge.backend, "unit_count": len(re_units)},
        )
        llm_re_regex, llm_re_lint = await _llm_check(rewritten)
        if llm_re_regex or llm_re_lint:
            offenders = sorted({h.phrase for h in llm_re_regex} | set(llm_re_lint))
            raise AbortLoud("Slop persisted after rewrite: " + ", ".join(offenders))
        issues = sorted({h.phrase for h in regex_hits}) + [
            f"{h.pattern}: {h.unit_text[:60]}" for h in pattern_hits
        ]
        return rewritten, StageResult(
            name="anti_slop",
            text_before=text_before,
            text_after=rewritten,
            duration_s=round(time.monotonic() - t0, 3),
            issues=issues,
            detail=(
                f"rewrite_applied (judged_by={judge.backend}, "
                "recheck via llm_lint after judge failure)"
            ),
        )

    if re_regex or re_pattern_hits:
        offenders = sorted({h.phrase for h in re_regex} | {h.pattern for h in re_pattern_hits})
        raise AbortLoud("Slop persisted after rewrite: " + ", ".join(offenders))

    issues = sorted({h.phrase for h in regex_hits}) + [
        f"{h.pattern}: {h.unit_text[:60]}" for h in pattern_hits
    ]
    if re_unjudged:
        issues += _unjudged_issues(re_unjudged, re_units)
        detail = (
            f"rewrite_applied (judged_by={judge.backend}, "
            f"{len(re_unjudged)} of {len(re_units)} units not judged on recheck)"
        )
    else:
        detail = f"rewrite_applied (judged_by={judge.backend})"

    return rewritten, StageResult(
        name="anti_slop",
        text_before=text_before,
        text_after=rewritten,
        duration_s=round(time.monotonic() - t0, 3),
        issues=issues,
        detail=detail,
    )


async def _persona_stage(
    *,
    text: str,
    content_type: str,
    voice: str,
    llm_client,
) -> StageResult:
    t0 = time.monotonic()
    res = await test_against_persona(
        text=text, content_type=content_type, voice=voice, llm_client=llm_client
    )
    issues = []
    if res.score < 7:
        issues.append(f"Persona score {res.score} < 7")
        if res.weak_sections:
            issues.extend(res.weak_sections)
    return StageResult(
        name="persona",
        text_before=text,
        text_after=text,
        duration_s=round(time.monotonic() - t0, 3),
        score=res.score,
        issues=issues,
        detail=res.feedback,
    )


def _readability_stage(*, text: str, content_type: str, style_md: str) -> StageResult:
    t0 = time.monotonic()
    targets = get_targets(content_type, style_md)
    scores = compute_readability(text)
    issues = check_against_target(scores, targets)
    return StageResult(
        name="readability",
        text_before=text,
        text_after=text,
        duration_s=round(time.monotonic() - t0, 3),
        issues=issues,
        detail=f"FRE={scores.flesch_reading_ease}, MSL={scores.mean_sentence_length}",
    )


async def _grounding_stage(
    *,
    text: str,
    project_paths: ProjectPaths,
    judge: Judge,
    llm_client,
    repo_facts: list[dict[str, Any]] | None,
    cut_unsourced: bool,
    confidence_min: float = 0.65,
) -> tuple[str, StageResult, GroundingResult]:
    """Optional stage: verify factual claims against the KB + repo facts.

    Runs only when ``ground=True``. Flags unsourced claims (and cuts them when
    ``cut_unsourced=True``). Never smooths a claim over; an unprovable claim is
    surfaced, not paraphrased away."""
    # Imported lazily: devrel_origin.core.__init__ eagerly loads Atlas, which
    # imports this module, so a top-level core.base import would cycle.
    from devrel_origin.core.base import KnowledgeBaseSearch

    t0 = time.monotonic()
    kb = KnowledgeBaseSearch(project_paths.kb_dir)
    gr = await ground_claims(
        text=text,
        kb=kb,
        judge=judge,
        llm_client=llm_client,
        repo_facts=repo_facts,
        cut_unsourced=cut_unsourced,
        confidence_min=confidence_min,
    )
    if not gr.judged:
        # Not a pass. There is no deterministic equivalent of grounding, so
        # without a backend the stage reports that it did not run.
        return (
            text,
            StageResult(
                name="grounding",
                text_before=text,
                text_after=text,
                duration_s=round(time.monotonic() - t0, 3),
                detail="skipped: no judgment backend",
            ),
            gr,
        )
    # A skipped claim (verdict unavailable) is not the same finding as an
    # unsourced one; keep them in separate issue lines so a reader (or a CI
    # gate parsing issues) never mistakes "not judged" for "checked and bad."
    issues = [f"Unsourced: {c.claim.text}" for c in gr.flagged]
    issues += [f"Not judged: {c.claim.text}" for c in gr.skipped]
    sr = StageResult(
        name="grounding",
        text_before=text,
        text_after=gr.text_after,
        duration_s=round(time.monotonic() - t0, 3),
        issues=issues,
        detail=(
            f"{gr.grounded_claims}/{gr.total_claims} grounded"
            + (", cut" if gr.cut_applied else "")
            + (f", {len(gr.skipped)} not judged" if gr.skipped else "")
            + f", judged_by={gr.backend}"
        ),
    )
    return gr.text_after, sr, gr


async def run_pipeline(
    *,
    initial_draft: str,
    content_type: str,
    project_paths: ProjectPaths,
    llm_client,
    ground: bool = False,
    repo_facts: list[dict[str, Any]] | None = None,
    cut_unsourced: bool = False,
) -> EditorialResult:
    """Run the 8-stage editorial pipeline. See module docstring.

    Args:
        ground: When True, run the optional grounding stage after readability.
            Defaults to False because grounding adds latency and cost; enable it
            for high-value artifacts (hero, CTA, landing pages).
        repo_facts: Pre-fetched repo facts (commits / stats) as dicts with
            ``ref`` and ``excerpt`` keys, used as grounding sources. Only
            consulted when ``ground=True``.
        cut_unsourced: When True (and ``ground=True``), delete unsourced claim
            sentences from the final text. When False, they are only flagged.
    """
    voice = load_voice(project_paths)
    style_md = load_style(project_paths)
    blocklist = parse_blocklist(
        project_paths.slop_file.read_text(encoding="utf-8")
        if project_paths.slop_file.is_file()
        else ""
    )
    judge = build_judge()
    if project_paths.state_db.is_file():
        # Imports core.llm (via project.cost_sink), which imports this module
        # through core/kai.py, so a top-level import here would cycle.
        from devrel_origin.project.cost_sink import make_sqlite_sink

        judge = with_cost_sink(judge, make_sqlite_sink(project_paths.state_db))

    # Fail-fast on unknown content_type before any LLM spend.
    get_targets(content_type, style_md)

    stages: list[StageResult] = []

    # Stages 2-4: editorial loops.
    text, sr = await _editorial_stage(
        name="developmental_edit",
        system=_DEV_EDIT_SYSTEM,
        text_before=initial_draft,
        voice=voice,
        style=style_md,
        content_type=content_type,
        llm_client=llm_client,
    )
    stages.append(sr)

    text, sr = await _editorial_stage(
        name="line_edit",
        system=_LINE_EDIT_SYSTEM,
        text_before=text,
        voice=voice,
        style=style_md,
        content_type=content_type,
        llm_client=llm_client,
    )
    stages.append(sr)

    text, sr = await _editorial_stage(
        name="copy_edit",
        system=_COPY_EDIT_SYSTEM,
        text_before=text,
        voice=voice,
        style=style_md,
        content_type=content_type,
        llm_client=llm_client,
    )
    stages.append(sr)

    # Stage 5: anti-slop. May raise AbortLoud — let it propagate.
    text, sr = await _slop_stage(
        text_before=text,
        blocklist=blocklist,
        voice=voice,
        llm_client=llm_client,
        judge=judge,
    )
    stages.append(sr)

    # Stage 6: persona.
    persona_sr = await _persona_stage(
        text=text,
        content_type=content_type,
        voice=voice,
        llm_client=llm_client,
    )
    stages.append(persona_sr)

    # Stage 7: readability.
    readability_sr = _readability_stage(text=text, content_type=content_type, style_md=style_md)
    stages.append(readability_sr)

    # Re-loop into copy-edit if either soft gate failed.
    flagged = False
    if persona_sr.issues or readability_sr.issues:
        extra = "Previous persona feedback: " + (persona_sr.detail or "")
        if readability_sr.issues:
            extra += "\nReadability issues: " + "; ".join(readability_sr.issues)
        text, sr = await _editorial_stage(
            name="copy_edit",
            system=_COPY_EDIT_SYSTEM,
            text_before=text,
            voice=voice,
            style=style_md,
            content_type=content_type,
            llm_client=llm_client,
            extra=extra,
        )
        stages.append(sr)

        # Re-run anti-slop, persona, readability one more time.
        text, sr = await _slop_stage(
            text_before=text,
            blocklist=blocklist,
            voice=voice,
            llm_client=llm_client,
            judge=judge,
        )
        stages.append(sr)

        persona2 = await _persona_stage(
            text=text,
            content_type=content_type,
            voice=voice,
            llm_client=llm_client,
        )
        stages.append(persona2)

        readability2 = _readability_stage(text=text, content_type=content_type, style_md=style_md)
        stages.append(readability2)

        # Readability re-runs are informational only because short test/mock
        # text often fails MSL. Persona is the hard ship/no-ship gate.
        if persona2.issues:
            issue_text = "; ".join(persona2.issues)
            logger.error(
                "editorial pipeline aborting for content_type=%s after persona repair failed: %s",
                content_type,
                issue_text,
            )
            raise AbortLoud(f"Persona gate failed after repair for {content_type}: {issue_text}")

    # Optional grounding stage (Stage 5b): claims → sources, unsourced flagged.
    grounding_dict: dict[str, Any] | None = None
    if ground:
        text, grounding_sr, grounding_result = await _grounding_stage(
            text=text,
            project_paths=project_paths,
            judge=judge,
            llm_client=llm_client,
            repo_facts=repo_facts,
            cut_unsourced=cut_unsourced,
        )
        stages.append(grounding_sr)
        grounding_dict = grounding_result.to_dict()
        # An unsourced claim that survives (not cut) makes the artifact flagged.
        if grounding_result.flagged and not grounding_result.cut_applied:
            flagged = True

    revision_trace = {
        "content_type": content_type,
        "voice_present": bool(voice),
        "style_present": bool(style_md),
        "blocklist_size": len(blocklist),
        "stages": [asdict(s) for s in stages],
        "flagged": flagged,
        "grounding": grounding_dict,
    }

    provenance = build_provenance(
        content_type=content_type,
        stages=revision_trace["stages"],
        grounding=grounding_dict,
    )

    return EditorialResult(
        final_text=text,
        stages=stages,
        flagged=flagged,
        revision_trace=revision_trace,
        provenance=provenance,
    )
