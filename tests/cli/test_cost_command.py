"""Tests for `devrel cost`."""

from __future__ import annotations

import json
import os
import sqlite3

from typer.testing import CliRunner

from devrel_origin.cli import app
from devrel_origin.project.state import init_db

runner = CliRunner()


def _run_in(tmp_path, *args, env=None):
    cwd = os.getcwd()
    saved = os.environ.copy()
    try:
        os.chdir(tmp_path)
        if env:
            os.environ.update(env)
        return runner.invoke(app, list(args))
    finally:
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(saved)


def _init(tmp_path):
    runner.invoke(
        app,
        [
            "init",
            "--non-interactive",
            "--name",
            "x",
            "--url",
            "",
            "--github-repo",
            "",
        ],
    )


def _seed_mixed_costs(tmp_path):
    """Seed one priced row (kai, sonnet) and one unpriced row (quality, jev)."""
    db_path = tmp_path / ".devrel" / "state.db"
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO costs (agent, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("kai", "claude-sonnet-4-5-20250929", 1000, 500, 0, 0, 0.0105),
        )
        conn.execute(
            "INSERT INTO costs (agent, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("quality", "typesafe:jev", 1314, 158, 0, 0, 0.0),
        )
        conn.commit()
    return 0.0105


def test_cost_unpriced_agent_shows_na_not_zero(tmp_path):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _init(tmp_path)
        _seed_mixed_costs(tmp_path)
    finally:
        os.chdir(cwd)

    result = _run_in(tmp_path, "cost")
    assert result.exit_code == 0, result.output
    assert "$0.00" not in result.output
    assert "n/a" in result.output


def test_cost_footer_names_unpriced_models(tmp_path):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _init(tmp_path)
        _seed_mixed_costs(tmp_path)
    finally:
        os.chdir(cwd)

    result = _run_in(tmp_path, "cost")
    assert result.exit_code == 0, result.output
    assert "typesafe:jev" in result.output
    assert "cost not published" in result.output


def test_cost_json_unpriced_block(tmp_path):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _init(tmp_path)
        priced_cost = _seed_mixed_costs(tmp_path)
    finally:
        os.chdir(cwd)

    result = _run_in(tmp_path, "cost", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["unpriced"]["typesafe:jev"]["input_tokens"] == 1314
    assert data["unpriced"]["typesafe:jev"]["output_tokens"] == 158
    assert data["unpriced"]["typesafe:jev"]["calls"] == 1
    assert data["total_usd"] == priced_cost
