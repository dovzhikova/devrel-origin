"""`devrel next`: turn synthesized signal into a ranked "do this next" queue.

Roadmap gap B3: close the loop. The most-requested capability is "tell me what
to DO, not just do things." This verb reads Iris's ranked pain themes and turns
them into a transparent, rules-based action queue, emitting the top 3 items as a
weekly deliverable.

Ranking is deterministic and key-free: ``score = reach x intent x effort``, each
factor derived from theme fields (frequency, severity, product areas). No LLM is
required for the ranked report, so the default path is fully testable offline.
With ``--draft`` (and keys set) each top item is attached to a Kai content draft.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from devrel_origin.cli._common import find_paths_or_exit
from devrel_origin.core.iris import FeedbackTheme, Iris
from devrel_origin.core.llm import LLMClient
from devrel_origin.tools.api_client import PostHogClient

logger = logging.getLogger(__name__)

next_app = typer.Typer(
    name="next",
    help='Ranked "do this next" queue from synthesized pain themes (Iris).',
    no_args_is_help=True,
)

_console = Console()

# How many items make the top-of-queue report. Kept small on purpose: the point
# is a focused set of next actions, not a backlog dump.
_TOP_N = 3

# Product areas that tend to gate activation/adoption carry more buyer intent:
# friction here blocks someone who is actively trying to succeed with the tool.
# Scores are on a 1-3 scale (higher = closer to the money).
_INTENT_BY_AREA: dict[str, float] = {
    "onboarding/docs": 3.0,
    "onboarding": 3.0,
    "docs": 3.0,
    "agent sdk": 2.5,
    "mcp tools": 2.5,
    "orchestration": 2.0,
    "knowledge base": 2.0,
    "scoring/eval": 1.5,
    "prompt optimization": 1.5,
    "security": 3.0,
}

# Effort is inverted leverage: cheap-to-fix areas score high so a small change
# with big payoff floats to the top. 1-3 scale (higher = lower effort / faster).
_EFFORT_BY_AREA: dict[str, float] = {
    "onboarding/docs": 3.0,
    "onboarding": 3.0,
    "docs": 3.0,
    "prompt optimization": 2.5,
    "scoring/eval": 2.0,
    "knowledge base": 2.5,
    "mcp tools": 1.5,
    "agent sdk": 1.5,
    "orchestration": 1.0,
    "security": 1.5,
}

# Neutral midpoints for themes whose product areas we can't map. Deliberately
# mid-scale so an unmapped theme neither dominates nor disappears.
_DEFAULT_INTENT = 2.0
_DEFAULT_EFFORT = 2.0


@dataclass
class NextAction:
    """One ranked "do this next" item, with a transparent score breakdown."""

    theme_id: str
    title: str
    action: str
    reach: float  # normalized mention volume (>= 1.0)
    intent: float  # buyer-intent weight from product area (1-3)
    effort: float  # inverse-effort / leverage from product area (1-3)
    score: float  # reach x intent x effort
    frequency: int
    severity: float
    product_areas: list[str]
    evidence: str


def _area_weight(product_areas: list[str], table: dict[str, float], default: float) -> float:
    """Best (max) weight across a theme's product areas, else the default.

    Matching is case-insensitive. Themes touching several areas take the most
    favorable weight: fixing the theme still resolves that area's friction.
    """
    best: float | None = None
    for area in product_areas:
        w = table.get(area.strip().lower())
        if w is not None and (best is None or w > best):
            best = w
    return best if best is not None else default


def rank_actions(themes: list[FeedbackTheme], top_n: int = _TOP_N) -> list[NextAction]:
    """Rank pain themes into next actions by ``reach x intent x effort``.

    Pure and deterministic (no I/O, no LLM), so it is fully unit-testable:

    - reach: frequency (mention volume) as the demand signal, floored at 1.0.
    - intent: how close the affected area is to activation/revenue (1-3).
    - effort: inverse effort, so cheap high-leverage fixes rank up (1-3).

    Ties break by frequency then severity so the ordering is stable.
    """
    actions: list[NextAction] = []
    for theme in themes:
        reach = float(max(theme.frequency, 1))
        intent = _area_weight(theme.product_areas, _INTENT_BY_AREA, _DEFAULT_INTENT)
        effort = _area_weight(theme.product_areas, _EFFORT_BY_AREA, _DEFAULT_EFFORT)
        score = reach * intent * effort
        action_text = (
            theme.recommended_actions[0]
            if theme.recommended_actions
            else f"Address '{theme.title}' (no recommended action captured)"
        )
        actions.append(
            NextAction(
                theme_id=theme.theme_id,
                title=theme.title,
                action=action_text,
                reach=round(reach, 2),
                intent=round(intent, 2),
                effort=round(effort, 2),
                score=round(score, 2),
                frequency=theme.frequency,
                severity=theme.severity,
                product_areas=list(theme.product_areas),
                evidence=f"{theme.frequency} mentions, severity {theme.severity}/10",
            )
        )
    actions.sort(key=lambda a: (a.score, a.frequency, a.severity), reverse=True)
    return actions[: max(0, top_n)]


def _build_iris(paths) -> Iris:
    """Construct Iris with clients from environment variables. Patched in tests."""
    posthog = PostHogClient(
        api_key=os.environ.get("POSTHOG_API_KEY", ""),
        project_id=os.environ.get("POSTHOG_PROJECT_ID", ""),
    )
    llm = LLMClient(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    llm.set_agent("iris")
    return Iris(api_client=posthog, knowledge_base_path=paths.kb_dir, llm_client=llm)


def _load_social_signals(db_path: Path, days: int) -> list[dict]:
    """Read recent social mentions as feedback signals for Iris (deterministic).

    Shaped like Sage's triaged issues (number/title/category) so Iris's chunk
    extractor can ingest them uniformly. Returns [] when the table or DB is
    absent so the verb degrades quietly instead of erroring.
    """
    import sqlite3

    if not db_path.is_file():
        return []
    cutoff = date.today().toordinal() - days
    signals: list[dict] = []
    with sqlite3.connect(db_path) as conn:
        try:
            cur = conn.execute(
                "SELECT id, title, platform, posted_at, content FROM social_mentions "
                "ORDER BY posted_at DESC LIMIT 500"
            )
        except sqlite3.OperationalError:
            return []
        for row_id, title, platform, posted_at, content in cur.fetchall():
            if posted_at:
                try:
                    day = datetime.fromisoformat(posted_at[:10]).date().toordinal()
                    if day < cutoff:
                        continue
                except ValueError:
                    pass
            signals.append(
                {
                    "number": row_id,
                    "title": title or (content or "")[:120],
                    "category": platform or "social",
                }
            )
    return signals


def _render_report(actions: list[NextAction], period_end: str, total_themes: int) -> str:
    """Build the markdown "do this next" deliverable body."""
    lines = [
        f"# Do this next — {period_end}",
        "",
        f"Ranked from {total_themes} synthesized pain theme(s) by "
        "`reach x intent x effort` (transparent, rules-based).",
        "",
    ]
    if not actions:
        lines.append("_No themes to rank yet. Run `devrel synthesize` first._")
        return "\n".join(lines) + "\n"
    for i, a in enumerate(actions, start=1):
        lines.extend(
            [
                f"## {i}. {a.title}  (score {a.score})",
                "",
                f"**Do:** {a.action}",
                "",
                f"- Reach: {a.reach}  x  Intent: {a.intent}  x  Effort: {a.effort}  "
                f"= **{a.score}**",
                f"- Evidence: {a.evidence}",
                f"- Areas: {', '.join(a.product_areas) or 'unclassified'}",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


@next_app.command("report")
def report(
    since: str = typer.Option("7d", "--since", help="Signal window: 7d, 30d, 90d."),
    top: int = typer.Option(_TOP_N, "--top", help="How many actions to surface."),
    format: str = typer.Option("table", "--format", help="table|json"),
    draft: bool = typer.Option(
        False, "--draft", help="Also draft each top action via the Kai pipeline."
    ),
) -> None:
    """Rank synthesized pain themes into the top "do this next" actions."""
    paths = find_paths_or_exit(_console)
    days = int(since.rstrip("d") or "7")

    signals = _load_social_signals(paths.state_db, days)
    iris = _build_iris(paths)

    async def _run() -> list[FeedbackTheme]:
        synthesis = await iris.synthesize_weekly(sage_triage={"issues": signals})
        return synthesis.themes

    themes = asyncio.run(_run())
    actions = rank_actions(themes, top_n=top)
    period_end = date.today().isoformat()

    # Emit the weekly deliverable artifact.
    body = _render_report(actions, period_end, len(themes))
    paths.deliverables_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    body_path = paths.deliverables_dir / f"{ts}-next-actions.md"
    body_path.write_text(body)

    if format == "json":
        _console.print(
            json.dumps(
                {
                    "period_end": period_end,
                    "total_themes": len(themes),
                    "deliverable": body_path.name,
                    "actions": [
                        {
                            "theme_id": a.theme_id,
                            "title": a.title,
                            "action": a.action,
                            "reach": a.reach,
                            "intent": a.intent,
                            "effort": a.effort,
                            "score": a.score,
                            "evidence": a.evidence,
                            "product_areas": a.product_areas,
                        }
                        for a in actions
                    ],
                },
                indent=2,
            )
        )
    else:
        if not actions:
            _console.print("[yellow]No themes to rank yet. Run `devrel synthesize` first.[/yellow]")
        else:
            table = Table(title=f"Do this next: {period_end}")
            table.add_column("#", justify="right")
            table.add_column("Action", style="cyan")
            table.add_column("Score", justify="right")
            table.add_column("R x I x E", justify="right")
            table.add_column("Evidence")
            for i, a in enumerate(actions, start=1):
                table.add_row(
                    str(i),
                    a.action,
                    f"{a.score}",
                    f"{a.reach} x {a.intent} x {a.effort}",
                    a.evidence,
                )
            _console.print(table)
        _console.print(f"[green]Wrote {body_path.name}[/green]")

    if not draft:
        if actions and format != "json":
            _console.print(
                "[dim]Re-run with --draft (and keys set) to draft the top actions via Kai.[/dim]"
            )
        return

    # Closed loop: draft the fix content for each top action via the real pipeline.
    from devrel_origin.cli.content import (
        _build_kai,
        _build_llm_client,
        _slug,
        _write_outputs,
    )

    client = _build_llm_client(paths)
    kai = _build_kai(paths, client)
    for a in actions:
        _console.print(f"[cyan]Drafting for:[/cyan] {a.title}")
        task = (
            f"Write a developer-facing guide that resolves the pain theme "
            f'"{a.title}". Recommended action: {a.action}. Lead with a direct fix, '
            "ground every step in the repo/KB, and include a short FAQ."
        )
        result = asyncio.run(
            kai.execute(task=task, content_type="blog_post", editorial_mode="fast")
        )
        if result.get("status") != "generated" or not result.get("content"):
            _console.print(f"[red]Kai did not produce content (theme {a.theme_id}).[/red]")
            continue
        trace = {"agent": "kai", "next_theme_id": a.theme_id, "next_period": period_end}
        drafted_path, _ = _write_outputs(paths, _slug(task), result["content"], trace)
        _console.print(f"[green]✓[/green] Wrote {drafted_path.name}")
