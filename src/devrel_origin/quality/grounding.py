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
1. Split the draft into sentences deterministically (no model: a sentence
   boundary is not a judgment), then select which sentences are factual
   claims via a typed judgment (``Judge.select_claims``).
2. For each claim, gather candidate evidence: KB search hits plus (optionally)
   repo facts. This is deterministic retrieval, no LLM.
3. Verify each claim against its candidates via a typed judgment
   (``Judge.verify_claim``): does the evidence support it, contradict it, or
   say nothing about it? A claim counts as grounded only when the evidence
   supports it at or above ``confidence_min``; a contradiction is always
   flagged, regardless of confidence.

Degradation differs from the anti-slop stage: grounding has no deterministic
fallback. With ``NullJudge`` (no judgment backend configured), the stage does
not substitute a lesser check; it reports ``judged=False``, grounds nothing,
flags nothing, and leaves the text untouched.

The output ``GroundingResult`` is JSON-serializable and feeds the provenance
trail (see ``quality.provenance``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from devrel_origin.quality.judgments import Judge

if TYPE_CHECKING:
    from devrel_origin.core.base import KnowledgeBaseSearch

logger = logging.getLogger(__name__)

CLAIM_PROB_MIN = 0.5


@dataclass(frozen=True)
class Claim:
    """A discrete factual assertion extracted from the draft."""

    text: str
    kind: str  # always "fact": deterministic splitting cannot infer a taxonomy


@dataclass(frozen=True)
class Source:
    """A candidate or confirmed evidence source for a claim."""

    origin: str  # "kb" or "repo"
    ref: str  # KB relative path, or repo fact identifier (e.g. commit sha / "repo_stats")
    excerpt: str  # short supporting snippet


@dataclass
class GroundedClaim:
    """A claim after verification against candidate sources."""

    claim: Claim
    grounded: bool
    sources: list[Source] = field(default_factory=list)
    reason: str = ""


@dataclass
class GroundingResult:
    """Outcome of the grounding stage over one draft."""

    total_claims: int
    grounded_claims: int
    flagged: list[GroundedClaim]  # unsourced or contradicted claims
    grounded: list[GroundedClaim]  # sourced claims (with citations)
    cut_applied: bool  # whether unsourced claims were removed from the text
    text_after: str
    judged: bool = True  # False when no judgment backend was available

    def to_dict(self) -> dict:
        """JSON-serializable view for the provenance trail."""
        return {
            "total_claims": self.total_claims,
            "grounded_claims": self.grounded_claims,
            "flagged_count": len(self.flagged),
            "cut_applied": self.cut_applied,
            "judged": self.judged,
            "flagged": [_grounded_claim_dict(c) for c in self.flagged],
            "grounded": [_grounded_claim_dict(c) for c in self.grounded],
        }


def _grounded_claim_dict(gc: GroundedClaim) -> dict:
    d = asdict(gc)
    return d


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    """Deterministic split. No model: a sentence boundary is not a judgment."""
    parts = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    return [p for p in parts if len(p.split()) >= 4]


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


async def _verify(
    claim: Claim, candidates: list[Source], judge: Judge, confidence_min: float
) -> GroundedClaim:
    if not candidates:
        return GroundedClaim(
            claim=claim, grounded=False, sources=[], reason="No candidate sources in repo or KB."
        )
    evidence = "\n".join(f"({s.origin}:{s.ref}) {s.excerpt}" for s in candidates)
    verdict = await judge.verify_claim(claim=claim.text, evidence=evidence)
    if not verdict.available:
        return GroundedClaim(claim=claim, grounded=False, sources=[], reason="Judgment skipped.")
    if verdict.relation == "contradicts":
        return GroundedClaim(
            claim=claim,
            grounded=False,
            sources=candidates,
            reason=f"Contradicted by the evidence (confidence {verdict.confidence:.2f}).",
        )
    if verdict.relation == "supports" and verdict.confidence >= confidence_min:
        return GroundedClaim(claim=claim, grounded=True, sources=candidates, reason="")
    return GroundedClaim(
        claim=claim,
        grounded=False,
        sources=[],
        reason=(
            f"Not established: {verdict.relation} at confidence {verdict.confidence:.2f}, "
            f"below {confidence_min:.2f}."
        ),
    )


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
    judge: Judge,
    repo_facts: list[dict] | None = None,
    cut_unsourced: bool = False,
    confidence_min: float = 0.65,
) -> GroundingResult:
    """Split ``text`` into claims and verify each against the KB and
    (optionally) repo facts, using ``judge`` for both claim selection and
    verification.

    Args:
        text: The draft to ground.
        kb: A ``KnowledgeBaseSearch`` over the project's harvested KB.
        judge: The judgment port. With an unavailable judge (``NullJudge``),
            the stage does not run: it returns ``judged=False`` and leaves
            ``text`` untouched.
        repo_facts: Pre-fetched repo facts (commits / stats) as dicts with
            ``ref`` and ``excerpt``. Fetch once via ``github_tools`` and reuse.
        cut_unsourced: When True, delete flagged (unsourced) claim sentences
            from the returned text. When False, the text is untouched and the
            claims are only flagged.
        confidence_min: Minimum confidence for a "supports" verdict to count
            as grounded.

    Returns:
        A JSON-serializable ``GroundingResult``.
    """
    if not judge.available:
        return GroundingResult(
            total_claims=0,
            grounded_claims=0,
            flagged=[],
            grounded=[],
            cut_applied=False,
            text_after=text,
            judged=False,
        )

    sentences = _split_sentences(text)
    probs = await judge.select_claims(sentences=sentences)
    claims = [
        Claim(text=s, kind="fact")
        for s, p in zip(sentences, probs, strict=True)
        if p >= CLAIM_PROB_MIN
    ]

    repo_sources = _repo_facts_to_sources(repo_facts)
    grounded: list[GroundedClaim] = []
    flagged: list[GroundedClaim] = []
    for claim in claims:
        candidates = _kb_candidates(claim, kb) + repo_sources
        gc = await _verify(claim, candidates, judge, confidence_min)
        (grounded if gc.grounded else flagged).append(gc)

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
            "backend": judge.backend,
        },
    )
    return GroundingResult(
        total_claims=len(claims),
        grounded_claims=len(grounded),
        flagged=flagged,
        grounded=grounded,
        cut_applied=cut_applied,
        text_after=text_after,
        judged=True,
    )
