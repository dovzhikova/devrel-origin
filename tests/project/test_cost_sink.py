"""Tests for the SQLite cost-sink adapter."""

from __future__ import annotations

import sqlite3

import pytest

from devrel_origin.project.cost_sink import _compute_cost_usd, is_priced, make_sqlite_sink
from devrel_origin.project.state import init_db


def test_compute_cost_usd_sonnet():
    # Sonnet 4.5: $3 input / $15 output per 1M
    cost = _compute_cost_usd(
        "claude-sonnet-4-5-20250929",
        {"input_tokens": 1_000_000, "output_tokens": 0},
    )
    assert cost == pytest.approx(3.0)
    cost = _compute_cost_usd(
        "claude-sonnet-4-5-20250929",
        {"input_tokens": 0, "output_tokens": 1_000_000},
    )
    assert cost == pytest.approx(15.0)


def test_compute_cost_usd_unknown_model_returns_zero():
    assert _compute_cost_usd("not-a-real-model", {"input_tokens": 1000}) == 0.0


def test_is_priced_known_model():
    assert is_priced("claude-sonnet-4-5-20250929") is True


def test_is_priced_unknown_model():
    assert is_priced("typesafe:jev") is False


def test_compute_cost_usd_includes_cache_tokens():
    base = _compute_cost_usd(
        "claude-haiku-4-5-20251001",
        {"input_tokens": 0, "output_tokens": 0},
    )
    with_cache = _compute_cost_usd(
        "claude-haiku-4-5-20251001",
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 1_000_000,
        },
    )
    assert with_cache > base


@pytest.mark.asyncio
async def test_sqlite_sink_inserts_row(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    sink = make_sqlite_sink(db)

    await sink(
        "kai",
        "claude-sonnet-4-5-20250929",
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )

    with sqlite3.connect(db) as conn:
        rows = list(
            conn.execute("SELECT agent, model, input_tokens, output_tokens, cost_usd FROM costs")
        )
    assert len(rows) == 1
    agent, model, in_t, out_t, cost = rows[0]
    assert agent == "kai"
    assert in_t == 100
    assert out_t == 50
    assert cost > 0


@pytest.mark.asyncio
async def test_sqlite_sink_two_calls_two_rows(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    sink = make_sqlite_sink(db)
    for agent in ("sage", "kai"):
        await sink(
            agent,
            "claude-haiku-4-5-20251001",
            {"input_tokens": 10, "output_tokens": 5},
        )
    with sqlite3.connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
    assert n == 2
