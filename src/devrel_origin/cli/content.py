"""`devrel content draft|audit` — primary entry points to the editorial pipeline."""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

from devrel_origin.cli._common import _build_llm_client as _build_project_llm_client
from devrel_origin.core.kai import Kai
from devrel_origin.core.llm import LLMClient
from devrel_origin.project.paths import ProjectNotFoundError, ProjectPaths, find_devrel_root
from devrel_origin.quality.editorial import AbortLoud, run_pipeline
from devrel_origin.quality.slop import find_slop, parse_blocklist
from devrel_origin.tools.api_client import PostHogClient
from devrel_origin.tools.search_tools import SearchTools

console = Console()

content_app = typer.Typer(
    name="content",
    help="Generate and audit content through the editorial quality pipeline.",
    no_args_is_help=True,
    add_completion=False,
)


def _build_llm_client(paths: ProjectPaths) -> LLMClient:
    return _build_project_llm_client(paths, console)


def _build_kai(paths: ProjectPaths, llm_client: LLMClient) -> Kai:
    """Wire Kai with the same KB + optional search-tools the weekly cycle uses,
    so `devrel content draft` produces grounded, code-validated output instead
    of an editorial-pipeline pass over a generic LLM draft."""
    posthog = PostHogClient(
        api_key=os.environ.get("POSTHOG_API_KEY", ""),
        project_id=os.environ.get("POSTHOG_PROJECT_ID", ""),
    )
    search_tools: SearchTools | None = None
    if (
        os.environ.get("FIRECRAWL_API_KEY", "").strip()
        or os.environ.get("BRAVE_API_KEY", "").strip()
    ):
        search_tools = SearchTools(
            firecrawl_api_key=os.environ.get("FIRECRAWL_API_KEY", ""),
            brave_api_key=os.environ.get("BRAVE_API_KEY", ""),
        )
    return Kai(
        api_client=posthog,
        knowledge_base_path=paths.kb_dir,
        llm_client=llm_client,
        search_tools=search_tools,
    )


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "draft"


def _write_outputs(paths: ProjectPaths, slug: str, body: str, trace: dict) -> tuple[Path, Path]:
    paths.deliverables_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    body_path = paths.deliverables_dir / f"{ts}-{slug}.md"
    trace_path = paths.deliverables_dir / f"{ts}-{slug}-trace.json"
    body_path.write_text(body)
    trace_path.write_text(json.dumps(trace, indent=2))
    return body_path, trace_path


