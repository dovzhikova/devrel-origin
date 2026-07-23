"""`devrel geo ...`: answer-engine visibility verbs (GEO / LLM-citation).

Tracks whether ChatGPT / Perplexity / Google AI Overviews mention or cite this
project, per engine, over time — persisted to the ``geo_visibility`` table.

Roadmap gap B1. `report` probes a prompt set and records rows; `history` and
`diff` read them back; `fix` closes the loop — it briefs the content/schema
change for each un-cited prompt and, with --draft, drafts it via the Kai pipeline.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.table import Table

from devrel_origin.cli._common import find_paths_or_exit
from devrel_origin.tools.citation_probe import ENGINES, CitationProbe
from devrel_origin.tools.geo_audit import run_geo_audit

geo_app = typer.Typer(
    name="geo",
    help="Answer-engine visibility (GEO). Per-engine LLM citation tracking + fix.",
    no_args_is_help=True,
)

_console = Console()


def _load_brand(paths) -> tuple[str, list[str], str | None]:
    """Read product identity (name, aliases, domain) from ``.devrel/config.toml``."""
    cfg_path = paths.devrel_dir / "config.toml"
    name, aliases, url = "", [], None
    if cfg_path.is_file():
        data = tomllib.loads(cfg_path.read_text())
        name = data.get("product_name", "") or ""
        aliases = list(data.get("product_aliases", []) or [])
        url = data.get("product_url") or None
    domain = urlparse(url).netloc.lower() if url else None
    if domain and domain.startswith("www."):
        domain = domain[4:]
    return name, aliases, domain


def _build_probe(paths) -> CitationProbe:
    """Construct a CitationProbe from config + environment keys. Patched in tests."""
    name, aliases, domain = _load_brand(paths)
    return CitationProbe(
        brand=name,
        aliases=aliases,
        domain=domain,
        openai_key=os.environ.get("OPENAI_API_KEY", ""),
        perplexity_key=os.environ.get("PERPLEXITY_API_KEY", ""),
        dfs_auth=os.environ.get("DFS_BASIC", ""),
        dfs_base=os.environ.get("DFS_BASE", ""),
        openai_model=os.environ.get("GEO_OPENAI_MODEL", "gpt-4o"),
        perplexity_model=os.environ.get("GEO_PERPLEXITY_MODEL", "sonar"),
    )


def _read_prompts(prompts_file: Path) -> list[tuple[str, str]]:
    """One prompt per line; optional ``id<TAB>prompt``, else auto ids ``p1..``."""
    prompts: list[tuple[str, str]] = []
    for i, line in enumerate(prompts_file.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            pid, text = line.split("\t", 1)
            prompts.append((pid.strip(), text.strip()))
        else:
            prompts.append((f"p{i}", line))
    return prompts


@geo_app.command("report")
def report(
    prompts_file: Path = typer.Option(
        ..., "--prompts-file", "-p", help="File of probe prompts (one per line)."
    ),
    format: str = typer.Option("table", "--format", help="table|json"),
) -> None:
    """Probe answer engines for the prompt set and persist geo_visibility rows."""
    paths = find_paths_or_exit(_console)
    if not prompts_file.is_file():
        _console.print(f"[red]Prompts file not found: {prompts_file}[/red]")
        raise typer.Exit(code=1)
    prompts = _read_prompts(prompts_file)
    if not prompts:
        _console.print("[yellow]No prompts found in file.[/yellow]")
        raise typer.Exit(code=0)

    period_end = date.today().isoformat()
    probe = _build_probe(paths)
    results = asyncio.run(probe.probe(prompts))

    persisted = 0
    skipped = 0
    with sqlite3.connect(paths.state_db) as conn:
        for r in results:
            if not r.configured:
                skipped += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO geo_visibility "
                "(prompt_id, engine, period_end, is_mentioned, mention_type, "
                " position_score, citation_share, quality_score, response_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r.prompt_id,
                    r.engine,
                    period_end,
                    1 if r.is_mentioned else 0,
                    r.mention_type,
                    r.position_score,
                    r.citation_share,
                    r.quality_score,
                    r.response_path,
                ),
            )
            persisted += 1
        conn.commit()

    if format == "json":
        _console.print(
            json.dumps(
                {
                    "period_end": period_end,
                    "persisted": persisted,
                    "skipped_unconfigured": skipped,
                    "results": [
                        {
                            "prompt_id": r.prompt_id,
                            "engine": r.engine,
                            "is_mentioned": r.is_mentioned,
                            "mention_type": r.mention_type,
                            "position_score": r.position_score,
                            "citation_share": r.citation_share,
                            "configured": r.configured,
                        }
                        for r in results
                    ],
                },
                indent=2,
            )
        )
        return

    table = Table(title=f"GEO visibility: {period_end}")
    table.add_column("Prompt", style="cyan")
    for eng in ENGINES:
        table.add_column(eng, justify="center")
    by_prompt: dict[str, dict[str, str]] = {}
    for r in results:
        cell = "—" if not r.configured else ("✓" if r.is_mentioned else "·")
        by_prompt.setdefault(r.prompt_id, {})[r.engine] = cell
    for pid, cells in by_prompt.items():
        table.add_row(pid, *[cells.get(e, "?") for e in ENGINES])
    _console.print(table)
    _console.print(
        f"[green]Persisted {persisted} result(s)[/green]"
        + (f"; [yellow]{skipped} skipped (engine key not set)[/yellow]" if skipped else "")
    )


@geo_app.command("history")
def history(
    prompt_id: str = typer.Argument(..., help="Prompt id to track over time."),
    engine: str = typer.Option("", "--engine", help="Filter to one engine."),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Show citation trajectory for a prompt across periods."""
    paths = find_paths_or_exit(_console)
    if not paths.state_db.is_file():
        _console.print("[yellow]No state.db yet, run `devrel geo report` first.[/yellow]")
        raise typer.Exit(code=0)

    table = Table(title=f"GEO history: {prompt_id}")
    table.add_column("Period", style="cyan")
    table.add_column("Engine")
    table.add_column("Cited", justify="center")
    table.add_column("Pos", justify="right")
    table.add_column("Share", justify="right")

    query = (
        "SELECT period_end, engine, is_mentioned, position_score, citation_share "
        "FROM geo_visibility WHERE prompt_id = ?"
    )
    params: list[object] = [prompt_id]
    if engine:
        query += " AND engine = ?"
        params.append(engine)
    query += " ORDER BY period_end DESC LIMIT ?"
    params.append(limit)

    with sqlite3.connect(paths.state_db) as conn:
        for period_end, eng, mentioned, pos, share in conn.execute(query, params):
            table.add_row(
                period_end,
                eng,
                "✓" if mentioned else "·",
                "" if pos is None else str(pos),
                "" if share is None else f"{share:.0%}",
            )
    _console.print(table)


