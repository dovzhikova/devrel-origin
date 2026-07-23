"""CLI + ranking tests for `devrel next ...`."""

import json
import re

from typer.testing import CliRunner

from devrel_origin.cli import app, next as next_module
from devrel_origin.core.iris import FeedbackTheme


def _strip_ansi(text: str) -> str:
    """CI colorizes help and may split option names across resets."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _theme(
    theme_id: str,
    title: str,
    frequency: int,
    severity: float,
    product_areas: list[str],
    actions: list[str] | None = None,
) -> FeedbackTheme:
    return FeedbackTheme(
        theme_id=theme_id,
        title=title,
        description=f"desc for {title}",
        frequency=frequency,
        severity=severity,
        composite_score=frequency * severity,
        sources=["github"],
        representative_quotes=[],
        product_areas=product_areas,
        recommended_actions=actions or [f"fix {title}"],
    )


def _project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    devrel_dir = tmp_path / ".devrel"
    devrel_dir.mkdir()
    (devrel_dir / "config.toml").write_text(
        'product_name = "Test"\nproduct_url = "https://example.com"\n'
    )
    return devrel_dir


# ---------------------------------------------------------------------------
# Pure ranking (no I/O, no keys)
# ---------------------------------------------------------------------------


def test_rank_actions_orders_by_reach_intent_effort():
    # High reach + high-intent/low-effort area (docs) should beat a
    # lower-reach, higher-effort area (orchestration) even at equal severity.
    themes = [
        _theme(
            "t1",
            "Onboarding docs unclear",
            frequency=10,
            severity=5.0,
            product_areas=["onboarding/docs"],
        ),
        _theme(
            "t2",
            "Orchestration edge case",
            frequency=6,
            severity=5.0,
            product_areas=["orchestration"],
        ),
    ]
    ranked = next_module.rank_actions(themes)
    assert [a.theme_id for a in ranked] == ["t1", "t2"]
    # docs: 10 x 3 x 3 = 90 ; orchestration: 6 x 2 x 1 = 12
    assert ranked[0].score == 90.0
    assert ranked[1].score == 12.0


def test_rank_actions_returns_at_most_top_n():
    themes = [
        _theme(f"t{i}", f"Theme {i}", frequency=i + 1, severity=5.0, product_areas=["docs"])
        for i in range(6)
    ]
    ranked = next_module.rank_actions(themes, top_n=3)
    assert len(ranked) == 3
    # Highest frequency wins within the same area.
    assert ranked[0].frequency == 6


def test_rank_actions_unmapped_area_uses_defaults():
    themes = [_theme("t1", "Mystery", frequency=4, severity=5.0, product_areas=["nonsense"])]
    ranked = next_module.rank_actions(themes)
    # 4 x 2.0 x 2.0 = 16.0 (default intent/effort)
    assert ranked[0].score == 16.0
    assert ranked[0].intent == 2.0
    assert ranked[0].effort == 2.0


def test_rank_actions_empty_is_empty():
    assert next_module.rank_actions([]) == []


def test_rank_actions_action_falls_back_when_no_recommendation():
    themes = [
        _theme("t1", "Naked theme", frequency=2, severity=5.0, product_areas=["docs"], actions=[])
    ]
    ranked = next_module.rank_actions(themes)
    assert "Naked theme" in ranked[0].action


# ---------------------------------------------------------------------------
# CLI wiring (monkeypatch _build_iris, no keys / no LLM)
# ---------------------------------------------------------------------------


def test_next_help_lists_report():
    runner = CliRunner()
    result = runner.invoke(app, ["next", "--help"])
    assert result.exit_code == 0
    assert "report" in _strip_ansi(result.output).lower()


def test_next_report_help_runs():
    runner = CliRunner()
    result = runner.invoke(app, ["next", "report", "--help"])
    assert result.exit_code == 0
    out = _strip_ansi(result.output).lower()
    assert "since" in out
    assert "draft" in out


def _patch_iris(monkeypatch, themes):
    """Stub _build_iris so the verb runs offline with canned themes."""

    class _FakeSynthesis:
        def __init__(self, t):
            self.themes = t

    class _FakeIris:
        async def synthesize_weekly(self, **kwargs):
            return _FakeSynthesis(themes)

    monkeypatch.setattr(next_module, "_build_iris", lambda paths: _FakeIris())


def test_next_report_writes_deliverable_and_ranks(tmp_path, monkeypatch):
    devrel_dir = _project(tmp_path, monkeypatch)
    themes = [
        _theme("t1", "Docs gap", frequency=8, severity=6.0, product_areas=["onboarding/docs"]),
        _theme("t2", "SDK confusion", frequency=5, severity=6.0, product_areas=["agent sdk"]),
        _theme("t3", "Scaling pain", frequency=2, severity=6.0, product_areas=["orchestration"]),
    ]
    _patch_iris(monkeypatch, themes)

    runner = CliRunner()
    result = runner.invoke(app, ["next", "report", "--format", "json"])
    assert result.exit_code == 0, f"CLI failed: {result.output!r}"

    payload = json.loads(result.output)
    assert payload["total_themes"] == 3
    assert len(payload["actions"]) == 3
    # Docs beats SDK beats orchestration on reach x intent x effort.
    assert [a["theme_id"] for a in payload["actions"]] == ["t1", "t2", "t3"]

    # Deliverable artifact was written under .devrel/deliverables/.
    deliverables = list((devrel_dir / "deliverables").glob("*-next-actions.md"))
    assert len(deliverables) == 1
    body = deliverables[0].read_text()
    assert "Do this next" in body
    assert "Docs gap" in body


def test_next_report_top_flag_limits_actions(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    themes = [
        _theme(f"t{i}", f"Theme {i}", frequency=i + 1, severity=5.0, product_areas=["docs"])
        for i in range(5)
    ]
    _patch_iris(monkeypatch, themes)

    runner = CliRunner()
    result = runner.invoke(app, ["next", "report", "--top", "2", "--format", "json"])
    assert result.exit_code == 0, f"CLI failed: {result.output!r}"
    payload = json.loads(result.output)
    assert len(payload["actions"]) == 2


def test_next_report_no_themes_degrades_gracefully(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    _patch_iris(monkeypatch, [])

    runner = CliRunner()
    result = runner.invoke(app, ["next", "report"])
    assert result.exit_code == 0
    assert "No themes to rank" in _strip_ansi(result.output)
