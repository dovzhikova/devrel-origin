# TypeSafe judgments in the quality pipeline and release gates: design spec

**Status:** Draft, awaiting review (2026-09-20)
**Author:** Daria Dovzhikova
**Scope:** `quality/` (lands independently) plus the release gates (blocked on `feat/release-loop`)
**Related:** `docs/cli/release.md` (once the release loop lands), `quality/editorial.py`
**Reference:** `petergyang/no-ai-slop` (MIT) for the slop taxonomy, see section 10

## 1. Why this exists

Origin's editorial pipeline is eight stages, and most of them work the same way:
send prose to an LLM, get prose back, parse the prose into a decision. Two of
those stages carry defects that exist because of that round trip, both recorded
in Origin's own source:

- `quality/slop.py:113` (`llm_lint`) asks Haiku for a list of offending phrases.
  The module docstring records it returning phrases that are absent from the
  draft ("verbatim from a real abort: 'replace this blockquote'"), which are then
  fed to `force_rewrite` and trip the abort-loud re-check. `_verify_lint_hits`
  (`slop.py:83`) exists purely to filter out the model's inventions.
- `quality/grounding.py` parses two free-text responses back into decisions:
  `_coerce_claims` (`:107`) and `_coerce_adjudication` (`:188`), the latter
  stripping code fences, guarding against non-objects, and downgrading a
  "grounded" verdict that cites no source.

The release loop has a third, conceded in its own documentation: the source-link
gate proves each citation resolves and that a draft has at least one, and "it
cannot prove every sentence is cited." Anti-slop is the product's central claim,
and today it rests on a regex blocklist plus a link check.

TypeSafe System One returns typed judgments with probabilities instead of prose.
A typed answer cannot hallucinate a phrase, and it needs no parser. This spec
replaces the judgment layer, not the generation layer.

## 2. Decisions already taken

1. **Optional extra with graceful degradation.** Installed as
   `pip install 'devrel-origin[typesafe]'`, matching the existing `video`, `seo`
   and `geo-google` extras. Without the extra or without `TYPESAFE_API_KEY`, the
   gates fall back to today's deterministic checks and say so. Origin runs on the
   customer's machine and in their CI, so requiring a second signup before a
   first draft was rejected, as was proxying through an Origin-hosted service.
2. **A judgment port, not inline SDK calls.** One module owns the typed calls and
   the stages receive it the way they receive `llm_client` today.
   - Rejected: SDK calls inline in each stage. Key detection, fallback and the
     evaluation harness would be duplicated across `slop.py`, `grounding.py` and
     the release gates, and Phase 0 could not drive the judgments without booting
     a pipeline.
   - Rejected: a `judge()` method on `LLMClient`. Budget, agent context and the
     cost sink are already there, but that class is 300+ lines with an
     Anthropic-keyed price table, and "generate prose" and "return a typed
     judgment" are different contracts.

## 3. Non-goals

- Replacing generation. Drafting blog, social and email copy stays an LLM job.
- Replacing `quality/readability.py`. It is deterministic and correct as is.
- Removing the regex blocklist. It is free, deterministic, and it is what runs
  when no key is present.
- Building the release loop. Phases 3 and 4 attach to code that arrives with
  `origin-release-loop.patch`.
- Any claim in README or marketing copy beyond what the Phase 0 numbers support.

## 4. Architecture

### 4.1 The port

New module `src/devrel_origin/quality/judgments.py`:

```python
class Judge(Protocol):
    async def score_slop(self, *, text: str, voice: str) -> SlopVerdict: ...
    async def verify_claim(self, *, claim: str, evidence: str) -> ClaimVerdict: ...
    async def rank_commits(self, commits: list[CommitRef]) -> list[CommitVerdict]: ...
```

Each verdict is a frozen dataclass carrying the answer, its probability or
confidence, and `backend: str`. Two implementations:

- `TypeSafeJudge`, backed by the Python SDK.
- `NullJudge`, which returns `available=False` and never a fabricated verdict.

`build_judge(paths) -> Judge` selects TypeSafe only when the SDK imports **and**
`TYPESAFE_API_KEY` is set, otherwise `NullJudge`. It never raises: a missing
extra, a missing key and an unreachable service all degrade to `NullJudge`.

### 4.2 The degradation contract

