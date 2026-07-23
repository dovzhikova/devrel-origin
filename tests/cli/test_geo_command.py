"""CLI smoke tests for `devrel geo ...`."""

import sqlite3

from typer.testing import CliRunner

from devrel_origin.cli import app


def test_geo_help_lists_subcommands():
    runner = CliRunner()
    result = runner.invoke(app, ["geo", "--help"])
    assert result.exit_code == 0
    for verb in ("report", "history", "diff", "fix"):
        assert verb in result.output.lower()


def test_geo_report_help_runs():
    runner = CliRunner()
    result = runner.invoke(app, ["geo", "report", "--help"])
    assert result.exit_code == 0
    assert "prompts-file" in result.output.lower()


def test_geo_report_persists_geo_visibility_rows(tmp_path, monkeypatch):
    """report verb writes one geo_visibility row per configured (prompt x engine)."""
    from devrel_origin.cli import geo as geo_module
    from devrel_origin.project import state
    from devrel_origin.tools.citation_probe import CitationResult

    monkeypatch.chdir(tmp_path)
    devrel_dir = tmp_path / ".devrel"
    devrel_dir.mkdir()
    (devrel_dir / "config.toml").write_text(
        'product_name = "Origin"\nproduct_url = "https://useorigin.co"\n'
    )
    db_path = devrel_dir / "state.db"
    state.init_db(db_path)

    prompts_file = tmp_path / "prompts.txt"
    prompts_file.write_text("best devtool marketing tool\n")

    class _FakeProbe:
        async def probe(self, prompts):
            pid = prompts[0][0]
            return [
                CitationResult(
                    prompt_id=pid,
                    engine="chatgpt",
                    is_mentioned=True,
                    mention_type="cited",
                    position_score=1,
                    citation_share=0.5,
                ),
                CitationResult(prompt_id=pid, engine="perplexity", is_mentioned=False),
                # unconfigured engine must be skipped, not persisted
                CitationResult(prompt_id=pid, engine="google_aio", configured=False),
            ]

    monkeypatch.setattr(geo_module, "_build_probe", lambda paths: _FakeProbe())

    runner = CliRunner()
    result = runner.invoke(app, ["geo", "report", "--prompts-file", str(prompts_file)])
    assert result.exit_code == 0, f"CLI failed: {result.output!r}"

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT engine, is_mentioned, mention_type FROM geo_visibility"
        ).fetchall()

    engines = {r[0]: (r[1], r[2]) for r in rows}
    # google_aio was unconfigured -> skipped
    assert set(engines) == {"chatgpt", "perplexity"}
    assert engines["chatgpt"] == (1, "cited")
    assert engines["perplexity"][0] == 0
