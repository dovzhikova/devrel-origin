"""LLM-citation probe — does an answer engine mention/cite this brand?

Backs the `geo_visibility` table and the `devrel geo` verbs. Roadmap gap B1
("per-engine LLM-citation tracking + fix"): answer engines cite differently
(Perplexity leans on Reddit ~47% of the time; ChatGPT favors authoritative +
recent sources), so results are kept **per engine**, never averaged.

This module is deliberately split into two layers:

* ``analyze_mention`` — pure, deterministic analysis of an answer string for a
  brand mention. Implemented and unit-tested now.
* ``CitationProbe._fetch_answer`` — the live engine call (ChatGPT / Perplexity /
  Google AI Overviews). Stubbed with a clear interface; wiring real API calls is
  the next slice of B1. Until a key is configured for an engine, that engine is
  reported as ``configured=False`` and skipped (no fabricated data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

# Engines we track, in a stable order. Kept per-engine on purpose.
ENGINES: tuple[str, ...] = ("chatgpt", "perplexity", "google_aio")


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
    ) -> None:
        self.brand = brand
        self.aliases = aliases or []
        self.domain = domain
        self._keys = {
            "chatgpt": openai_key,
            "perplexity": perplexity_key,
            "google_aio": dfs_auth,
        }

    async def _fetch_answer(self, engine: str, prompt: str) -> tuple[str, list[str]] | None:
        """Return ``(answer_text, cited_urls)`` from a live engine, or ``None`` if
        the engine has no configured key.

        TODO(B1): implement the real calls (async httpx) —
          * chatgpt: OpenAI Responses API with web_search tool
          * perplexity: Perplexity chat/completions (returns citations)
          * google_aio: DataForSEO SERP AI-mode / AI-Overview endpoint
        Until then this returns ``None`` for unconfigured engines and raises for
        configured-but-unimplemented ones so we never fabricate citations.
        """
        if not self._keys.get(engine):
            return None
        raise NotImplementedError(
            f"live probe for {engine!r} not wired yet (B1). Key is set but the "
            "engine call is a stub; see citation_probe.CitationProbe._fetch_answer."
        )

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
