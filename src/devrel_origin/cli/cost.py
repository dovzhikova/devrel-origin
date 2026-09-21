"""`devrel cost` — read the costs ledger from .devrel/state.db.

Reads the SQLite `costs` table populated by the LLM cost sink (Phase 4
Task 1). Reports total spend in USD, plus a per-agent breakdown. No
ANTHROPIC_API_KEY required — this only reads local state.
"""

from __future__ import annotations

import json

import typer
from rich.console import Console

from devrel_origin.cli._common import find_paths_or_exit
from devrel_origin.project.cost_sink import is_priced
from devrel_origin.project.state import open_db

console = Console()


def cost_command(
    month: str = typer.Option(
        "",
        "--month",
        help="Filter to a YYYY-MM slice (e.g., '2026-04').",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show recorded LLM cost totals from the project state DB."""
    paths = find_paths_or_exit(console)
    if not paths.state_db.is_file():
        console.print("[yellow]No state.db yet. Run an agent first.[/yellow]")
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "total_usd": 0.0,
                        "by_agent": {},
                        "calls": 0,
                        "month_filter": month or None,
                    }
                )
            )
        return

    # Build a parameterised WHERE clause. The `where` literal is one of two
    # fixed strings we control — user input flows only through `params`,
    # so SQL injection is impossible.
    where = ""
    params: tuple = ()
    if month:
        where = "WHERE recorded_at LIKE ?"
        params = (f"{month}%",)

    with open_db(paths.state_db) as conn:
        total_row = conn.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0.0) AS total, COUNT(*) AS calls FROM costs {where}",
            params,
        ).fetchone()
        total_usd = float(total_row["total"]) if total_row else 0.0
        calls = int(total_row["calls"]) if total_row else 0

        by_agent_rows = conn.execute(
            f"SELECT agent, "
            f"COALESCE(SUM(cost_usd), 0.0) AS usd, "
            f"COALESCE(SUM(input_tokens), 0) AS in_tok, "
            f"COALESCE(SUM(output_tokens), 0) AS out_tok, "
            f"COUNT(*) AS calls "
            f"FROM costs {where} GROUP BY agent ORDER BY usd DESC",
            params,
        ).fetchall()

        by_model_rows = conn.execute(
            f"SELECT agent, model, "
            f"COALESCE(SUM(input_tokens), 0) AS in_tok, "
            f"COALESCE(SUM(output_tokens), 0) AS out_tok, "
            f"COUNT(*) AS calls "
            f"FROM costs {where} GROUP BY agent, model",
            params,
        ).fetchall()

    by_agent = {
        r["agent"]: {
            "usd": float(r["usd"]),
            "input_tokens": int(r["in_tok"]),
            "output_tokens": int(r["out_tok"]),
            "calls": int(r["calls"]),
        }
        for r in by_agent_rows
    }

    # An agent's calls may mix priced and unpriced models. Track, per agent,
    # whether it has any priced and any unpriced calls, and separately
    # aggregate unpriced spend per model so it can be surfaced instead of
    # silently rendering as `$0.00`.
    agent_priced_flags: dict[str, dict[str, bool]] = {}
    unpriced_by_model: dict[str, dict[str, int]] = {}
    for r in by_model_rows:
        agent = r["agent"]
        model = r["model"]
        flags = agent_priced_flags.setdefault(agent, {"has_priced": False, "has_unpriced": False})
        if is_priced(model):
            flags["has_priced"] = True
        else:
            flags["has_unpriced"] = True
            model_stats = unpriced_by_model.setdefault(
                model, {"calls": 0, "input_tokens": 0, "output_tokens": 0}
            )
            model_stats["calls"] += int(r["calls"])
            model_stats["input_tokens"] += int(r["in_tok"])
            model_stats["output_tokens"] += int(r["out_tok"])

    if json_output:
        payload = {
            "total_usd": total_usd,
            "calls": calls,
            "by_agent": by_agent,
            "month_filter": month or None,
        }
        if unpriced_by_model:
            payload["unpriced"] = unpriced_by_model
        typer.echo(json.dumps(payload, indent=2))
        return

    suffix = f" for {month}" if month else ""
    console.print(f"[bold]Total{suffix}:[/bold] ${total_usd:.4f}  [dim]({calls} call(s))[/dim]")
    if not by_agent:
        console.print("[dim]No cost rows yet.[/dim]")
        return
    console.print("\n[bold]By agent:[/bold]")
    for agent, row in by_agent.items():
        flags = agent_priced_flags.get(agent, {"has_priced": True, "has_unpriced": False})
        if flags["has_unpriced"] and not flags["has_priced"]:
            cost_display = "n/a"
        elif flags["has_unpriced"]:
            cost_display = f"${row['usd']:.4f} + n/a"
        else:
            cost_display = f"${row['usd']:.4f}"
        console.print(
            f"  {agent:>10s}  {cost_display}  "
            f"[dim]in={row['input_tokens']} out={row['output_tokens']} "
            f"calls={row['calls']}[/dim]"
        )

    if unpriced_by_model:
        total_unpriced_calls = sum(m["calls"] for m in unpriced_by_model.values())
        total_unpriced_in = sum(m["input_tokens"] for m in unpriced_by_model.values())
        total_unpriced_out = sum(m["output_tokens"] for m in unpriced_by_model.values())
        model_names = ", ".join(sorted(unpriced_by_model))
        console.print(
            f"\n[dim]{total_unpriced_calls} calls on unpriced models ({model_names}): "
            f"{total_unpriced_in:,} in / {total_unpriced_out:,} out, "
            f"cost not published[/dim]",
            soft_wrap=True,
        )