@geo_app.command("diff")
def diff(
    period_a: str = typer.Argument(..., help="Earlier ISO period (YYYY-MM-DD)."),
    period_b: str = typer.Argument(..., help="Later ISO period (YYYY-MM-DD)."),
) -> None:
    """Per-(prompt, engine) citation change between two periods."""
    paths = find_paths_or_exit(_console)
    if not paths.state_db.is_file():
        _console.print("[yellow]No state.db yet.[/yellow]")
        raise typer.Exit(code=0)

    table = Table(title=f"GEO diff: {period_a} -> {period_b}")
    table.add_column("Prompt", style="cyan")
    table.add_column("Engine")
    table.add_column(period_a, justify="center")
    table.add_column(period_b, justify="center")
    table.add_column("Change", justify="center")

    with sqlite3.connect(paths.state_db) as conn:
        cur = conn.execute(
            "SELECT a.prompt_id, a.engine, a.is_mentioned, b.is_mentioned "
            "FROM geo_visibility a JOIN geo_visibility b "
            "  ON a.prompt_id = b.prompt_id AND a.engine = b.engine "
            "WHERE a.period_end = ? AND b.period_end = ? "
            "ORDER BY a.prompt_id, a.engine",
            (period_a, period_b),
        )
        for prompt_id, engine, was, now in cur:
            if now and not was:
                change = "[green]gained[/green]"
            elif was and not now:
                change = "[red]lost[/red]"
            else:
                change = "—"
            table.add_row(prompt_id, engine, "✓" if was else "·", "✓" if now else "·", change)
    _console.print(table)


# Per-engine fix strategy, grounded in how each engine actually cites.
_ENGINE_ANGLE = {
    "chatgpt": (
        "ChatGPT favors authoritative, recent owned sources — publish or refresh a "
        "canonical page that answers this directly, with a dated update."
    ),
    "perplexity": (
        "Perplexity leans on community sources (Reddit/HN) — seed an authentic "
        "community answer and ensure a citable owned page exists."
    ),
    "google_aio": (
        "Google AI Overviews pull structured, well-linked pages — add clear H2/H3 "
        "question headings, FAQ/HowTo schema, and internal links."
    ),
}


@dataclass
class GeoFixBrief:
    """One un-cited prompt and how to win the citation."""

    prompt_id: str
    prompt_text: str
    missing_engines: list[str]
    angles: list[str]
    task: str


def _fix_task(prompt_text: str) -> str:
    return (
        f'Write a canonical answer page for the query "{prompt_text}" that a devtool '
        "buyer would search. Lead with a direct 2-3 sentence answer, then specifics; "
        "ground every claim in the repo/KB; include an FAQ block."
    )


