"""Grounding stage: turn anti-slop into a provable guarantee.

Free "AI slop" linters only strip patterns. The moat here is grounding: every
factual claim in a high-value draft must resolve to a source in the project's
OWN repo (facts via ``github_tools``: commits / API / repo stats) and/or its
harvested knowledge base (via TF-IDF ``KnowledgeBaseSearch``). Claims that do
not resolve are FLAGGED (and optionally cut), never silently smoothed over.

Pipeline placement: this runs as an OPTIONAL stage in ``editorial.run_pipeline``,
gated behind ``ground=False`` by default because it adds latency and cost. Turn
it on for hero / CTA / landing-page artifacts where an unsourced claim is
expensive.

Three steps, mirroring the anti-slop stage's shape:
1. Extract discrete factual claims from the draft (one Haiku call, strict JSON).
   Pure opinion / instructions / code are not claims and are skipped.
2. For each claim, gather candidate evidence: KB search hits plus (optionally)
   repo facts. This is deterministic retrieval, no LLM.
3. Adjudicate each claim against its candidates (one Haiku call per claim):
   does any candidate actually support it? Supported claims cite their source;
   unsupported claims are flagged as unsourced.

The output ``GroundingResult`` is JSON-serializable and feeds the provenance
trail (see ``quality.provenance``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devrel_origin.core.base import KnowledgeBaseSearch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Claim:
    """A discrete factual assertion extracted from the draft."""

    text: str
    kind: str  # "metric", "capability", "comparison", "fact"


@dataclass(frozen=True)
class Source:
    """A candidate or confirmed evidence source for a claim."""

    origin: str  # "kb" or "repo"
    ref: str  # KB relative path, or repo fact identifier (e.g. commit sha / "repo_stats")
    excerpt: str  # short supporting snippet


@dataclass
class GroundedClaim:
    """A claim after adjudication against candidate sources."""

    claim: Claim
    grounded: bool
    sources: list[Source] = field(default_factory=list)
    reason: str = ""


@dataclass
class GroundingResult:
    """Outcome of the grounding stage over one draft."""

    total_claims: int
    grounded_claims: int
    flagged: list[GroundedClaim]  # unsourced claims
    grounded: list[GroundedClaim]  # sourced claims (with citations)
    cut_applied: bool  # whether unsourced claims were removed from the text
    text_after: str

    def to_dict(self) -> dict:
        """JSON-serializable view for the provenance trail."""
        return {
            "total_claims": self.total_claims,
            "grounded_claims": self.grounded_claims,
            "flagged_count": len(self.flagged),
            "cut_applied": self.cut_applied,
            "flagged": [_grounded_claim_dict(c) for c in self.flagged],
            "grounded": [_grounded_claim_dict(c) for c in self.grounded],
        }


def _grounded_claim_dict(gc: GroundedClaim) -> dict:
    d = asdict(gc)
    return d


_EXTRACT_SYSTEM = (
    "You extract discrete, checkable factual claims from marketing / developer "
    "content. A claim is an assertion that could be TRUE or FALSE about the "
    "product: a metric ('cuts build time 40%'), a capability ('supports "
    "OpenTelemetry'), a comparison ('faster than X'), or a concrete fact. "
    "Opinions, calls-to-action, instructions, headings, and code are NOT claims. "
    "Return strict JSON: a list of objects with 'text' (the claim, quoted from "
    "the draft) and 'kind' (one of: metric, capability, comparison, fact). "
    "Return [] if there are no checkable claims. No prose, no markdown fences."
)


def _coerce_claims(raw: str) -> list[Claim]:
    """Parse the extractor's JSON into Claim objects. Tolerant of fences /
    junk: on any parse failure, return an empty list so grounding degrades to
    a no-op rather than crashing the pipeline."""
    text = raw.strip()
    if text.startswith("```"):
        # Strip a leading/trailing fence if the model added one.
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.info("grounding_extract_unparseable", extra={"raw_head": raw[:120]})
        return []
    if not isinstance(data, list):
        return []
    claims: list[Claim] = []
    valid_kinds = {"metric", "capability", "comparison", "fact"}
    for item in data:
        if not isinstance(item, dict):
            continue
        ctext = str(item.get("text", "")).strip()
        if not ctext:
            continue
        kind = str(item.get("kind", "fact")).strip().lower()
        if kind not in valid_kinds:
            kind = "fact"
        claims.append(Claim(text=ctext, kind=kind))
    return claims


async def _extract_claims(text: str, llm_client) -> list[Claim]:
    raw = await llm_client.generate(
        system_prompt=_EXTRACT_SYSTEM,
        user_prompt="Draft:\n\n" + text,
        model="haiku",
    )
    return _coerce_claims(raw)


def _kb_candidates(claim: Claim, kb: KnowledgeBaseSearch, limit: int = 3) -> list[Source]:
    """Deterministic KB retrieval for one claim. Only real matches (relevance
    > 0); the KB's padding fallback is suppressed so we never present an
    unrelated doc as evidence."""
    hits = kb.search(claim.text, limit=limit, content_truncate=600, pad_with_remaining=False)
    return [
        Source(origin="kb", ref=h["source"], excerpt=h["content"][:400])
        for h in hits
        if h.get("relevance", 0) > 0
    ]


def _repo_facts_to_sources(repo_facts: list[dict] | None) -> list[Source]:
    """Convert pre-fetched repo facts (commits / stats) into candidate Sources.

    Repo facts are fetched once by the caller (they are draft-independent) and
    passed in, so grounding many claims does not re-hit the GitHub API. Each
    fact dict carries 'ref' and 'excerpt'."""
    if not repo_facts:
        return []
    out: list[Source] = []
    for fact in repo_facts:
        ref = str(fact.get("ref", "")).strip()
        excerpt = str(fact.get("excerpt", "")).strip()
        if ref and excerpt:
            out.append(Source(origin="repo", ref=ref, excerpt=excerpt[:400]))
    return out


_ADJUDICATE_SYSTEM = (
    "You are a fact-checker. Given a CLAIM and a list of candidate SOURCES "
    "(excerpts from the product's own repo and knowledge base), decide whether "
    "any source actually SUPPORTS the claim. Be strict: a source supports a "
    "claim only if it states or directly implies it. Topical overlap is NOT "
    'support. Return strict JSON: {"grounded": true|false, "source_indexes": '
    '[0-based ints of supporting sources], "reason": "one sentence"}. '
    "No prose outside the JSON, no markdown fences."
)


def _coerce_adjudication(raw: str, candidates: list[Source]) -> tuple[bool, list[Source], str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False, [], "Could not parse fact-checker response."
    if not isinstance(data, dict):
        return False, [], "Fact-checker returned a non-object."
    grounded = bool(data.get("grounded", False))
    idxs = data.get("source_indexes", []) or []
    picked: list[Source] = []
    if isinstance(idxs, list):
        for i in idxs:
            if isinstance(i, int) and 0 <= i < len(candidates):
                picked.append(candidates[i])
    reason = str(data.get("reason", "")).strip()
    # A "grounded" verdict with no cited source is not provable; downgrade it.
    if grounded and not picked:
        return False, [], reason or "Marked grounded but cited no source."
    return grounded, picked, reason


async def _adjudicate(claim: Claim, candidates: list[Source], llm_client) -> GroundedClaim:
    if not candidates:
        return GroundedClaim(
            claim=claim,
            grounded=False,
            sources=[],
            reason="No candidate sources in repo or KB.",
        )
    listing = "\n".join(f"[{i}] ({s.origin}:{s.ref}) {s.excerpt}" for i, s in enumerate(candidates))
    user = (
        "CLAIM:\n" + claim.text + "\n\n"
        "CANDIDATE SOURCES:\n" + listing + "\n\n"
        "Which sources, if any, support the claim?"
    )
    raw = await llm_client.generate(
        system_prompt=_ADJUDICATE_SYSTEM,
        user_prompt=user,
        model="haiku",
    )
    grounded, picked, reason = _coerce_adjudication(raw, candidates)
    return GroundedClaim(claim=claim, grounded=grounded, sources=picked, reason=reason)


def _cut_flagged(text: str, flagged: list[GroundedClaim]) -> str:
    """Remove unsourced claim sentences from the text (best-effort, exact
    substring). Pure and deterministic: we never rewrite, we only delete the
    offending assertion so nothing unprovable ships. Whitespace is tidied."""
    out = text
    for gc in flagged:
        needle = gc.claim.text
        if needle and needle in out:
            out = out.replace(needle, "")
    # Collapse the gaps a deletion can leave behind.
    lines = [ln.rstrip() for ln in out.splitlines()]
    cleaned: list[str] = []
    blank = False
    for ln in lines:
        if ln.strip() == "":
            if not blank:
                cleaned.append("")
            blank = True
        else:
            cleaned.append(ln)
            blank = False
    return "\n".join(cleaned).strip() + ("\n" if text.endswith("\n") else "")


async def ground_claims(
    *,
    text: str,
    kb: KnowledgeBaseSearch,
    llm_client,
    repo_facts: list[dict] | None = None,
    cut_unsourced: bool = False,
) -> GroundingResult:
    """Extract factual claims from ``text`` and verify each against the KB and
    (optionally) repo facts.

    Args:
        text: The draft to ground.
        kb: A ``KnowledgeBaseSearch`` over the project's harvested KB.
        llm_client: LLM client (uses Haiku for extraction + adjudication).
        repo_facts: Pre-fetched repo facts (commits / stats) as dicts with
            ``ref`` and ``excerpt``. Fetch once via ``github_tools`` and reuse.
        cut_unsourced: When True, delete flagged (unsourced) claim sentences
            from the returned text. When False, the text is untouched and the
            claims are only flagged.

    Returns:
        A JSON-serializable ``GroundingResult``.
    """
    claims = await _extract_claims(text, llm_client)
    repo_sources = _repo_facts_to_sources(repo_facts)

    grounded: list[GroundedClaim] = []
    flagged: list[GroundedClaim] = []
    for claim in claims:
        candidates = _kb_candidates(claim, kb) + repo_sources
        gc = await _adjudicate(claim, candidates, llm_client)
        if gc.grounded:
            grounded.append(gc)
        else:
            flagged.append(gc)

    cut_applied = False
    text_after = text
    if cut_unsourced and flagged:
        text_after = _cut_flagged(text, flagged)
        cut_applied = True

    logger.info(
        "grounding_complete",
        extra={
            "total": len(claims),
            "grounded": len(grounded),
            "flagged": len(flagged),
            "cut_applied": cut_applied,
        },
    )
    return GroundingResult(
        total_claims=len(claims),
        grounded_claims=len(grounded),
        flagged=flagged,
        grounded=grounded,
        cut_applied=cut_applied,
        text_after=text_after,
    )