A caller must be able to distinguish "judged and passed" from "not judged."
`GateReport` and `gates.json` gain `judged: bool` and the backend name, and a
skipped judgment renders as `skipped`, never as a pass. A gate that silently
degraded and still printed green would be worse than no gate.

Runtime failure of the TypeSafe backend mid-run is treated as `skipped` for that
item, not as a pass and not as a crash, and the count of skipped items appears in
the gate output.

### 4.3 Cost

The port emits usage through the existing `make_sqlite_sink(state_db)` so
`devrel cost` sees judgment spend, using the sink's `(agent, model, usage)`
signature with model `typesafe:jev`. TypeSafe's price per token is not published,
so `_compute_cost_usd` must not silently return `0.0` for it: tokens are
recorded, cost is stored as unknown, and `devrel cost` prints `n/a` rather than a
false `$0.00`.

### 4.4 Cache and invalidation

Judgments are cached per item keyed by `(question_version, content_hash)`, the
pattern already proven on AgenticCareers where a stale reuse would have silently
served old labels. Bumping `QUESTION_VERSION` invalidates everything.

## 5. The judgments

Written and frozen before reading any of the evaluation corpus.

### 5.1 Slop: named patterns, not a score

Replaces `llm_lint`. The decomposition follows `petergyang/no-ai-slop` (MIT, see
section 10), which splits slop into three kinds that need three different
mechanisms. Origin currently treats all three with one regex list, which is the
root of the `enforce_slop=False` compromise.

**Tier 1, words that are almost never legitimate.** "delve", "tapestry",
"leverage", "utilize", "robust", "cutting-edge", "paradigm shift",
"ever-evolving". Deterministic regex, no model, no key. This is what runs in the
degraded path, and Origin's starter blocklist should adopt this list, which is
sharper than its current 32 entries.

**Tier 2, words that are legitimate about half the time.** "just", "simply",
"actually", "truly", "honestly", "it's worth noting", "at the end of the day".
The reference's rule is explicit: "Cut them when they add nothing. Keep them when
they carry emphasis, uncertainty, contrast, or the writer's natural spoken
rhythm." A regex cannot make that call, which is precisely why Origin's list
contains "very" and "really" and therefore had to be switched off for changelogs.
One Noul per occurrence: does this occurrence carry emphasis, uncertainty or
contrast, or could it be deleted without loss?