@content_app.command("draft")
def draft_command(
    prompt: str = typer.Argument(..., help="Topic or instruction for the new content."),
    content_type: str = typer.Option(
        "tutorial",
        "--type",
        help="Content type for targeting (tutorial, blog_post, landing_page, cold_email, battle_card).",
    ),
    timeout_seconds: float = typer.Option(
        600.0,
        "--timeout",
        min=0.1,
        help="Maximum seconds to wait for Kai before exiting with a clear timeout.",
    ),
    editorial_mode: str = typer.Option(
        "fast",
        "--editorial-mode",
        help="Generation path: fast for a bounded first draft, full for the full editorial pipeline.",
    ),
) -> None:
    """Generate new content through Kai: KB-grounded prompt + grounding/code validation."""
    editorial_mode = editorial_mode.strip().lower()
    if editorial_mode == "full":
        editorial_mode = "pipeline"
    if editorial_mode not in {"fast", "pipeline"}:
        console.print("[red]--editorial-mode must be 'fast' or 'full'.[/red]")
        raise typer.Exit(code=1)

    try:
        root = find_devrel_root()
    except ProjectNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    paths = ProjectPaths.from_root(root)
    client = _build_llm_client(paths)
    kai = _build_kai(paths, client)

    async def _do() -> None:
        console.print(
            f"[cyan]Generating with Kai[/cyan] "
            f"({content_type}, {editorial_mode}, timeout {timeout_seconds:g}s)..."
        )

        async def _heartbeat() -> None:
            elapsed = 0
            while True:
                await asyncio.sleep(30)
                elapsed += 30
                console.print(f"[dim]Still generating... {elapsed}s elapsed[/dim]")

        heartbeat = asyncio.create_task(_heartbeat())
        try:
            result = await asyncio.wait_for(
                kai.execute(
                    task=prompt,
                    content_type=content_type,
                    editorial_mode=editorial_mode,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            console.print(
                f"[red]Kai timed out after {timeout_seconds:g}s.[/red] "
                "Try a narrower prompt, add more focused KB evidence, or increase --timeout."
            )
            raise typer.Exit(code=1) from None
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

        status = result.get("status")
        body = result.get("content") or ""
        if status != "generated" or not body:
            console.print(f"[red]Kai did not produce content (status={status}).[/red]")
            for gap in result.get("evidence_gaps", []):
                console.print(f"  - {gap}")
            if result.get("error"):
                console.print(f"  error: {result['error']}")
            raise typer.Exit(code=1)

        trace = {
            "agent": "kai",
            "task": result.get("task"),
            "content_type": content_type,
            "editorial_mode": result.get("editorial_mode", editorial_mode),
            "grounding_sources": result.get("grounding_sources", []),
            "pain_points_addressed": result.get("pain_points_addressed", []),
            "real_issues_referenced": result.get("real_issues_referenced", []),
            "revision": result.get("revision", {}),
            "code_validation": result.get("code_validation", {}),
            "grounding_validation": result.get("grounding_validation", {}),
            "status": status,
        }
        body_path, trace_path = _write_outputs(paths, _slug(prompt), body, trace)
        console.print(f"[green]✓[/green] Wrote {body_path.name} ({len(body)} chars)")
        console.print(f"[green]✓[/green] Wrote {trace_path.name}")

        sources = result.get("grounding_sources") or []
        if sources:
            console.print(f"[green]✓[/green] Grounded in {len(sources)} KB doc(s)")
        else:
            console.print(
                "[yellow]⚠[/yellow] No KB sources matched the prompt; "
                "output may be ungrounded. Run `devrel kb add` to populate the KB."
            )

        cv = result.get("code_validation") or {}
        if cv and not cv.get("all_passed", True):
            console.print(
                f"[yellow]⚠[/yellow] Code validation: "
                f"{cv.get('failed', 0)}/{cv.get('validated', 0)} blocks failed syntax checks"
            )

    asyncio.run(_do())


@content_app.command("slop")
def slop_command(
    file: Path = typer.Argument(..., exists=True, readable=True, help="File to lint for slop."),
) -> None:
    """Run the deterministic regex slop blocklist against a file. Exits
    nonzero if any blocklisted phrase is hit. No LLM calls."""
    try:
        root = find_devrel_root()
    except ProjectNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    paths = ProjectPaths.from_root(root)
    if not paths.slop_file.is_file():
        console.print(f"[red]No slop blocklist at {paths.slop_file}[/red]")
        raise typer.Exit(code=1)
    blocklist = parse_blocklist(paths.slop_file.read_text())
    text = file.read_text()
    hits = find_slop(text, blocklist)
    if not hits:
        console.print(
            f"[green]✓[/green] {file.name}: no slop hits ({len(blocklist)} phrases checked)"
        )
        return
    console.print(f"[red]✗[/red] {file.name}: {len(hits)} slop hit(s)")
    for h in hits[:50]:
        console.print(f"  [yellow]{h.phrase!r}[/yellow] at offset {h.start}")
    raise typer.Exit(code=1)


@content_app.command("audit")
def audit_command(
    file: Path = typer.Argument(..., exists=True, readable=True, help="Existing draft to audit."),
    content_type: str = typer.Option("tutorial", "--type"),
    ground: bool = typer.Option(
        False,
        "--ground/--no-ground",
        help="Run the grounding stage (verify factual claims against KB + repo). "
        "Off by default; enable for high-value artifacts (hero, CTA).",
    ),
    cut_unsourced: bool = typer.Option(
        False,
        "--cut-unsourced",
        help="With --ground, delete unsourced claim sentences instead of only flagging them.",
    ),
) -> None:
    """Run the editorial pipeline against an existing draft file."""
    try:
        root = find_devrel_root()
    except ProjectNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    paths = ProjectPaths.from_root(root)
    client = _build_llm_client(paths)

    async def _do() -> None:
        body = file.read_text()
        repo_facts: list[dict] | None = None
        if ground:
            repo_facts = await _fetch_repo_facts()
        try:
            result = await run_pipeline(
                initial_draft=body,
                content_type=content_type,
                project_paths=paths,
                llm_client=client,
                ground=ground,
                repo_facts=repo_facts,
                cut_unsourced=cut_unsourced,
            )
        except AbortLoud as e:
            console.print(f"[red]Pipeline aborted: {e}[/red]")
            raise typer.Exit(code=1) from None
        # Persist provenance alongside the revision trace so
        # `devrel deliverables provenance` can render it later.
        trace = dict(result.revision_trace)
        if isinstance(result.provenance, dict):
            trace["provenance"] = result.provenance
        body_path, trace_path = _write_outputs(paths, _slug(file.stem), result.final_text, trace)
        console.print(f"[green]✓[/green] Wrote {body_path.name}")
        console.print(f"[green]✓[/green] Wrote {trace_path.name}")
        if ground:
            gs = result.provenance.get("grounding_summary", {})
            console.print(
                f"[cyan]Grounding:[/cyan] {gs.get('grounded_claims', 0)}/"
                f"{gs.get('total_claims', 0)} claims sourced"
            )
        if result.flagged:
            console.print("[yellow]⚠[/yellow] Flagged.")

    asyncio.run(_do())


async def _fetch_repo_facts() -> list[dict] | None:
    """Fetch draft-independent repo facts (commits + stats) for grounding.
    Degrades to None when the GitHub client is unavailable."""
    repo = os.environ.get("GITHUB_REPO", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    try:
        from devrel_origin.tools.github_tools import GitHubTools

        gh = GitHubTools(token=token, repo=repo) if repo else GitHubTools(token=token)
        try:
            return await gh.get_repo_facts()
        finally:
            await gh.close()
    except Exception as exc:  # noqa: BLE001 - grounding degrades, never blocks
        console.print(f"[yellow]⚠[/yellow] Could not fetch repo facts: {exc}")
        return None
