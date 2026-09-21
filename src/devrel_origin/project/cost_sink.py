"""Build a cost-sink callable that inserts rows into the project state DB.

Used by Atlas to wire LLMClient cost events into `.devrel/state.db`'s
`costs` table. The pricing table lives in core/llm.py — we read it
indirectly via the model names we receive.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from devrel_origin.core.llm import MODEL_COSTS


def is_priced(model: str) -> bool:
    """Return True when the price table can price `model`."""
    return model in MODEL_COSTS


def _compute_cost_usd(model: str, usage: dict[str, Any]) -> float:
    pricing = MODEL_COSTS.get(model)
    if pricing is None:
        return 0.0
    input_per_1m = pricing["input"]
    output_per_1m = pricing["output"]
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    # Cache pricing: read at 0.1×, write at 1.25× of input rate (Anthropic standard)
    cost = (
        (input_tokens / 1_000_000) * input_per_1m
        + (output_tokens / 1_000_000) * output_per_1m
        + (cache_read / 1_000_000) * input_per_1m * 0.1
        + (cache_write / 1_000_000) * input_per_1m * 1.25
    )
    return round(cost, 6)


def make_sqlite_sink(db_path: Path):
    """Return an async ``(agent, model, usage) -> None`` callback that inserts
    a row into the `costs` table at `db_path`."""

    async def _sink(agent: str, model: str, usage: dict[str, Any]) -> None:
        cost_usd = _compute_cost_usd(model, usage)
        # SQLite is sync; we accept the brief blocking write inline.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO costs (agent, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    agent,
                    model,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("cache_read_input_tokens", 0),
                    usage.get("cache_creation_input_tokens", 0),
                    cost_usd,
                ),
            )
            conn.commit()

    return _sink
