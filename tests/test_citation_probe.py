"""Unit tests for the pure citation-analysis layer (no network)."""

from devrel_origin.tools.citation_probe import analyze_mention


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
