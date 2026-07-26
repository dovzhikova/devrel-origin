"""`devrel deliverables {list, show}` — browse generated artifacts.

Lists / cats files under .devrel/deliverables/ — the canonical output
directory used by `devrel content draft|audit` and the agent pipeline.
"""

from __future__ import annotations

import json

import typer
from rich.console import Console

from devrel_origin.cli._common import find_paths_or_exit
from devrel_origin.quality.provenance import build_provenance, render_pr_summary

console = Console()

deliverables_app = typer.Typer(
    name="deliverables",
    help="List and inspect generated content/artifacts under .devrel/deliverables/.",
    no_args_is_help=True,
    add_completion=False,
)


@deliverables_app.command("list")
def list_files() -> None:
    """List all deliverable files (newest first)."""
    paths = find_paths_or_exit(console)
    if not paths.deliverables_dir.exists():
        console.print("[yellow]No deliverables directory yet.[/yellow]")
        return
    files = sorted(
        paths.deliverables_dir.rglob("*"),
        key=lambda p: p.stat().st_mtime if p.is_file() else 0,
        reverse=True,
    )
    files = [p for p in files if p.is_file()]
    if not files:
        console.print("[yellow]No deliverables yet.[/yellow]")
        return
    for p in files:
        rel = p.relative_to(paths.deliverables_dir)
        size = p.stat().st_size
        console.print(f"  [dim]{size:>7d}[/dim]  {rel}")
    console.print(f"\n[green]{len(files)} file(s)[/green]")


@deliverables_app.command("show")
def show(
    name: str = typer.Argument(..., help="Filename (or substring) to display."),
) -> None:
    """Print the contents of a deliverable file (substring match on name)."""
    paths = find_paths_or_exit(console)
    if not paths.deliverables_dir.exists():
        console.print("[yellow]No deliverables directory yet.[/yellow]")
        raise typer.Exit(code=1)
    matches = [p for p in paths.deliverables_dir.rglob("*") if p.is_file() and name in p.name]
    if not matches:
        console.print(f"[red]No deliverable matching '{name}'[/red]")
        raise typer.Exit(code=1)
    if len(matches) > 1:
        console.print(f"[yellow]Multiple matches for '{name}':[/yellow]")
        for p in matches:
            console.print(f"  {p.relative_to(paths.deliverables_dir)}")
        raise typer.Exit(code=1)
    typer.echo(matches[0].read_text())


@deliverables_app.command("provenance")
def provenance(
    name: str = typer.Argument(..., help="Trace filename (or substring) to render."),
) -> None:
    """Render a deliverable's provenance trail as a PR-style summary.

    Reads a ``*-trace.json`` file (written by `devrel content draft|audit`) and
    prints which stages ran, what each changed, and the source each grounded
    claim cites."""
    paths = find_paths_or_exit(console)
    if not paths.deliverables_dir.exists():
        console.print("[yellow]No deliverables directory yet.[/yellow]")
        raise typer.Exit(code=1)
    matches = [
        p
        for p in paths.deliverables_dir.rglob("*")
        if p.is_file() and name in p.name and p.suffix == ".json"
    ]
    if not matches:
        console.print(f"[red]No trace JSON matching '{name}'[/red]")
        raise typer.Exit(code=1)
    if len(matches) > 1:
        console.print(f"[yellow]Multiple trace matches for '{name}':[/yellow]")
        for p in matches:
            console.print(f"  {p.relative_to(paths.deliverables_dir)}")
        raise typer.Exit(code=1)

    trace_path = matches[0]
    try:
        trace = json.loads(trace_path.read_text())
    except json.JSONDecodeError:
        console.print(f"[red]{trace_path.name} is not valid JSON[/red]")
        raise typer.Exit(code=1) from None

    # A pipeline trace may already carry a prebuilt provenance block; otherwise
    # rebuild it from the stages + grounding recorded in the trace.
    prov = trace.get("provenance")
    if not prov:
        prov = build_provenance(
            content_type=trace.get("content_type", ""),
            stages=trace.get("stages", []),
            grounding=trace.get("grounding"),
            artifact=trace_path.stem,
        )
    typer.echo(render_pr_summary(prov))
