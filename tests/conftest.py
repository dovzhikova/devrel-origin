"""Shared pytest fixtures for devrel-origin tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from devrel_origin.core.llm import LLMClient
from devrel_origin.tools.api_client import PostHogClient
from devrel_origin.tools.github_tools import GitHubIssue, GitHubTools


@pytest.fixture(autouse=True)
def _no_ambient_typesafe_key(monkeypatch):
    """`build_judge()` reads `TYPESAFE_API_KEY` straight from the environment.
    A developer's shell may export a real one (it does in this repo's dev
    environment), and the anti-slop stage now calls `judge.judge_patterns`
    whenever `judge.available`. Without this, any test that exercises
    `run_pipeline`/`_slop_stage` without explicitly mocking the judge would
    make a real, paid, non-deterministic network call. Strip it by default;
    a test that wants a real-looking key sets it back with
    `monkeypatch.setenv`."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


# The 21-test baseline-xfail set was retired in v0.2.7 dogfood follow-on;
# every entry is now either renamed-and-passing or asserts the new behavior
# directly. If new prod-vs-test drift accumulates, re-introduce the xfail
# machinery here rather than letting failures linger.


@pytest.fixture
def posthog_client():
    """Fixture providing a mocked PostHog client."""
    client = MagicMock(spec=PostHogClient)
    client.api_key = "test_key"
    client.project_id = "12345"
    client.query_insights = AsyncMock(return_value={"results": []})
    client.capture = AsyncMock(return_value={"status": 1})
    return client


@pytest.fixture
def knowledge_base_path(tmp_path):
    """Fixture providing a temp knowledge base directory."""
    kb = tmp_path / "knowledge_base"
    kb.mkdir()
    (kb / "sdks").mkdir()
    (kb / "sdks" / "python.md").write_text("# Python SDK\nInstall with pip.")
    (kb / "products").mkdir()
    (kb / "products" / "analytics.md").write_text("# Analytics\nTrack events.")
    return kb


@pytest.fixture
def sample_issues():
    """Fixture providing sample GitHub issues."""
    return [
        {
            "id": "issue_1",
            "number": 1,
            "title": "Bug: Authentication fails intermittently",
            "body": "OAuth login sometimes fails with timeout error.",
            "created_at": "2026-03-10",
            "state": "open",
            "reactions": {"thumbsup": 12, "confused": 3},
            "comments": 5,
            "labels": ["bug", "authentication"],
        },
        {
            "id": "issue_2",
            "number": 2,
            "title": "Feature Request: Dark mode support",
            "body": "Would be great to have dark mode for the dashboard.",
            "created_at": "2026-03-08",
            "state": "open",
            "reactions": {"thumbsup": 45, "heart": 12},
            "comments": 8,
            "labels": ["feature", "ui"],
        },
        {
            "id": "issue_3",
            "number": 3,
            "title": "Docs: API endpoint examples unclear",
            "body": "The REST API documentation needs more examples.",
            "created_at": "2026-03-05",
            "state": "open",
            "reactions": {"thumbsup": 3, "confused": 8},
            "comments": 2,
            "labels": ["documentation"],
        },
        {
            "id": "issue_4",
            "number": 4,
            "title": "Performance: Slow query response times",
            "body": "Analytics queries take 30+ seconds on large datasets.",
            "created_at": "2026-03-02",
            "state": "open",
            "reactions": {"thumbsup": 25, "fire": 8},
            "comments": 14,
            "labels": ["performance", "bug"],
        },
    ]


@pytest.fixture
def mock_llm_client():
    """Fixture providing a mocked LLM client."""
    client = MagicMock(spec=LLMClient)
    client.generate = AsyncMock(return_value="Mocked LLM response")
    client.close = AsyncMock()
    client.usage = MagicMock()
    client.usage.to_dict = MagicMock(
        return_value={
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_calls": 0,
        }
    )
    return client


@pytest.fixture
def mock_github_tools():
    """Fixture providing mocked GitHub tools."""
    gh = MagicMock(spec=GitHubTools)
    gh.fetch_recent_issues = AsyncMock(
        return_value=[
            GitHubIssue(
                number=101,
                title="Bug: SDK init fails on React Native",
                body="Getting crash on startup. I'm switching to Amplitude if this isn't fixed.",
                author="frustrated-dev",
                state="open",
                labels=["bug"],
                created_at="2026-03-10T10:00:00Z",
                updated_at="2026-03-10T10:00:00Z",
                comments_count=3,
                reactions_total=8,
                url="https://github.com/PostHog/posthog/issues/101",
            ),
            GitHubIssue(
                number=102,
                title="Feature Request: Export insights as PDF",
                body="Would be nice to export analytics dashboards.",
                author="happy-user",
                state="open",
                labels=["feature"],
                created_at="2026-03-09T10:00:00Z",
                updated_at="2026-03-09T10:00:00Z",
                comments_count=1,
                reactions_total=15,
                url="https://github.com/PostHog/posthog/issues/102",
            ),
            GitHubIssue(
                number=103,
                title="Docs: Feature flags tutorial is outdated",
                body="The docs reference the old API. Please update.",
                author="docs-reader",
                state="open",
                labels=["documentation"],
                created_at="2026-03-08T10:00:00Z",
                updated_at="2026-03-08T10:00:00Z",
                comments_count=0,
                reactions_total=2,
                url="https://github.com/PostHog/posthog/issues/103",
            ),
        ]
    )
    gh.close = AsyncMock()
    return gh
