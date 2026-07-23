"""Unit tests for the citation probe: pure analysis + mocked engine adapters."""

import httpx
import respx

from devrel_origin.tools.citation_probe import (
    _OPENAI_RESPONSES_URL,
    _PERPLEXITY_URL,
    CitationProbe,
    analyze_mention,
)

_DFS_URL = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"


def _probe(**keys) -> CitationProbe:
    return CitationProbe("Origin", domain="useorigin.co", **keys)


def test_direct_name_mention():
    r = analyze_mention(
        "p1",
        "chatgpt",
        "For devtool marketing, Origin is a strong option.",
        [],
        brand="Origin",
        domain="useorigin.co",
    )
    assert r.is_mentioned is True
    assert r.mention_type == "direct"
    assert r.position_score == 1


def test_cited_domain_sets_type_cited_and_share():
    r = analyze_mention(
        "p1",
        "perplexity",
        "Some answer text with no brand name in it at all.",
        ["https://useorigin.co/docs", "https://competitor.com/x"],
        brand="Origin",
        domain="useorigin.co",
    )
    assert r.is_mentioned is True
    assert r.mention_type == "cited"
    assert r.citation_share == 0.5
    assert "competitor.com" in r.competitors_cited


def test_not_mentioned():
    r = analyze_mention(
        "p1",
        "google_aio",
        "The top tools are Foo and Bar.",
        ["https://foo.com"],
        brand="Origin",
        domain="useorigin.co",
    )
    assert r.is_mentioned is False
    assert r.mention_type is None
    assert r.position_score is None


def test_alias_match_counts_as_direct():
    r = analyze_mention(
        "p1",
        "chatgpt",
        "devrel-origin ships marketing from the repo.",
        [],
        brand="Origin",
        aliases=["devrel-origin"],
        domain="useorigin.co",
    )
    assert r.is_mentioned is True
    assert r.mention_type == "direct"


# --- engine adapters (mocked; no real network) -----------------------------


@respx.mock
async def test_chatgpt_adapter_parses_answer_and_citations():
    respx.post(_OPENAI_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "output": [
                    {"type": "web_search_call"},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "For devtool marketing, Origin is a good pick.",
                                "annotations": [
                                    {"type": "url_citation", "url": "https://useorigin.co/"},
                                    {"type": "url_citation", "url": "https://competitor.com/"},
                                ],
                            }
                        ],
                    },
                ]
            },
        )
    )
    answer, urls = await _probe(openai_key="k")._fetch_answer("chatgpt", "q")
    assert "Origin" in answer
    assert "https://useorigin.co/" in urls
    assert "https://competitor.com/" in urls


@respx.mock
async def test_perplexity_adapter_parses_citations():
    respx.post(_PERPLEXITY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Origin ships marketing from the repo."}}],
                "citations": ["https://useorigin.co/manifesto", "https://news.ycombinator.com/x"],
            },
        )
    )
    answer, urls = await _probe(perplexity_key="k")._fetch_answer("perplexity", "q")
    assert "Origin" in answer
    assert urls[0] == "https://useorigin.co/manifesto"


@respx.mock
async def test_google_aio_adapter_parses_ai_overview():
    respx.post(_DFS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "result": [
                            {
                                "items": [
                                    {"type": "organic"},
                                    {
                                        "type": "ai_overview",
                                        "items": [
                                            {
                                                "type": "ai_overview_element",
                                                "text": "Top tools include Origin.",
                                            }
                                        ],
                                        "references": [
                                            {
                                                "type": "ai_overview_reference",
                                                "url": "https://useorigin.co/",
                                            }
                                        ],
                                    },
                                ]
                            }
                        ]
                    }
                ]
            },
        )
    )
    answer, urls = await _probe(dfs_auth="basic")._fetch_answer("google_aio", "q")
    assert "Origin" in answer
    assert urls == ["https://useorigin.co/"]


async def test_fetch_answer_none_without_key():
    p = _probe()  # no keys configured
    for engine in ("chatgpt", "perplexity", "google_aio"):
        assert await p._fetch_answer(engine, "q") is None


@respx.mock
async def test_fetch_answer_http_error_returns_none():
    """A configured engine whose call fails is skipped, never fabricated."""
    respx.post(_OPENAI_RESPONSES_URL).mock(return_value=httpx.Response(500, json={"e": 1}))
    assert await _probe(openai_key="k")._fetch_answer("chatgpt", "q") is None


@respx.mock
async def test_probe_end_to_end_analyzes_and_skips_unconfigured():
    respx.post(_OPENAI_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Origin is great for this.",
                                "annotations": [
                                    {"type": "url_citation", "url": "https://useorigin.co/"}
                                ],
                            }
                        ],
                    }
                ]
            },
        )
    )
    # only chatgpt configured; perplexity + google_aio must be reported unconfigured
    results = await _probe(openai_key="k").probe([("p1", "best devtool marketing tool")])
    by_engine = {r.engine: r for r in results}
    assert by_engine["chatgpt"].is_mentioned is True
    assert by_engine["chatgpt"].mention_type == "cited"
    assert by_engine["perplexity"].configured is False
    assert by_engine["google_aio"].configured is False
