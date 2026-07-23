"""GEO-hygiene audit — is a site set up to be found and cited by answer engines?

Roadmap gap A2, a free (no-auth) lead magnet. Checks the cheap, high-signal
things sites get wrong: accidentally blocking AI crawlers in robots.txt (the
"accidental blockade" ~30% of sites hit), a missing llms.txt, no structured
data for engines to parse, and no sitemap. Returns a scored report + fixes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

# Crawlers the major answer engines use for training/retrieval/citation.
AI_BOTS = (
    "GPTBot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "PerplexityBot",
    "ClaudeBot",
    "Claude-Web",
    "Google-Extended",
)

_STATUS_WEIGHT = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
_TIMEOUT = httpx.Timeout(20.0)


@dataclass
class AuditCheck:
    key: str
    label: str
    status: str  # "pass" | "warn" | "fail"
    detail: str
    fix: str = ""


@dataclass
class GeoAuditReport:
    url: str
    checks: list[AuditCheck] = field(default_factory=list)

    @property
    def score(self) -> int:
        if not self.checks:
            return 0
        total = sum(_STATUS_WEIGHT[c.status] for c in self.checks)
        return round(100 * total / len(self.checks))


def _origin(url: str) -> str:
    p = urlparse(url if "//" in url else f"https://{url}")
    scheme = p.scheme or "https"
    return f"{scheme}://{p.netloc}"


def blocked_ai_bots(robots_txt: str) -> list[str]:
    """AI bots fully disallowed at root by robots.txt (via their own group or `*`)."""
    groups: dict[str, list[str]] = {}
    current: list[str] = []
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, value = (p.strip() for p in line.split(":", 1))
        field_name = field_name.lower()
        if field_name == "user-agent":
            current = [value.lower()]
            groups.setdefault(value.lower(), [])
        elif field_name in ("disallow", "allow") and current:
            for ua in current:
                groups.setdefault(ua, []).append(f"{field_name} {value}")

    def _root_blocked(ua: str) -> bool:
        rules = groups.get(ua.lower())
        if not rules:
            return False
        # last matching rule wins-ish; treat an explicit "allow /" as unblocking root
        blocked = any(r == "disallow /" for r in rules)
        allowed = any(r == "allow /" for r in rules)
        return blocked and not allowed

    return [bot for bot in AI_BOTS if _root_blocked(bot) or _root_blocked("*")]


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        return await client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        logger.warning("geo_audit: GET %s failed: %s", url, exc)
        return None


async def run_geo_audit(url: str) -> GeoAuditReport:
    """Run the GEO-hygiene checks against ``url`` and return a scored report."""
    origin = _origin(url)
    report = GeoAuditReport(url=url)
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "devrel-geo-audit"}) as c:
        robots = await _get(c, urljoin(origin + "/", "robots.txt"))
        page = await _get(c, url if "//" in url else f"https://{url}")
        llms = await _get(c, urljoin(origin + "/", "llms.txt"))
        sitemap = await _get(c, urljoin(origin + "/", "sitemap.xml"))

    # 1. AI crawler access (the accidental blockade)
    if robots is not None and robots.status_code == 200:
        blocked = blocked_ai_bots(robots.text)
        if blocked:
            report.checks.append(
                AuditCheck(
                    "ai_crawlers",
                    "AI crawler access",
                    "fail",
                    f"robots.txt blocks: {', '.join(blocked)}",
                    "Remove the Disallow rules for these bots so answer engines can read you.",
                )
            )
        else:
            report.checks.append(
                AuditCheck("ai_crawlers", "AI crawler access", "pass", "No AI crawlers blocked.")
            )
    else:
        report.checks.append(
            AuditCheck(
                "ai_crawlers",
                "AI crawler access",
                "pass",
                "No robots.txt (nothing blocked).",
            )
        )

    # 2. llms.txt
    if llms is not None and llms.status_code == 200:
        report.checks.append(AuditCheck("llms_txt", "llms.txt", "pass", "Present."))
    else:
        report.checks.append(
            AuditCheck(
                "llms_txt",
                "llms.txt",
                "warn",
                "Missing.",
                "Add /llms.txt pointing engines at your best canonical pages.",
            )
        )

    # 3. Structured data (JSON-LD)
    if page is not None and page.status_code == 200 and "application/ld+json" in page.text:
        types = sorted(set(re.findall(r'"@type"\s*:\s*"([A-Za-z]+)"', page.text)))
        detail = "Present" + (f" ({', '.join(types[:5])})" if types else "")
        report.checks.append(AuditCheck("schema", "Structured data", "pass", detail))
    else:
        report.checks.append(
            AuditCheck(
                "schema",
                "Structured data",
                "warn",
                "No JSON-LD found on the page.",
                "Add JSON-LD (Organization, FAQPage, HowTo, SoftwareApplication) so engines can parse you.",
            )
        )

    # 4. Sitemap
    if sitemap is not None and sitemap.status_code == 200:
        report.checks.append(AuditCheck("sitemap", "Sitemap", "pass", "Present."))
    else:
        report.checks.append(
            AuditCheck(
                "sitemap",
                "Sitemap",
                "warn",
                "No /sitemap.xml.",
                "Publish a sitemap.xml with lastmod so engines discover fresh content.",
            )
        )

    return report
