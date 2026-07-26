"""
GitHub Tools — Issue, PR, and contributor analysis.

Provides typed async access to the GitHub REST API v3 for:
- Fetching and filtering issues and pull requests
- Analyzing contributor history and activity
- Detecting duplicate issues
- Extracting labels and milestones
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_REPO = os.getenv("GITHUB_REPO", "openclaw/openclaw")
GITHUB_API = "https://api.github.com"
API_TIMEOUT = 30.0


@dataclass
class GitHubIssue:
    """Parsed GitHub issue."""

    number: int
    title: str
    body: str
    author: str
    state: str
    labels: list[str]
    created_at: str
    updated_at: str
    comments_count: int
    reactions_total: int = 0
    is_pull_request: bool = False
    url: str = ""


@dataclass
class GitCommit:
    """Parsed GitHub commit (a repo fact usable for grounding claims)."""

    sha: str
    message: str
    author: str
    date: str
    url: str = ""


@dataclass
class ContributorProfile:
    """Summary of a GitHub contributor's activity."""

    username: str
    total_issues: int
    total_prs: int
    total_comments: int
    first_contribution: str
    last_contribution: str
    is_maintainer: bool = False


class GitHubTools:
    """
    Async GitHub API client focused on community health analysis.

    Usage::

        gh = GitHubTools(token="ghp_...")
        issues = await gh.fetch_recent_issues(days=7)
        profile = await gh.get_contributor_profile("some-user")
    """

    def __init__(
        self,
        token: str = "",
        repo: str = DEFAULT_REPO,
    ):
        self.repo = repo
        headers: dict[str, str] = {
            "Accept": "application/vnd.github.v3+json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers=headers,
            timeout=API_TIMEOUT,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def repo_full_name(self) -> str:
        """Compatibility alias used by analytics collectors."""
        return self.repo

    # -- Issues -----------------------------------------------------------

    async def fetch_recent_issues(
        self,
        days: int = 7,
        state: str = "open",
        labels: Optional[list[str]] = None,
        per_page: int = 100,
    ) -> list[GitHubIssue]:
        """Fetch issues created in the last N days."""
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
        from datetime import timedelta

        since -= timedelta(days=days)

        params: dict[str, Any] = {
            "state": state,
            "since": since.isoformat() + "Z",
            "per_page": per_page,
            "sort": "created",
            "direction": "desc",
        }
        if labels:
            params["labels"] = ",".join(labels)

        resp = await self._client.get(f"/repos/{self.repo}/issues", params=params)
        resp.raise_for_status()

        issues = []
        for item in resp.json():
            # Skip PRs returned via the issues endpoint
            is_pr = "pull_request" in item
            issues.append(
                GitHubIssue(
                    number=item["number"],
                    title=item["title"],
                    body=item.get("body") or "",
                    author=item["user"]["login"],
                    state=item["state"],
                    labels=[lbl["name"] for lbl in item.get("labels", [])],
                    created_at=item["created_at"],
                    updated_at=item["updated_at"],
                    comments_count=item.get("comments", 0),
                    reactions_total=item.get("reactions", {}).get("total_count", 0),
                    is_pull_request=is_pr,
                    url=item["html_url"],
                )
            )

        logger.info(f"Fetched {len(issues)} issues from {self.repo} (last {days} days)")
        return issues

    async def get_issue(self, issue_number: int) -> GitHubIssue:
        """Fetch a single issue by number."""
        resp = await self._client.get(f"/repos/{self.repo}/issues/{issue_number}")
        resp.raise_for_status()
        item = resp.json()
        return GitHubIssue(
            number=item["number"],
            title=item["title"],
            body=item.get("body") or "",
            author=item["user"]["login"],
            state=item["state"],
            labels=[lbl["name"] for lbl in item.get("labels", [])],
            created_at=item["created_at"],
            updated_at=item["updated_at"],
            comments_count=item.get("comments", 0),
            reactions_total=item.get("reactions", {}).get("total_count", 0),
            is_pull_request="pull_request" in item,
            url=item["html_url"],
        )

    async def get_issue_comments(
        self, issue_number: int, per_page: int = 100
    ) -> list[dict[str, Any]]:
        """Fetch all comments on an issue."""
        resp = await self._client.get(
            f"/repos/{self.repo}/issues/{issue_number}/comments",
            params={"per_page": per_page},
        )
        resp.raise_for_status()
        return [
            {
                "author": c["user"]["login"],
                "body": c["body"],
                "created_at": c["created_at"],
                "reactions": c.get("reactions", {}).get("total_count", 0),
            }
            for c in resp.json()
        ]

    # -- Contributors -----------------------------------------------------

    async def get_contributor_profile(self, username: str) -> ContributorProfile:
        """Build a contributor profile from issue/PR/comment activity."""
        # Issues authored
        issues_resp = await self._client.get(
            "/search/issues",
            params={
                "q": f"repo:{self.repo} author:{username} is:issue",
                "per_page": 1,
            },
        )
        issues_resp.raise_for_status()
        total_issues = issues_resp.json().get("total_count", 0)

        # PRs authored
        prs_resp = await self._client.get(
            "/search/issues",
            params={
                "q": f"repo:{self.repo} author:{username} is:pr",
                "per_page": 1,
            },
        )
        prs_resp.raise_for_status()
        total_prs = prs_resp.json().get("total_count", 0)

        # Comments (approximation via commenter search)
        comments_resp = await self._client.get(
            "/search/issues",
            params={
                "q": f"repo:{self.repo} commenter:{username}",
                "per_page": 1,
            },
        )
        comments_resp.raise_for_status()
        total_comments = comments_resp.json().get("total_count", 0)

        return ContributorProfile(
            username=username,
            total_issues=total_issues,
            total_prs=total_prs,
            total_comments=total_comments,
            first_contribution="",  # Would require deeper pagination
            last_contribution="",
        )

    # -- Search / Duplicates ---------------------------------------------

    async def search_similar_issues(self, query: str, limit: int = 5) -> list[GitHubIssue]:
        """Search for issues matching a query (for duplicate detection)."""
        resp = await self._client.get(
            "/search/issues",
            params={
                "q": f"repo:{self.repo} is:issue {query}",
                "per_page": limit,
                "sort": "relevance",
            },
        )
        resp.raise_for_status()
        return [
            GitHubIssue(
                number=item["number"],
                title=item["title"],
                body=item.get("body") or "",
                author=item["user"]["login"],
                state=item["state"],
                labels=[lbl["name"] for lbl in item.get("labels", [])],
                created_at=item["created_at"],
                updated_at=item["updated_at"],
                comments_count=item.get("comments", 0),
                url=item["html_url"],
            )
            for item in resp.json().get("items", [])
        ]

    # -- Labels & Milestones ---------------------------------------------

    async def list_labels(self) -> list[dict[str, str]]:
        """List all repo labels."""
        resp = await self._client.get(f"/repos/{self.repo}/labels", params={"per_page": 100})
        resp.raise_for_status()
        return [
            {
                "name": lbl["name"],
                "color": lbl["color"],
                "description": lbl.get("description") or "",
            }
            for lbl in resp.json()
        ]

    async def add_labels(self, issue_number: int, labels: list[str]) -> list[dict[str, str]]:
        """Add labels to an issue."""
        resp = await self._client.post(
            f"/repos/{self.repo}/issues/{issue_number}/labels",
            json={"labels": labels},
        )
        resp.raise_for_status()
        return resp.json()

    # -- Commits (repo facts for grounding) -------------------------------

    async def fetch_recent_commits(self, per_page: int = 30) -> list[GitCommit]:
        """Fetch recent commits to the default branch. These are repo FACTS a
        grounding stage can check claims against (e.g. "we shipped X")."""
        resp = await self._client.get(
            f"/repos/{self.repo}/commits",
            params={"per_page": per_page},
        )
        resp.raise_for_status()
        commits: list[GitCommit] = []
        for item in resp.json():
            commit = item.get("commit", {})
            author = commit.get("author", {}) or {}
            commits.append(
                GitCommit(
                    sha=item.get("sha", ""),
                    message=commit.get("message", ""),
                    author=author.get("name", ""),
                    date=author.get("date", ""),
                    url=item.get("html_url", ""),
                )
            )
        logger.info(f"Fetched {len(commits)} commits from {self.repo}")
        return commits

    async def get_repo_facts(self, commit_limit: int = 30) -> list[dict[str, str]]:
        """Assemble draft-independent repo facts (recent commits + repo stats)
        as grounding-source dicts with ``ref`` and ``excerpt`` keys. Failures
        in either source degrade gracefully to whatever was retrievable."""
        facts: list[dict[str, str]] = []
        try:
            for c in await self.fetch_recent_commits(per_page=commit_limit):
                summary = c.message.splitlines()[0] if c.message else ""
                facts.append({"ref": f"commit:{c.sha[:10]}", "excerpt": summary})
        except Exception as exc:
            logger.warning(f"Could not fetch commits for repo facts: {exc}")
        try:
            stats = await self.get_repo_stats()
            facts.append(
                {
                    "ref": "repo_stats",
                    "excerpt": (
                        f"{stats['stars']} stars, {stats['forks']} forks, "
                        f"{stats['open_issues']} open issues, "
                        f"primary language {stats['language']}."
                    ),
                }
            )
        except Exception as exc:
            logger.warning(f"Could not fetch repo stats for repo facts: {exc}")
        return facts

    # -- Repo Stats -------------------------------------------------------

    async def get_repo_stats(self) -> dict[str, Any]:
        """Get basic repository statistics."""
        resp = await self._client.get(f"/repos/{self.repo}")
        resp.raise_for_status()
        data = resp.json()
        return {
            "stars": data["stargazers_count"],
            "forks": data["forks_count"],
            "open_issues": data["open_issues_count"],
            "watchers": data["subscribers_count"],
            "language": data["language"],
            "updated_at": data["updated_at"],
        }
