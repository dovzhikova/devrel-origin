"""Tests for github_tools repo-facts helpers used by the grounding stage."""

import httpx
import respx

from devrel_origin.tools.github_tools import GITHUB_API, GitCommit, GitHubTools

_COMMIT = {
    "sha": "deadbeef0123456789",
    "html_url": "https://github.com/openclaw/openclaw/commit/deadbeef",
    "commit": {
        "message": "feat: add OpenTelemetry export\n\nlong body",
        "author": {"name": "Dev", "date": "2026-03-10T00:00:00Z"},
    },
}

_REPO = {
    "stargazers_count": 100,
    "forks_count": 10,
    "open_issues_count": 5,
    "subscribers_count": 7,
    "language": "Python",
    "updated_at": "2026-03-10T00:00:00Z",
}


@respx.mock
async def test_fetch_recent_commits():
    respx.get(f"{GITHUB_API}/repos/openclaw/openclaw/commits").mock(
        return_value=httpx.Response(200, json=[_COMMIT])
    )
    gh = GitHubTools(token="", repo="openclaw/openclaw")
    try:
        commits = await gh.fetch_recent_commits()
    finally:
        await gh.close()
    assert len(commits) == 1
    assert isinstance(commits[0], GitCommit)
    assert commits[0].sha == "deadbeef0123456789"
    assert commits[0].message.startswith("feat: add OpenTelemetry")


@respx.mock
async def test_get_repo_facts_combines_commits_and_stats():
    respx.get(f"{GITHUB_API}/repos/openclaw/openclaw/commits").mock(
        return_value=httpx.Response(200, json=[_COMMIT])
    )
    respx.get(f"{GITHUB_API}/repos/openclaw/openclaw").mock(
        return_value=httpx.Response(200, json=_REPO)
    )
    gh = GitHubTools(token="", repo="openclaw/openclaw")
    try:
        facts = await gh.get_repo_facts()
    finally:
        await gh.close()

    refs = {f["ref"] for f in facts}
    assert any(r.startswith("commit:") for r in refs)
    assert "repo_stats" in refs
    # Commit fact excerpt is the first line of the message only.
    commit_fact = next(f for f in facts if f["ref"].startswith("commit:"))
    assert commit_fact["excerpt"] == "feat: add OpenTelemetry export"
    stats_fact = next(f for f in facts if f["ref"] == "repo_stats")
    assert "100 stars" in stats_fact["excerpt"]


@respx.mock
async def test_get_repo_facts_degrades_when_commits_fail():
    respx.get(f"{GITHUB_API}/repos/openclaw/openclaw/commits").mock(
        return_value=httpx.Response(500)
    )
    respx.get(f"{GITHUB_API}/repos/openclaw/openclaw").mock(
        return_value=httpx.Response(200, json=_REPO)
    )
    gh = GitHubTools(token="", repo="openclaw/openclaw")
    try:
        facts = await gh.get_repo_facts()
    finally:
        await gh.close()
    # Commits failed, but stats still produced a fact.
    assert [f["ref"] for f in facts] == ["repo_stats"]