**Tier 3, structural patterns, where the real value is.** Binary contrasts ("It's
not X. It's Y."), throat-clearing openers, faux-insight setups ("what nobody
tells you"), colon reveals, importance puffery, weasel attribution ("experts
agree"), interpretive metadiscourse, synonym cycling, dramatic fragmentation,
fake-profound kickers, summary-recap endings, formatting slop. No regex sees any
of these. Code splits the draft into units and asks one Choice per unit over a
closed pattern vocabulary plus `none`.

**Named patterns rather than a register score, and this is the important
decision.** The reference argues the case directly: "Do not rewrite, score the
draft, or guess whether AI wrote it. AI detectors guess. Named patterns are
evidence the user can check." That fits Origin's model better than a number
does. The release loop's output is a pull request, so "colon reveal, line 14,
here is the line" becomes a review comment a human can verify or dismiss, while
"register 2.1" is not actionable and invites arguing with a number.

It also kills the original defect by construction. `llm_lint` hallucinated
phrases because it was asked to *return* phrases. A Choice returns one label from
a closed set; the quoted line is located by code from the unit offsets that
`SlopHit` already carries. The model cannot name a line that is not there.

`ai_register` survives only as a secondary whole-draft Score, measured in Phase 0
and kept only if it earns a place, for example as a cheap triage that decides
whether per-unit judging is worth spending.

**Behaviour change this forces.** `force_rewrite` currently consumes a flat list
of phrases. It now receives named patterns with their quoted lines, which is
strictly better input for a targeted rewrite, plus the Tier 1 regex hits, which
are real by construction. It is not a drop-in replacement and `slop.py`'s tests
change with it.

**Cost this forces.** Per-unit judging is many judgments per draft rather than
one. A 1,500-word post is roughly 60 sentences, and at the AgenticCareers
calibration (~1,660 in / 400 out per item) that is material. Three mitigations,
to be chosen on Phase 0 evidence rather than now: judge paragraphs instead of
sentences, batch independent questions into one request, or gate per-unit
judging behind the cheap whole-draft Score.

**Second benefit.** The release loop disables the slop gate for release notes and
changelog (`enforce_slop=False`) because the blocklist trips on the team's own
commit subjects, which legitimately contain words like "very" and "really." A
semantic Score has no such failure mode, so those two channels get a real gate
instead of none.

### 5.2 Citations: a Noul, then a Choice

Replaces `_extract_claims` and `_adjudicate`, deleting `_coerce_claims` and
`_coerce_adjudication`.

1. Sentence split in code. Deterministic, no model.
2. `is_factual_claim` (Noul), per sentence: does this sentence assert a checkable
   fact about what changed in this release (behaviour, API, fix, version), as
   opposed to framing, instruction or opinion?
3. `relation` (Choice), per surviving claim, using the citation-check cookbook's
   wording, with state `{claim, evidence}` where evidence is the cited commit's
   subject, body and changed-file list:
   - `supports`: the section states the claim or directly implies it is true.
   - `contradicts`: the section states the opposite or implies it is false.
   - `says_nothing`: the section does not address what the claim asserts.

Verdict mapping: `supports` to cited, `contradicts` to a loud failure,
`says_nothing` to the uncited warning. **Gate on confidence, not the label.** The
cookbook's 0.8 auto-accept is a starting point to measure, not a rule: the pshat
spike produced a `supports` verdict at confidence 0.33 that was simply wrong. The
threshold is set from Phase 0 numbers on Origin's own data and recorded in
`config.toml`.

### 5.3 Commit impact: two Scores

Independent dimensions, per the composite-scoring pattern, combined in code.

`reader_impact`, four levels:

0. Internal only: CI config, lint, test-only changes, formatting, or a dependency
   bump with no behaviour change.
1. Behaviour changes for contributors or in developer tooling, not for someone
   using the released package.
2. A user of the package would notice: a bug fixed, a message changed, a flag or
   a default adjusted.
3. A user must act, or gains a new capability: a new command or API, a removed or
   renamed surface, a changed output format.

`breaking_risk`, three levels:

0. Nothing existing changes meaning; purely additive or internal.
1. Existing behaviour changes in a way most callers will not notice, such as a
   corrected edge case or reworded output.
2. Existing callers can break: removed or renamed surface, changed defaults,
   changed output format or exit code.

Composite in code, weights in `config.toml` so re-ranking costs no inference:

```
weighted = 0.7 * (reader_impact / 3) + 0.3 * (breaking_risk / 2)
```

This replaces "keep the newest 300 and warn." Selection becomes "keep everything
above the threshold, then fill by recency," which is defensible to a user whose
commit was dropped.

## 6. Phase 0: the evaluation

No judgment is wired into a gate before it beats the incumbent on this corpus.

**Corpus, all real and already in the repo.** 227 commits across 15 tags on
`main`; the 7 generated drafts in `.devrel/deliverables/` (`wave1-cyra-*`,
`wave1-mox-*`), which already passed today's gate; the shipped 0.3.0 changelog.
(`v0.2.16..v0.3.0` is only 4 commits because of squash merges, too thin for
selection on its own.)

**Protocol.** Question file frozen before the corpus is opened. Hand labels
written before the run, on a held-out set. This is not ceremony: a previous
TypeSafe evaluation was contaminated by writing examples after reading the
sample, and the blind re-run changed the conclusion.

**Labels are named patterns, not numbers.** Each flagged unit is labelled with a
pattern name from the Tier 3 vocabulary and the offending line. Two people agree
far more readily on "this is a colon reveal" than on "this drafts at register 2
rather than 3", so the labels are reproducible and a disagreement is arguable
against the text. The reference's own `eval.md` checklist supplies the wording
for the label set.

**Controls.** The slop Score is run against human-written text (README prose,
real commit messages) as well as the generated drafts. A Score that flags
everything is worthless, and only the control shows that. Citations are run
against deliberately corrupted pairs (wrong SHA, overstated claim) as well as
true ones.

**Incumbents to beat.** Regex blocklist plus `llm_lint` for slop; the source-link
gate for citations; "newest 300" for selection.

**Measured.** Agreement with hand labels; the two failure modes that matter
specifically (a flagged phrase that is not in the text, a citation passed that
should not have been); tokens in and out; wall clock per release.

**Kill criterion.** If the judgments do not beat the incumbents on the hand
labels, Phases 1 through 4 do not ship and the result is reported as a negative
finding.

## 7. Phases

| Phase | Content | Blocked on |
|---|---|---|
| 0 | Question file, harness, blind run, report | nothing |
| 1 | `judgments.py` port plus typed grounding in `quality/grounding.py`, behind the existing `ground=True` flag | nothing |
| 2 | Slop Score replacing `llm_lint` in `quality/slop.py`, with `force_rewrite` rewired | nothing |
| 3 | Semantic citation gate in the release loop; slop gate re-enabled for release notes and changelog | `origin-release-loop.patch` |
| 4 | Commit selection and channel routing | `origin-release-loop.patch` |

Phases 1 and 2 ship on their own and are worth shipping on their own: together
they remove both prose parsers and the hallucinating lint from code that is
already in the repo, independently of whether the release loop ever arrives.

Parked, not in scope: `devrel next` ranking, and scoring prospect repos for pilot
outreach.

## 8. Testing

The baseline on clean `main` is 1098 passed, 0 skipped, coverage 77.99%, ruff
clean, so any regression is visible.

- Tests inject a `FakeJudge`; no test makes a network call, following the
  existing `respx` convention for HTTP.
- The `NullJudge` path is tested explicitly, including that a skipped judgment
  renders as `skipped` and never as a pass. That test is the degradation
  contract, so it is written first.
- `slop.py` tests change with `force_rewrite`'s new input and are updated in the
  same commit.
- Phase 0's harness lives outside the package, in a scratch directory, and is not
  shipped.

## 9. Risks and open items

1. **Phases 3 and 4 are blocked.** `origin-release-loop.patch` is not on this
   machine and the branch is not on GitHub. Phases 0 through 2 proceed regardless.
2. **The no-LLM check workflow.** The drafts-check workflow is deliberately the
   no-key gate, and the Action's zero-config path never writes
   `.devrel/config.toml`. Degradation keeps it working, but the gate output must
   state which checks ran, or a green check will mean less than a reader assumes.
3. **Price per token is unpublished**, hence the `n/a` handling in 4.3. Revisit
   if TypeSafe publishes pricing.
4. **Thresholds come from data, not from the cookbook.** Recorded in
   `config.toml` with the date and corpus they were fitted on.
5. **A second provider** is a new supply-chain and offline dependency, which the
   optional extra bounds but does not remove.
6. **Cost visibility depends on an unrelated fix.** `cli/release.py` does not
   register the cost sink today, so release spend of any kind is invisible to
   `devrel cost` until that is wired (release-loop task 3). Judgment spend in the
   release path inherits that blindness until then.
7. **Per-unit judging multiplies calls.** See the cost note in 5.1. If Phase 0
   shows the token cost is out of proportion to the catch rate, the fallback is
   paragraph-level units or a Score-gated cascade, not shipping it anyway.
8. **Separate finding, not part of this work.** The shipped starter template
   `project/templates/slop-blocklist.md` ingests two of its own prose lines as
   blocklist entries, because `parse_blocklist` skips only lines beginning with
   `#` and the file's explanatory paragraphs are plain text. Verified by running
   the shipped parser over the shipped template: 32 entries, 2 of them prose.
   Harmless today, since neither sentence will ever appear in a draft, but it
   should be fixed when the Tier 1 list is revised.

## 10. Reference and attribution

The slop taxonomy in 5.1 follows `petergyang/no-ai-slop` (MIT licence), whose
`SKILL.md` names the patterns and whose `eval.md` supplies the checklist wording
used for the Phase 0 label set. Two things carry over beyond the vocabulary:

- The three-way split between banned words, context-dependent words and
  structural patterns, which is what Origin's single regex list conflates.
- The argument against scoring: "Do not rewrite, score the draft, or guess
  whether AI wrote it. AI detectors guess. Named patterns are evidence the user
  can check."

It also independently reaches Origin's house rule on em dashes ("in short copy,
use none").

If any of the reference's wordlist ships inside Origin's starter template, the
MIT notice travels with it: a credit line in `slop-blocklist.md` and the licence
text retained. Paraphrasing the pattern *names* into Origin's own criteria is
fine; copying its prose wholesale is not, without that notice.
