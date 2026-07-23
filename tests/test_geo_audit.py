"""Tests for the GEO-hygiene audit (gap A2)."""

import httpx
import respx
from typer.testing import CliRunner

from devrel_origin.cli import app
from devrel_origin.tools.geo_audit import blocked_ai_bots, run_geo_audit

_BASE = "https://example.com/"


def test_blocked_ai_bots_detects_gptbot_block():
    robots = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    assert "GPTBot" in blocked_ai_bots(robots)


def test_blocked_ai_bots_none_when_allowed():
    assert blocked_ai_bots("User-agent: *\nAllow: /\n") == []


def _mock_site(*, robots, page, llms_status, sitemap_status):
    respx.get(f"{_BASE}robots.txt").mock(return_value=httpx.Response(200, text=robots))
    respx.get(_BASE).mock(return_value=httpx.Response(200, text=page))
    respx.get(f"{_BASE}llms.txt").mock(return_value=httpx.Response(llms_status))
    respx.get(f"{_BASE}sitemap.xml").mock(return_value=httpx.Response(sitemap_status))


@respx.mock
async def test_run_geo_audit_scores_mixed_site():
    _mock_site(
        robots="User-agent: GPTBot\nDisallow: /\n",
        page='<script type="application/ld+json">{"@type":"Organization"}</script>',
        llms_status=404,
        sitemap_status=200,
    )
    report = await run_geo_audit(_BASE)
    by = {c.key: c.status for c in report.checks}
    assert by["ai_crawlers"] == "fail"  # GPTBot blocked
    assert by["schema"] == "pass"  # JSON-LD present
    assert by["llms_txt"] == "warn"  # missing
    assert by["sitemap"] == "pass"
    assert 0 < report.score < 100


@respx.mock
def test_geo_audit_cli_runs():
    _mock_site(
        robots="User-agent: *\nAllow: /\n",
        page="<html></html>",
        llms_status=200,
        sitemap_status=200,
    )
    result = CliRunner().invoke(app, ["geo", "audit", _BASE])
    assert result.exit_code == 0, result.output
    assert "GEO hygiene" in result.output
