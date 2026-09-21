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


def _seed_all_unpriced_cost(tmp_path):
    """Seed a single unpriced row; no priced calls recorded at all."""
    db_path = tmp_path / ".devrel" / "state.db"
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO costs (agent, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("quality", "typesafe:jev", 1314, 158, 0, 0, 0.0),
        )
        conn.commit()


def _seed_mixed_single_agent_cost(tmp_path):
    """Seed ONE agent with both a priced row and an unpriced row."""
    db_path = tmp_path / ".devrel" / "state.db"
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO costs (agent, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("quality", "claude-sonnet-4-5-20250929", 1000, 500, 0, 0, 0.0105),
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


def test_cost_total_line_all_unpriced_shows_na(tmp_path):
    """Finding 1: the Total line must not read $0.00 when every recorded
    call is on an unpriced model. It follows the same n/a rule as agent
    rows."""
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _init(tmp_path)
        _seed_all_unpriced_cost(tmp_path)
    finally:
        os.chdir(cwd)

    result = _run_in(tmp_path, "cost")
    assert result.exit_code == 0, result.output
    assert "$0.00" not in result.output
    total_line = next(line for line in result.output.splitlines() if line.startswith("Total"))
    assert "n/a" in total_line


def test_cost_json_by_agent_reports_unpriced_calls(tmp_path):
    """FINAL RE-REVIEW part A, Minor: an all-unpriced agent's `usd: 0.0` must
    not read as "free". Every `by_agent` entry carries `unpriced_calls` (0
    when none), so a machine reader can tell "$0 because free" apart from
    "$0 because every call here is unpriced"."""
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _init(tmp_path)
        _seed_mixed_costs(tmp_path)
    finally:
        os.chdir(cwd)

    result = _run_in(tmp_path, "cost", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["by_agent"]["quality"]["usd"] == 0.0
    assert data["by_agent"]["quality"]["unpriced_calls"] == 1
    assert data["by_agent"]["kai"]["unpriced_calls"] == 0


def test_cost_mixed_agent_and_total_show_priced_sum_plus_na(tmp_path):
    """Finding 2: an agent mixing a priced and an unpriced call shows its
    priced sum plus a `+ n/a` marker, and the Total line (which also mixes
    priced and unpriced spend here) carries the same marker."""
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _init(tmp_path)
        priced_cost = _seed_mixed_single_agent_cost(tmp_path)
    finally:
        os.chdir(cwd)

    result = _run_in(tmp_path, "cost")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    total_line = next(line for line in lines if line.startswith("Total"))
    quality_line = next(line for line in lines if "quality" in line)
    marker = f"${priced_cost:.4f} + n/a"
    assert marker in total_line
    assert marker in quality_line
