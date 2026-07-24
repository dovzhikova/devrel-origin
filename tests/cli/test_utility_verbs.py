"""Tests for utility verbs: cost, deliverables, config get/set, content slop."""

from __future__ import annotations

import json
import os
import sqlite3
import tomllib

import pytest
from typer.testing import CliRunner

from devrel_origin.cli import app

runner = CliRunner()


def _init(tmp_path):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        runner.invoke(
            app,
            ["init", "--non-interactive", "--name", "x", "--url", "", "--github-repo", ""],
        )
    finally:
        os.chdir(cwd)


def _run(tmp_path, args):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, args)
    finally:
        os.chdir(cwd)


def _seed_costs(tmp_path, rows):
    db = tmp_path / ".devrel" / "state.db"
    with sqlite3.connect(db) as conn:
        for r in rows:
            conn.execute(
                "INSERT INTO costs (agent, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, 0, 0, ?)",
                (
                    r["agent"],
                    r.get("model", "claude-sonnet-4-6"),
                    r.get("input", 100),
                    r.get("output", 50),
                    r["usd"],
                ),
            )
        conn.commit()


# ---- cost ------------------------------------------------------------


def test_cost_empty_db_outputs_zero(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, ["cost"])
    assert r.exit_code == 0, r.output
    assert "$0.0000" in r.output


def test_cost_aggregates_by_agent(tmp_path):
    _init(tmp_path)
    _seed_costs(
        tmp_path,
        [
            {"agent": "kai", "usd": 0.12},
            {"agent": "kai", "usd": 0.08},
            {"agent": "mox", "usd": 0.50},
        ],
    )
    r = _run(tmp_path, ["cost"])
    assert r.exit_code == 0, r.output
    assert "$0.7000" in r.output  # total
    assert "kai" in r.output
    assert "mox" in r.output
    assert "$0.5000" in r.output  # mox row sorts first (highest)


