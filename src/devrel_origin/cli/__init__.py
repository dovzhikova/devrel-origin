"""Typer CLI app for devrel-origin.

Phase 2 registers `init` and `doctor`. Later phases register additional
verb groups (run, content, sales, marketing, etc.).
"""

from __future__ import annotations

import typer

from devrel_origin import __version__
from devrel_origin.cli.analytics import analytics_app
from devrel_origin.cli.argus import argus_app
from devrel_origin.cli.auth import auth_command
from devrel_origin.cli.config import config_app
from devrel_origin.cli.content import content_app
from devrel_origin.cli.cost import cost_command
from devrel_origin.cli.cro import cro_app
from devrel_origin.cli.deliverables import deliverables_app
from devrel_origin.cli.docs import docs_app
from devrel_origin.cli.doctor import doctor_command
from devrel_origin.cli.experiment import experiment_command
from devrel_origin.cli.geo import geo_app
from devrel_origin.cli.growth import growth_app
from devrel_origin.cli.init import init_command
from devrel_origin.cli.intel import intel_command
from devrel_origin.cli.kb import kb_app
from devrel_origin.cli.listen import listen_command
from devrel_origin.cli.marketing import marketing_app
from devrel_origin.cli.migrate import migrate_command
from devrel_origin.cli.run import run_command
from devrel_origin.cli.sales import sales_app
from devrel_origin.cli.schedule import schedule_app
from devrel_origin.cli.synthesize import synthesize_command
from devrel_origin.cli.triage import triage_command
from devrel_origin.cli.video import video_app

app = typer.Typer(
    name="devrel",
    help="DevRel + Sales + Marketing agent system. Run from inside a project repo.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"devrel-origin {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Root callback. Subcommands are registered below."""
    return None


app.command(name="init")(init_command)
app.command(name="auth")(auth_command)
app.command(name="doctor")(doctor_command)
app.command(name="migrate")(migrate_command)
app.add_typer(content_app, name="content")
app.add_typer(cro_app, name="cro")
app.add_typer(geo_app, name="geo")
app.command(name="run")(run_command)
app.command(name="triage")(triage_command)
app.command(name="listen")(listen_command)
app.command(name="synthesize")(synthesize_command)
app.command(name="experiment")(experiment_command)
app.command(name="intel")(intel_command)
app.add_typer(sales_app, name="sales")
app.add_typer(marketing_app, name="marketing")
app.add_typer(kb_app, name="kb")
app.add_typer(schedule_app, name="schedule")
app.command(name="cost")(cost_command)
app.add_typer(deliverables_app, name="deliverables")
app.add_typer(config_app, name="config")
app.add_typer(docs_app, name="docs")
app.add_typer(video_app, name="video")
app.add_typer(argus_app, name="argus")
app.add_typer(growth_app, name="growth")
app.add_typer(analytics_app, name="analytics")


if __name__ == "__main__":
    app()
