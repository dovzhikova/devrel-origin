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

Backend selection, tried in this order:
1. A typed judgment backend (``Judge.available`` is True): sentences are split
   deterministically, claim selection and verification both go through the
   typed judgment port (``Judge.select_claims`` / ``Judge.verify_claim``).
   ``llm_client`` is not consulted while this path succeeds. If claim
   selection itself fails entirely (``select_claims`` returns ``None``: any
   request error, including a partial failure of a chunked call), that is
   NOT "zero claims" and does not render a pass; it falls through to step 2.
2. Otherwise (no typed judge, or its claim selection just failed), an
   ``llm_client``: Haiku extracts claims and adjudicates each one against
   candidate sources with two prompted calls per claim. If Haiku's own claim
   extraction reply cannot be parsed, that is also NOT "zero claims"; the
   draft is reported ``judged=False``, exactly like step 3.
3. Otherwise: the stage does not run. It reports ``judged=False``, grounds
   nothing, flags nothing, and leaves the text untouched.

"Not judged" is a third state, distinct from "pass" and from "flagged," at
both the per-claim and the per-draft level. A single claim can also come back
unjudged even when the stage as a whole ran: a verdict can be unavailable for
one call (a transient backend failure, or a Haiku reply that cannot be
parsed) while the stage overall did run. That claim goes into
``GroundingResult.skipped``, never into ``flagged``, and is never cut by
``cut_unsourced`` (a skip is not evidence the claim is wrong; deleting it on
a hiccup would be worse than leaving it flagged for a human).

The output ``GroundingResult`` is JSON-serializable and feeds the provenance
trail (see ``quality.provenance``).
"""

from __future__ import annotations

import json
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
    kind: str  # "metric", "capability", "comparison", or "fact"


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
    judged: bool = True  # False when no backend was available at all (stage-level)
    skipped: list[GroundedClaim] = field(default_factory=list)  # per-claim: verdict unavailable
    backend: str = "none"  # "typesafe", "haiku", or "none"

    def to_dict(self) -> dict:
        """JSON-serializable view for the provenance trail."""
        return {
            "total_claims": self.total_claims,
            "grounded_claims": self.grounded_claims,
            "flagged_count": len(self.flagged),
            "skipped_count": len(self.skipped),
            "cut_applied": self.cut_applied,
            "judged": self.judged,
            "backend": self.backend,
            "flagged": [_grounded_claim_dict(c) for c in self.flagged],
            "grounded": [_grounded_claim_dict(c) for c in self.grounded],
            "skipped": [_grounded_claim_dict(c) for c in self.skipped],
        }


def _grounded_claim_dict(gc: GroundedClaim) -> dict:
    d = asdict(gc)
    return d


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    """Deterministic split. No model: a sentence boundary is not a judgment."""
    parts = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    return [p for p in parts if len(p.split()) >= 4]


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


def _coerce_claims(raw: str) -> list[Claim] | None:
    """Parse the extractor's JSON into Claim objects. Tolerant of fences.

    Returns ``None`` when the reply could not be interpreted as a claims
    list at all (a JSON parse failure, or valid JSON that is not a list): an
    explicit "could not parse" signal, distinct from a valid empty list
    (``[]``), which means the model genuinely found no checkable claims. The
    caller must not treat the two the same, or a parse failure silently
    reads as "zero claims, nothing to flag."""
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
        return None
    if not isinstance(data, list):
        return None
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


async def _extract_claims(text: str, llm_client) -> list[Claim] | None:
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


# Which bucket a verified claim belongs in: "grounded", "flagged", or
# "skipped" (verdict unavailable: not evidence either way, never cut).
_VerifyBucket = str


async def _verify(
    claim: Claim, candidates: list[Source], judge: Judge, confidence_min: float
) -> tuple[_VerifyBucket, GroundedClaim]:
    if not candidates:
        return "flagged", GroundedClaim(
            claim=claim, grounded=False, sources=[], reason="No candidate sources in repo or KB."
        )
    evidence = "\n".join(f"({s.origin}:{s.ref}) {s.excerpt}" for s in candidates)
    verdict = await judge.verify_claim(claim=claim.text, evidence=evidence)
    if not verdict.available:
        return "skipped", GroundedClaim(
            claim=claim, grounded=False, sources=[], reason="Judgment skipped."
        )
    if verdict.relation == "contradicts":
        return "flagged", GroundedClaim(
            claim=claim,
            grounded=False,
            sources=candidates,
            reason=f"Contradicted by the evidence (confidence {verdict.confidence:.2f}).",
        )
    if verdict.relation == "supports" and verdict.confidence >= confidence_min:
        return "grounded", GroundedClaim(claim=claim, grounded=True, sources=candidates, reason="")
    return "flagged", GroundedClaim(
        claim=claim,
        grounded=False,
        sources=[],
        reason=(
            f"Not established: {verdict.relation} at confidence {verdict.confidence:.2f}, "
            f"below {confidence_min:.2f}."
        ),
    )


_ADJUDICATE_SYSTEM = (
    "You are a fact-checker. Given a CLAIM and a list of candidate SOURCES "
    "(excerpts from the product's own repo and knowledge base), decide whether "
    "any source actually SUPPORTS the claim. Be strict: a source supports a "
    "claim only if it states or directly implies it. Topical overlap is NOT "
    'support. Return strict JSON: {"grounded": true|false, "source_indexes": '
    '[0-based ints of supporting sources], "reason": "one sentence"}. '
    "No prose outside the JSON, no markdown fences."
)