def test_cost_json_output(tmp_path):
    _init(tmp_path)
    _seed_costs(
        tmp_path,
        [
            {"agent": "kai", "usd": 0.25},
            {"agent": "rex", "usd": 0.15},
        ],
    )
    r = _run(tmp_path, ["cost", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["total_usd"] == pytest.approx(0.40)
    assert data["calls"] == 2
    assert "kai" in data["by_agent"]
    assert data["by_agent"]["kai"]["usd"] == pytest.approx(0.25)


def test_cost_no_api_key_required(tmp_path, monkeypatch):
    """`devrel cost` reads local state, must not require ANTHROPIC_API_KEY."""
    _init(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = _run(tmp_path, ["cost"])
    assert r.exit_code == 0, r.output


def test_cost_month_filter(tmp_path):
    _init(tmp_path)
    db = tmp_path / ".devrel" / "state.db"
    with sqlite3.connect(db) as conn:
        # Two rows: one in 2026-04, one in 2026-05.
        conn.execute(
            "INSERT INTO costs (agent, model, input_tokens, output_tokens, cost_usd, recorded_at) "
            "VALUES ('kai', 'm', 100, 50, 0.01, '2026-04-15 10:00:00')"
        )
        conn.execute(
            "INSERT INTO costs (agent, model, input_tokens, output_tokens, cost_usd, recorded_at) "
            "VALUES ('mox', 'm', 200, 100, 0.02, '2026-05-15 10:00:00')"
        )
        conn.commit()

    # Without filter: both rows.
    r_all = _run(tmp_path, ["cost", "--json"])
    assert r_all.exit_code == 0
    data_all = json.loads(r_all.output)
    assert data_all["calls"] == 2

    # April only.
    r_apr = _run(tmp_path, ["cost", "--month", "2026-04", "--json"])
    assert r_apr.exit_code == 0
    data_apr = json.loads(r_apr.output)
    assert data_apr["calls"] == 1
    assert "kai" in data_apr["by_agent"]
    assert "mox" not in data_apr["by_agent"]

    # May only.
    r_may = _run(tmp_path, ["cost", "--month", "2026-05", "--json"])
    assert r_may.exit_code == 0
    data_may = json.loads(r_may.output)
    assert data_may["calls"] == 1
    assert "mox" in data_may["by_agent"]


# ---- deliverables ----------------------------------------------------


def test_deliverables_list_empty(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, ["deliverables", "list"])
    # Either "no deliverables yet" or empty dir handling
    assert r.exit_code == 0, r.output
    assert "No deliverables" in r.output or "0 file" in r.output


def test_deliverables_list_shows_files(tmp_path):
    _init(tmp_path)
    d = tmp_path / ".devrel" / "deliverables"
    d.mkdir(parents=True, exist_ok=True)
    (d / "post.md").write_text("hello")
    (d / "trace.json").write_text("{}")
    r = _run(tmp_path, ["deliverables", "list"])
    assert r.exit_code == 0, r.output
    assert "post.md" in r.output
    assert "trace.json" in r.output
    assert "2 file" in r.output


def test_deliverables_show_substring_match(tmp_path):
    _init(tmp_path)
    d = tmp_path / ".devrel" / "deliverables"
    d.mkdir(parents=True, exist_ok=True)
    (d / "20260429-post.md").write_text("BODY-CONTENT")
    r = _run(tmp_path, ["deliverables", "show", "post"])
    assert r.exit_code == 0, r.output
    assert "BODY-CONTENT" in r.output


def test_deliverables_show_missing(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, ["deliverables", "show", "nope"])
    assert r.exit_code == 1, r.output


def test_deliverables_list_when_dir_absent(tmp_path):
    """`deliverables list` exits 0 with friendly notice when dir doesn't exist."""
    _init(tmp_path)
    d = tmp_path / ".devrel" / "deliverables"
    if d.exists():
        for child in d.rglob("*"):
            if child.is_file():
                child.unlink()
        d.rmdir()
    r = _run(tmp_path, ["deliverables", "list"])
    assert r.exit_code == 0, r.output
    assert "No deliverables directory" in r.output


def test_deliverables_show_when_dir_absent(tmp_path):
    """`deliverables show` exits 1 when deliverables dir is missing."""
    _init(tmp_path)
    d = tmp_path / ".devrel" / "deliverables"
    if d.exists():
        for child in d.rglob("*"):
            if child.is_file():
                child.unlink()
        d.rmdir()
    r = _run(tmp_path, ["deliverables", "show", "anything"])
    assert r.exit_code == 1, r.output
    assert "No deliverables directory" in r.output


def test_deliverables_show_multiple_matches(tmp_path):
    """Substring matching multiple files exits 1 and lists candidates."""
    _init(tmp_path)
    d = tmp_path / ".devrel" / "deliverables"
    d.mkdir(parents=True, exist_ok=True)
    (d / "alpha-post.md").write_text("A")
    (d / "beta-post.md").write_text("B")
    r = _run(tmp_path, ["deliverables", "show", "post"])
    assert r.exit_code == 1, r.output
    assert "Multiple matches" in r.output
    assert "alpha-post.md" in r.output
    assert "beta-post.md" in r.output


# ---- deliverables provenance -----------------------------------------


def _seed_trace(tmp_path, name, trace):
    import json

    d = tmp_path / ".devrel" / "deliverables"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(trace))


def test_deliverables_provenance_from_stages(tmp_path):
    """Renders a PR-style summary from a trace's stages when no prebuilt
    provenance block is present."""
    _init(tmp_path)
    _seed_trace(
        tmp_path,
        "20260101-hero-trace.json",
        {
            "content_type": "hero",
            "stages": [
                {"name": "copy_edit", "text_before": "a", "text_after": "b", "score": 8},
            ],
            "grounding": None,
        },
    )
    r = _run(tmp_path, ["deliverables", "provenance", "hero"])
    assert r.exit_code == 0, r.output
    assert "Provenance" in r.output
    assert "copy_edit" in r.output
    assert "Grounding: not run" in r.output


def test_deliverables_provenance_uses_prebuilt_block(tmp_path):
    _init(tmp_path)
    _seed_trace(
        tmp_path,
        "20260101-cta-trace.json",
        {
            "content_type": "cta",
            "provenance": {
                "artifact": "cta-v1",
                "content_type": "cta",
                "stages": [],
                "grounding_ran": True,
                "grounded_ok": True,
                "grounding_summary": {
                    "total_claims": 1,
                    "grounded_claims": 1,
                    "flagged_count": 0,
                    "cut_applied": False,
                },
                "citations": [
                    {
                        "claim": "supports OTel",
                        "kind": "capability",
                        "sources": [{"origin": "kb", "ref": "docs/otel.md"}],
                    }
                ],
                "unsourced": [],
            },
        },
    )
    r = _run(tmp_path, ["deliverables", "provenance", "cta"])
    assert r.exit_code == 0, r.output
    assert "Grounding: PASS" in r.output
    assert "kb:docs/otel.md" in r.output


def test_deliverables_provenance_no_match(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, ["deliverables", "provenance", "nope"])
    assert r.exit_code == 1, r.output


# ---- config ----------------------------------------------------------


def test_config_get_existing_key(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, ["config", "get", "project.name"])
    assert r.exit_code == 0, r.output
    assert "x" in r.output  # name set to "x" in _init


def test_config_get_missing_key(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, ["config", "get", "nope.deep.key"])
    assert r.exit_code == 1, r.output


def test_config_set_int(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, ["config", "set", "budget.monthly_usd", "250"])
    assert r.exit_code == 0, r.output
    cfg = tmp_path / ".devrel" / "config.toml"
    with cfg.open("rb") as f:
        data = tomllib.load(f)
    assert data["budget"]["monthly_usd"] == 250
    assert isinstance(data["budget"]["monthly_usd"], int)


def test_config_set_bool_and_string(tmp_path):
    _init(tmp_path)
    r1 = _run(tmp_path, ["config", "set", "model.opus_opt_in", "false"])
    assert r1.exit_code == 0, r1.output
    r2 = _run(tmp_path, ["config", "set", "project.url", "https://example.com"])
    assert r2.exit_code == 0, r2.output
    cfg = tmp_path / ".devrel" / "config.toml"
    with cfg.open("rb") as f:
        data = tomllib.load(f)
    assert data["model"]["opus_opt_in"] is False
    assert data["project"]["url"] == "https://example.com"


# ---- content slop ----------------------------------------------------


def test_content_slop_clean_file(tmp_path):
    _init(tmp_path)
    target = tmp_path / "clean.md"
    target.write_text("This is plain prose without any flagged terms.")
    r = _run(tmp_path, ["content", "slop", str(target)])
    assert r.exit_code == 0, r.output
    assert "no slop" in r.output


def test_content_slop_dirty_file_exits_nonzero(tmp_path):
    _init(tmp_path)
    target = tmp_path / "dirty.md"
    # 'delve' and 'seamlessly' are in the default blocklist template.
    target.write_text("Let's delve into this seamlessly integrated solution.")
    r = _run(tmp_path, ["content", "slop", str(target)])
    assert r.exit_code == 1, r.output
    assert "slop hit" in r.output
