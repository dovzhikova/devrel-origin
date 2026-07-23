"""LLM-citation probe — does an answer engine mention/cite this brand?

Backs the `geo_visibility` table and the `devrel geo` verbs. Roadmap gap B1
("per-engine LLM-citation tracking + fix"): answer engines cite differently
(Perplexity leans on Reddit ~47% of the time; ChatGPT favors authoritative +
recent sources), so results are kept **per engine**, never averaged.

This module is deliberately split into two layers:

* ``analyze_mention`` — pure, deterministic analysis of an answer string for a
  brand mention. Implemented and unit-tested now.
* ``CitationProbe._fetch_answer`` — the live engine call (ChatGPT via the OpenAI
  Responses API + web_search tool, Perplexity chat/completions, Google AI
  Overviews via DataForSEO). Engines without a configured key are reported
  ``configured=False`` and skipped; a configured engine whose call fails returns
  ``None`` too (skipped, never fabricated).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Engines we track, in a stable order. Kept per-engine on purpose.
ENGINES: tuple[str, ...] = ("chatgpt", "perplexity", "google_aio")

# Live endpoints (overridable models via the CitationProbe ctor).
_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
_PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
_DEFAULT_DFS_BASE = "https://api.dataforseo.com"
_HTTP_TIMEOUT = httpx.Timeout(90.0)


@dataclass
class CitationResult:
    """One (prompt x engine) probe outcome. Maps 1:1 onto ``geo_visibility``."""

    prompt_id: str
    engine: str
    is_mentioned: bool = False
    # 'cited'  -> brand's own domain appears in the answer's sources
    # 'direct' -> brand name/alias appears in the answer text
    # 'indirect' -> described but not named (reserved; set by richer analysis)
    # None -> not mentioned
    mention_type: str | None = None
    # 1 = first/most prominent mention, higher = later. None if unmentioned.
    position_score: int | None = None
    # brand-owned cited URLs / total cited URLs, 0.0-1.0. None if no citations.
    citation_share: float | None = None
    # reserved for sentiment/quality of the mention (0-100). Filled by richer analysis.
    quality_score: int | None = None
    # path to the saved raw answer for audit, or None.
    response_path: str | None = None
    # competitor brands/domains cited alongside (for share-of-voice; not persisted here).
    competitors_cited: list[str] = field(default_factory=list)

    # False when the engine had no configured key and was skipped (not persisted).
    configured: bool = True


def _domain(url: str) -> str:
    """Bare registrable-ish host for a URL, lowercased, no leading www."""
    try:
        host = urlparse(url if "//" in url else f"//{url}").netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def analyze_mention(
    prompt_id: str,
    engine: str,
    answer_text: str,
    cited_urls: list[str],
    *,
    brand: str,
    aliases: list[str] | None = None,
    domain: str | None = None,
) -> CitationResult:
    """Detect whether ``brand`` is mentioned/cited in one engine answer.

    Pure and deterministic — no network. ``position_score`` is 1-based on the
    order the first brand token appears in the answer text (1 = most prominent).
    ``mention_type`` is 'cited' when the brand's own ``domain`` is among
    ``cited_urls``, else 'direct' when a name/alias appears in the text.
    """
    aliases = aliases or []
    names = [n.lower() for n in ([brand, *aliases]) if n]
    text_l = (answer_text or "").lower()
    brand_domain = _domain(domain) if domain else ""

    cited_domains = [_domain(u) for u in (cited_urls or []) if u]
    own_cited = bool(brand_domain) and brand_domain in cited_domains
    citation_share: float | None = None
    if cited_domains:
        hits = sum(1 for d in cited_domains if d == brand_domain)
        citation_share = round(hits / len(cited_domains), 4)

    # earliest character index of any name in the answer text
    first_idx = min((text_l.find(n) for n in names if n and n in text_l), default=-1)
    named = first_idx != -1

    is_mentioned = bool(named or own_cited)
    if not is_mentioned:
        return CitationResult(prompt_id=prompt_id, engine=engine, is_mentioned=False)

    mention_type = "cited" if own_cited else "direct"
    # crude prominence: 1 if the brand appears in the first ~200 chars, scaling to 5.
    position_score: int | None
    if named:
        position_score = min(5, max(1, first_idx // 200 + 1))
    else:
        position_score = None

    competitors = sorted({d for d in cited_domains if d and d != brand_domain})

    return CitationResult(
        prompt_id=prompt_id,
        engine=engine,
        is_mentioned=True,
        mention_type=mention_type,
        position_score=position_score,
        citation_share=citation_share,
        competitors_cited=competitors,
    )


class CitationProbe:
    """Probes answer engines for brand citations across a prompt set.

    Construct with the brand identity and whatever engine keys are available.
    Engines without a key are skipped (reported ``configured=False``), so the
    probe runs and records partial coverage rather than inventing data.
    """

    def __init__(
        self,
        brand: str,
        *,
        aliases: list[str] | None = None,
        domain: str | None = None,
        openai_key: str = "",
        perplexity_key: str = "",
        dfs_auth: str = "",  # DataForSEO Basic auth for Google AI Overviews
        dfs_base: str = "",
        openai_model: str = "gpt-4o",
        perplexity_model: str = "sonar",
    ) -> None:
        self.brand = brand
        self.aliases = aliases or []
        self.domain = domain
        self._keys = {
            "chatgpt": openai_key,
            "perplexity": perplexity_key,
            "google_aio": dfs_auth,
        }
        self._dfs_base = (dfs_base or _DEFAULT_DFS_BASE).rstrip("/")
        self._openai_model = openai_model
        self._perplexity_model = perplexity_model

    async def _post_json(self, url: str, *, headers: dict, json: object) -> dict | None:
        """POST and return parsed JSON, or ``None`` on any HTTP/parse failure.

        A failed configured engine returns ``None`` (skip) rather than fabricating
        a "not mentioned" result — we never invent citation data.
        """
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=json)
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("citation_probe: POST %s failed: %s", url, exc)
            return None

    async def _probe_chatgpt(self, prompt: str) -> tuple[str, list[str]] | None:
        """OpenAI Responses API with the web_search tool → (answer, cited urls)."""
        data = await self._post_json(
            _OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {self._keys['chatgpt']}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._openai_model,
                "input": prompt,
                "tools": [{"type": "web_search"}],
            },
        )
        if data is None:
            return None
        parts: list[str] = []
        urls: list[str] = []
        for item in data.get("output") or []:
            if item.get("type") != "message":
                continue
            for c in item.get("content") or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    parts.append(c["text"])
                for ann in c.get("annotations") or []:
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        urls.append(ann["url"])
        if not parts and data.get("output_text"):
            parts.append(str(data["output_text"]))
        return " ".join(parts), urls

    async def _probe_perplexity(self, prompt: str) -> tuple[str, list[str]] | None:
        """Perplexity chat/completions → (answer, cited urls)."""
        data = await self._post_json(
            _PERPLEXITY_URL,
            headers={
                "Authorization": f"Bearer {self._keys['perplexity']}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._perplexity_model,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if data is None:
            return None
        choices = data.get("choices") or []
        answer = ""
        if choices:
            answer = (choices[0].get("message") or {}).get("content", "") or ""
        urls: list[str] = [u for u in (data.get("citations") or []) if u]
        if not urls:
            urls = [r.get("url") for r in (data.get("search_results") or []) if r.get("url")]
        return answer, urls

    async def _probe_google_aio(self, prompt: str) -> tuple[str, list[str]] | None:
        """DataForSEO Google organic SERP → the AI Overview block (answer + refs)."""
        data = await self._post_json(
            f"{self._dfs_base}/v3/serp/google/organic/live/advanced",
            headers={
                "Authorization": f"Basic {self._keys['google_aio']}",
                "Content-Type": "application/json",
            },
            json=[
                {
                    "keyword": prompt,
                    "location_name": "United States",
                    "language_name": "English",
                }
            ],
        )
        if data is None:
            return None
        try:
            items = data["tasks"][0]["result"][0]["items"]
        except (KeyError, IndexError, TypeError):
            return "", []
        parts: list[str] = []
        urls: list[str] = []
        for it in items or []:
            if it.get("type") != "ai_overview":
                continue
            for el in it.get("items") or []:
                if el.get("text"):
                    parts.append(el["text"])
            for ref in it.get("references") or []:
                if ref.get("url"):
                    urls.append(ref["url"])
        return " ".join(parts), urls

    async def _fetch_answer(self, engine: str, prompt: str) -> tuple[str, list[str]] | None:
        """Return ``(answer_text, cited_urls)`` from a live engine, or ``None`` when
        the engine has no key or the call fails (skipped, never fabricated)."""
        if not self._keys.get(engine):
            return None
        if engine == "chatgpt":
            return await self._probe_chatgpt(prompt)
        if engine == "perplexity":
            return await self._probe_perplexity(prompt)
        if engine == "google_aio":
            return await self._probe_google_aio(prompt)
        return None

    async def probe(self, prompts: list[tuple[str, str]]) -> list[CitationResult]:
        """Probe every configured engine for each ``(prompt_id, prompt_text)``.

        Unconfigured engines yield a ``configured=False`` marker result so the
        caller can report coverage; configured engines yield real analysis.
        """
        results: list[CitationResult] = []
        for prompt_id, prompt_text in prompts:
            for engine in ENGINES:
                fetched = await self._fetch_answer(engine, prompt_text)
                if fetched is None:
                    results.append(
                        CitationResult(prompt_id=prompt_id, engine=engine, configured=False)
                    )
                    continue
                answer_text, cited_urls = fetched
                results.append(
                    analyze_mention(
                        prompt_id,
                        engine,
                        answer_text,
                        cited_urls,
                        brand=self.brand,
                        aliases=self.aliases,
                        domain=self.domain,
                    )
                )
        return results