def _coerce_adjudication(
    raw: str, candidates: list[Source]
) -> tuple[bool | None, list[Source], str]:
    """Parse the fact-checker's JSON reply.

    Returns ``(grounded, picked_sources, reason)``. ``grounded`` is an
    explicit three-state signal, never inferred from the reason text: ``None``
    means the reply could not be parsed at all (a skip, never a flag);
    ``True``/``False`` means it parsed and rendered a verdict.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, [], "Could not parse fact-checker response."
    if not isinstance(data, dict):
        return None, [], "Fact-checker returned a non-object."
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


async def _adjudicate(
    claim: Claim, candidates: list[Source], llm_client
) -> tuple[_VerifyBucket, GroundedClaim]:
    if not candidates:
        return "flagged", GroundedClaim(
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
    if grounded is None:
        # A skip, never a flag: a skipped judgment must never read as a pass,
        # and cut_unsourced can never delete a sentence over a malformed reply.
        return "skipped", GroundedClaim(claim=claim, grounded=False, sources=[], reason=reason)
    if grounded:
        return "grounded", GroundedClaim(claim=claim, grounded=True, sources=picked, reason=reason)
    return "flagged", GroundedClaim(claim=claim, grounded=False, sources=picked, reason=reason)


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
    llm_client=None,
    repo_facts: list[dict] | None = None,
    cut_unsourced: bool = False,
    confidence_min: float = 0.65,
) -> GroundingResult:
    """Split ``text`` into claims and verify each against the KB and
    (optionally) repo facts.

    Args:
        text: The draft to ground.
        kb: A ``KnowledgeBaseSearch`` over the project's harvested KB.
        judge: The typed judgment port. When ``judge.available`` is True, this
            path is used and ``llm_client`` is ignored entirely.
        llm_client: Fallback LLM client (Haiku), used when ``judge`` is
            unavailable, or when it is available but its claim selection
            fails entirely. When neither an available judge's selection nor
            ``llm_client`` produces a usable result (including an
            unparseable Haiku extraction), the stage does not run: it
            returns ``judged=False`` and leaves ``text`` untouched.
        repo_facts: Pre-fetched repo facts (commits / stats) as dicts with
            ``ref`` and ``excerpt``. Fetch once via ``github_tools`` and reuse.
        cut_unsourced: When True, delete flagged (unsourced) claim sentences
            from the returned text. When False, the text is untouched and the
            claims are only flagged. Skipped claims (verdict unavailable) are
            never cut, regardless of this flag.
        confidence_min: Minimum confidence for a "supports" verdict to count
            as grounded. Only consulted on the typed judgment path.

    Returns:
        A JSON-serializable ``GroundingResult``.
    """
    grounded: list[GroundedClaim] = []
    flagged: list[GroundedClaim] = []
    skipped: list[GroundedClaim] = []
    repo_sources = _repo_facts_to_sources(repo_facts)

    backend: str | None = None
    claims: list[Claim] = []

    if judge.available:
        sentences = _split_sentences(text)
        probs = await judge.select_claims(sentences=sentences)
        if probs is not None:
            backend = "typesafe"
            claims = [
                Claim(text=s, kind="fact")
                for s, p in zip(sentences, probs, strict=True)
                if p >= CLAIM_PROB_MIN
            ]
            for claim in claims:
                candidates = _kb_candidates(claim, kb) + repo_sources
                bucket, gc = await _verify(claim, candidates, judge, confidence_min)
                if bucket == "grounded":
                    grounded.append(gc)
                elif bucket == "skipped":
                    skipped.append(gc)
                else:
                    flagged.append(gc)
        # else: the typed claim selector failed entirely (any request error).
        # That is not "zero claims": fall through to Haiku (spec order:
        # TypeSafe, else Haiku, else skipped), never treat it as a pass.

    if backend is None and llm_client is not None:
        backend = "haiku"
        extracted = await _extract_claims(text, llm_client)
        if extracted is None:
            # The extractor's reply could not be parsed: this is "we could
            # not judge," not "there were no claims." The stage ran (Haiku
            # was asked) but produced nothing usable, so the draft as a
            # whole is not judged, exactly like a total backend failure.
            return GroundingResult(
                total_claims=0,
                grounded_claims=0,
                flagged=[],
                grounded=[],
                cut_applied=False,
                text_after=text,
                judged=False,
                backend=backend,
            )
        claims = extracted
        for claim in claims:
            candidates = _kb_candidates(claim, kb) + repo_sources
            bucket, gc = await _adjudicate(claim, candidates, llm_client)
            if bucket == "grounded":
                grounded.append(gc)
            elif bucket == "skipped":
                skipped.append(gc)
            else:
                flagged.append(gc)

    if backend is None:
        # Neither backend produced a usable result: a failed TypeSafe
        # selection with no llm_client to fall back to, or no backend at
        # all. `judged=False` must never read as a pass.
        return GroundingResult(
            total_claims=0,
            grounded_claims=0,
            flagged=[],
            grounded=[],
            cut_applied=False,
            text_after=text,
            judged=False,
            backend="none",
        )

    # cut_unsourced only ever touches `flagged`; a skipped claim is not
    # evidence the claim is wrong, so it is never a candidate for deletion.
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
            "skipped": len(skipped),
            "cut_applied": cut_applied,
            "backend": backend,
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
        skipped=skipped,
        backend=backend,
    )