def _latest_period(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(period_end) FROM geo_visibility").fetchone()
    return row[0] if row else None


@geo_app.command("fix")
def fix(
    period: str = typer.Option("", "--period", help="Period to fix (default: latest)."),
    prompts_file: Path | None = typer.Option(
        None, "--prompts-file", "-p", help="Map prompt ids -> text for richer tasks."
    ),
    draft: bool = typer.Option(
        False, "--draft", help="Also draft the fix content via the Kai pipeline."
    ),
    top: int = typer.Option(3, "--top", help="How many gaps to draft with --draft."),
) -> None:
    """For each prompt not cited last run, brief the fix (and optionally draft it)."""
    paths = find_paths_or_exit(_console)
    if not paths.state_db.is_file():
        _console.print("[yellow]No state.db yet, run `devrel geo report` first.[/yellow]")
        raise typer.Exit(code=0)

    text_by_id = (
        dict(_read_prompts(prompts_file)) if prompts_file and prompts_file.is_file() else {}
    )

    with sqlite3.connect(paths.state_db) as conn:
        period = period or _latest_period(conn)
        if not period:
            _console.print("[yellow]No geo_visibility rows yet.[/yellow]")
            raise typer.Exit(code=0)
        rows = conn.execute(
            "SELECT prompt_id, engine, is_mentioned FROM geo_visibility "
            "WHERE period_end = ? ORDER BY prompt_id, engine",
            (period,),
        ).fetchall()

    missing: dict[str, list[str]] = {}
    for prompt_id, engine, mentioned in rows:
        if not mentioned:
            missing.setdefault(prompt_id, []).append(engine)

    if not missing:
        _console.print(f"[green]Cited on every engine probed for {period}. 🎉[/green]")
        raise typer.Exit(code=0)

    briefs = [
        GeoFixBrief(
            prompt_id=pid,
            prompt_text=text_by_id.get(pid, pid),
            missing_engines=engines,
            angles=[_ENGINE_ANGLE[e] for e in engines if e in _ENGINE_ANGLE],
            task=_fix_task(text_by_id.get(pid, pid)),
        )
        # most-missing first: prompts absent on more engines are higher leverage
        for pid, engines in sorted(missing.items(), key=lambda kv: -len(kv[1]))
    ]

    table = Table(title=f"GEO fix plan: {period} ({len(briefs)} gap(s))")
    table.add_column("Prompt", style="cyan")
    table.add_column("Missing on")
    table.add_column("Do this")
    for b in briefs:
        table.add_row(b.prompt_text, ", ".join(b.missing_engines), b.angles[0] if b.angles else "")
    _console.print(table)

    if not draft:
        _console.print(
            "[dim]Re-run with --draft (and keys set) to draft the top fixes via Kai.[/dim]"
        )
        return

    # Closed loop: draft the fix content for the top-N gaps via the real pipeline.
    from devrel_origin.cli.content import (
        _build_kai,
        _build_llm_client,
        _slug,
        _write_outputs,
    )

    client = _build_llm_client(paths)
    kai = _build_kai(paths, client)
    for b in briefs[: max(0, top)]:
        _console.print(f"[cyan]Drafting fix for:[/cyan] {b.prompt_text}")
        result = asyncio.run(
            kai.execute(task=b.task, content_type="blog_post", editorial_mode="fast")
        )
        if result.get("status") != "generated" or not result.get("content"):
            _console.print(f"[red]Kai did not produce content (prompt {b.prompt_id}).[/red]")
            continue
        trace = {"agent": "kai", "geo_prompt_id": b.prompt_id, "geo_period": period}
        body_path, _ = _write_outputs(paths, _slug(b.task), result["content"], trace)
        _console.print(f"[green]✓[/green] Wrote {body_path.name}")


_STATUS_STYLE = {"pass": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}


@geo_app.command("audit")
def audit(
    url: str = typer.Argument(..., help="Site URL to audit (e.g. https://example.com)."),
    format: str = typer.Option("table", "--format", help="table|json"),
) -> None:
    """Free GEO-hygiene check: AI crawler access, llms.txt, schema, sitemap. No auth."""
    report = asyncio.run(run_geo_audit(url))

    if format == "json":
        _console.print(
            json.dumps(
                {
                    "url": report.url,
                    "score": report.score,
                    "checks": [
                        {"key": c.key, "status": c.status, "detail": c.detail, "fix": c.fix}
                        for c in report.checks
                    ],
                },
                indent=2,
            )
        )
        return

    table = Table(title=f"GEO hygiene: {report.url}  —  score {report.score}/100")
    table.add_column("", justify="center")
    table.add_column("Check", style="cyan")
    table.add_column("Detail")
    for c in report.checks:
        table.add_row(_STATUS_STYLE.get(c.status, "?"), c.label, c.detail)
    _console.print(table)
    fixes = [c for c in report.checks if c.status != "pass" and c.fix]
    if fixes:
        _console.print("\n[bold]Fixes:[/bold]")
        for c in fixes:
            _console.print(f"  • [cyan]{c.label}[/cyan]: {c.fix}")
