# Changelog

## 0.3.0: anti-slop grounding + `devrel next` action queue (2026-07-26)

Phase B (roadmap items B2 + B3): a claim-grounding quality stage with a
provenance trail, plus a ranked action queue that closes the loop from
analysis to next steps.

### Added

- **`devrel next` ranked action queue (B3).** New command that surfaces a
  prioritized list of recommended next actions for a project, wiring
  analysis output into an actionable, ranked queue.
- **Grounding stage in the editorial pipeline (B2).** New
  `quality.grounding` stage verifies factual claims against repo facts
  before content ships, with a `--grounding` CLI flag to enable it.
- **Provenance trail (`quality.provenance`).** Records and renders the
  evidence chain behind grounded claims, exposed via CLI provenance
  rendering.
- **GitHub repo-facts helpers (`tools.github_tools`).** Fetch structured
  repository facts used by the grounding stage to substantiate claims.

### Changed

- Pinned `ruff` to `>=0.15,<0.16` in dev dependencies so lint/format
  checks are reproducible across local and CI (newer ruff began
  reformatting Markdown code blocks, breaking the format gate).

## 0.2.16: doctor video-extra hint bracket escape (2026-05-14)

### Fixed

- **`devrel doctor` video-extra install hint was stripped to `pip install
  'devrel-origin'`** because Rich console interpreted `[video]` as a style
  tag. Escaped to `'devrel-origin\[video]'` so the literal brackets reach
  the user. Caught via fresh-install validation by the codex branch on
  PR #8; cherry-picked onto current main.

## 0.2.15: PostHog pipeline hardening (2026-05-14)

Cherry-picked the unique work from the stale `codex/posthog-output-quality`
branch (PR #6) onto current main as PR #7. PR #6 predated the rename and
~50% of its diff was already merged via PR #3/#4, so a direct merge was
impossible.

### Added

- **`PostHogClient.fetch_events_by_url`** (HogQL helper). The existing
  `PostHogCollector.collect()` already called this method, but it didn't
  exist on `PostHogClient` — every PostHog-backed Argus run was failing
  silently. Now returns page-view + unique-visitor counts grouped by URL
  with positional-or-dict row normalization.
- **Schema v6 `social_mentions` table** for Echo/Argus social-mention
  storage, plus `_migrate_to_v6` that handles legacy column variants
  (`score`, `engagement`, `url`-fallback for missing `post_id`).
- **`devrel doctor` video toolchain checks**: `video_ffmpeg` and
  `video_playwright` rows with the exact install command in the
  warning text.
- **`last_ok` health flag on all 4 Argus collectors** (PostHog, GitHub,
  Instantly, Social) so partial-data runs can be distinguished from
  fully-healthy ones.
- **Per-agent `started_at` / `completed_at`** in `RunReport` agent
  timings JSON. Atlas's new `timed_delegate` wrapper populates the field
  automatically for stages 0-5.
- **`Vox.execute` result fields** `video_produced`, `recording_skipped`,
  `missing_dependencies` so callers can distinguish "video produced" from
  "script-only fallback" instead of guessing from `status`.

### Changed

- **Editorial pipeline aborts loudly on second persona failure** instead
  of shipping content with `flagged=True`. Kai already catches
  `AbortLoud` as a quality-gate block, so the user-facing effect is
  "content blocked" rather than "weak content shipped".
- **`Atlas._build_kai_task`** now emits "avoid GitHub issue claims" /
  "avoid source-code claims" when Sage/Dex returned no evidence, instead
  of unconditionally demanding both. Stops Kai from inventing issue
  numbers and file paths when upstream had nothing.
- **`Argus._coerce_str_list`** normalizes `evidence`, `source_ids`, and
  `trend_signals` when the LLM returns a string instead of a list. Stops
  the markdown renderer from iterating character-by-character.

### Tests

- 1039 passing (up from 1034 baseline). New focused coverage for
  `_coerce_str_list`, `_build_kai_task` with/without evidence,
  `_missing_video_dependencies`, doctor video-toolchain checks, and the
  new persona `AbortLoud` behavior.

## 0.2.13: Wizard UX fixes from real user testing + audit hygiene (2026-05-13)

Real-user testing on v0.2.12 surfaced three onboarding bugs and one
release-blocker. v0.2.13 fixes all four plus the recommendations from a
full pre-rename audit.

### Fixed

- **Wizard's first-draft step crashed with `ANTHROPIC_API_KEY is required`
  even when `OPENROUTER_API_KEY` was configured.** v0.2.12 shipped the
  wizard before PR #3's `_build_llm_client(paths)` signature change, so
  the wizard's call to `_build_llm_client()` (no args) hit the
  Anthropic-only legacy code path. Fixed in PR #3 via `77cd67a`, but
  PR #3 wasn't tagged. v0.2.13 finally ships the fix to PyPI.
- **`devrel init` asked for `github-repo` even when run inside a git
  working copy.** Now auto-detects from `git remote get-url origin`,
  parses `https://github.com/...` and `git@github.com:...` URLs, and
  offers the result as a prompt default (still empty-acceptable).
- **Vi as the default editor for the voice-edit step was a known
  onboarding killer.** New `_pick_editor()` helper: $VISUAL → $EDITOR →
  first installed of {nano, micro, code} → vi as POSIX last resort. The
  wizard names the chosen editor in the prompt so users aren't surprised.
- **Content-type prompt accepted typos.** Real user typed `bblog_post`
  in 2026-05-13 testing and it silently became the content type. Now a
  numbered picker (`1) tutorial 2) blog_post 3) landing_page 4) cold_email
  5) battle_card`) that accepts both numbers and exact names; rejects
  typos and reprompts.

### Security

- **`urllib3>=2.7.0`** pinned in core deps to mitigate CVE-2026-44431
  and CVE-2026-44432 (transitively pulled via httpx + PyGithub at the
  vulnerable v2.6.x range).

### Internal

- `pyproject.toml` description: "13-agent CLI ... BYO Anthropic key" →
  "15-agent CLI ... BYO Anthropic or OpenRouter key" (Cyra was added
  after the original count, OpenRouter has been supported since v0.2.8).
- `CLAUDE.md` agent count refreshed (13 → 15) and CLI verb count
  refreshed (18 → 24, adds: auth, migrate, growth, cro, argus, analytics).
- `tools/code_validator.py:314`: rename `attrs` → `_attrs` (HTMLParser
  interface requires the param even when unused). Suppresses the vulture
  100% false positive.
- 10 new tests in `tests/cli/test_init_command.py` covering
  `_detect_github_repo` (https/ssh/non-github/non-repo), `_pick_editor`
  ($VISUAL precedence, friendly fallback, vi last resort), and
  `_pick_content_type` (number, name, typo + reprompt).
- Suite at 1018 passed, ruff + format clean.

### Deferred

- Promoting 3 private cross-module imports (`cli/init.py` → `cli/doctor.py`
  + `cli/content.py`, `cli/content.py` → `cli/_common.py`) to public
  helpers. Architecturally right but mechanical scope across 4 test files.
  Tracked for v0.2.14.

## 0.2.12: Onboarding overhaul (2026-05-11)

### Added

- **`devrel init` is now an interactive wizard** that walks you from a
  fresh shell to your first content draft in one session:
  1. Scaffold `.devrel/` (existing behavior)
  2. Configure an LLM key (provider picker + key entry + one-token
     validation, auto-skipped if a key is already in `.devrel/.env`)
  3. Run a health check inline; offer to continue or stop on failures
  4. Open `voice.md` in `$EDITOR` so you can drop in voice samples
     before the first generation
  5. Prompt for topic + content type, generate the first draft via
     Kai, persist `<slug>.md` + `<slug>-trace.json` to deliverables
- **New flags** to opt out of pieces of the chain:
  - `--skip-chain`: scaffold only, even in interactive mode (matches
    the pre-0.2.12 behavior)
  - `--skip-draft`: run the chain through health check + voice edit
    but stop before the LLM call (no spend, no network)
  - `--non-interactive`: implies `--skip-chain` (CI shape unchanged)
- **`devrel auth` success message** now ends with explicit "Next steps"
  pointing at `devrel doctor` + `devrel content draft "..."` so users
  know what to run next.

### Changed

- **README quick start**: now leads with `devrel init` as a one-command
  onboarding entry point. The old `init` → `auth` → `doctor` → `draft`
  sequence is documented under "skip flags for non-default flows."
- **docs/quickstart.md**: added a TL;DR block at the top featuring the
  wizard; the rest of the doc explains what each step does and how to
  recover if you skip or fail one.
- **docs/troubleshooting.md** (added in 0.2.11): now anchored on the
  wizard flow.

### Internal

- 5 new tests in `tests/cli/test_init_command.py` covering the chain:
  scaffold-only escape hatches still work, chain stops on user 'n',
  pre-existing key short-circuits the auth step, `--skip-draft` runs
  voice edit but no LLM call, full chain produces deliverable + trace.
- Suite at 997 passed, ruff + format clean.

## 0.2.11: Fix OpenRouter 400 on default model ids (2026-05-11)

### Fixed

- **`devrel run` / `devrel auth` no longer 400 on OpenRouter.** The
  hardcoded default model ids in `core/llm_backends.py` used Anthropic's
  dated suffix (`anthropic/claude-sonnet-4-5-20250929`), which OpenRouter
  rejects with 400 Bad Request. OpenRouter uses dot notation without a
  date suffix; bumped the three OPENROUTER_ALIASES to the real ids:
  - `sonnet` → `anthropic/claude-sonnet-4.5` (was `…-4-5-20250929`)
  - `haiku` → `anthropic/claude-haiku-4.5` (was `…-4-5-20251001`)
  - `opus` → `anthropic/claude-opus-4` (was `…-4-0-20250514`)
- Native Anthropic backend ids (`ANTHROPIC_DEFAULT_MODEL` etc.) are
  unchanged: the dated form is what Anthropic's API expects.
- Updated `LLMBackend.resolve_alias` docstring + the OpenRouter section
  comment to spell out the dot-notation / no-date-suffix rule, with the
  400-Bad-Request symptom called out so the next reader doesn't
  re-introduce the bug.

## 0.2.10: Output grounding (2026-05-11)

Two changes that close grounding gaps surfaced during PostHog dogfooding:
content paths now refuse to silently produce ungrounded output, and the
CLI verb that pitches the editorial pipeline now actually uses the KB.

### Added

- **Kai evidence gates.** `Kai.execute` now refuses to generate when no
  KB / official-docs / repository evidence is available, returning
  `status="insufficient_evidence"` with an `evidence_gaps` list instead of
  silently producing ungrounded content. Task-shape gating: prompts that
  ask for "pain points" / "GitHub issues" / "file paths" require the
  matching upstream signal or short-circuit.
- **Content brief in Stage 3.** Atlas builds a compact evidence pack (top
  relevant Dex modules + Sage issues + Iris pain point) and threads it
  into Kai's stage-3 task via the new `context={"content_brief": ...}`
  delegation kwarg. Forbidden-claims list bans inventing SDK methods,
  endpoints, install commands, file paths, benchmarks, or issue numbers.
- **Weekly deliverables.** `Atlas._write_weekly_deliverables` persists
  Kai's content + grounding trace + Dex repo summary as
  `.devrel/deliverables/<week>/<slug>.md` + `.trace.json`, surfaced in
  OKRs as `deliverables_written`.
- **Code validator coverage.** `bash` / `sh` / `zsh` / `yaml` / `yml`
  moved from `SKIP_LANGUAGES` to `SUPPORTED_LANGUAGES`. YAML uses
  `yaml.safe_load` and flags deprecated `actions/upload-artifact@v3`.
  Shell uses `shlex.split` per line plus a `rm -rf /` guard.
- **`GitHubTools.repo_full_name`** alias for analytics collectors.

### Changed

- **`devrel content draft` now routes through Kai** instead of calling
  `client.generate()` with a generic "writer producing a first draft"
  system prompt. Kai's path searches `.devrel/kb`, fetches official docs
  via SearchTools when configured, runs the editorial pipeline via
  `generate_with_pipeline`, and validates code blocks. The trace JSON
  carries `grounding_sources`, `pain_points_addressed`,
  `real_issues_referenced`, `revision`, and `code_validation`. Surfaces
  "No KB sources matched" and "Code validation: N/M blocks failed"
  warnings; exits nonzero on `insufficient_evidence` or
  `blocked_by_quality_gate`.
- **`AbortLoud` propagates from `generate_with_pipeline`** instead of
  being silently swallowed and replaced with a single-revision draft.
  Kai catches it explicitly and sets `status="blocked_by_quality_gate"`
  so quality-gate failures are visible, not masked.
- **Sentinel JSON parse failures** no longer fall back to structural
  audit; return `status="audit_failed"` with `overall_score=0` so failed
  audits are visible in the report. `_safe_json_loads` strips markdown
  fences and scans for `{...}` substring before raising.
- **`Atlas._compile_okrs` `content_produced`** is now strict:
  `status=="generated"` AND content non-empty (was: any truthy
  `kai_content` dict, which was True even on `status="error"`).
- **`build_atlas_or_exit` constructs `GitHubTools` for public repos**
  even when `GITHUB_TOKEN` is unset, so Sage / Rex / Argus can read the
  configured repo unauthenticated instead of dropping to no-tool mode.
- **Argus `GitHubCollector`** uses real `GitHubTools` when wired (was:
  always `_dummy_github_client()`).

### Internal

- 13 files / +555 / -55 across PR #1 (`7778325`); 2 files / +184 / -69
  across PR #2 (`91b166f`). Suite at 992 passed, ruff + format clean.
- Test fixture fix: `make_atlas` now mocks `generate_with_revision`, so
  the editorial pipeline completes end-to-end in integration tests; the
  pre-PR `_compile_okrs` check was masking the unmocked-revision crash.
- Renamed `test_falls_back_on_abort_loud` → `test_propagates_abort_loud`.

## 0.2.9: BYO-key onboarding polish (2026-05-08)

Reduces the friction between `pipx install devrel-origin` and a working
`devrel run`. None of the policy-disallowed Claude-Code-session-auth paths
(see Anthropic's Agent SDK terms); the goal is just to make BYO-key as
short a setup as possible.

### Added

- **`devrel auth`**: interactive verb that picks Anthropic or OpenRouter,
  takes the key, validates it with a one-token ping, and writes
  `.devrel/.env` with `chmod 600` (via `dotenv.set_key`, so other entries
  are preserved). Flags: `--provider {anthropic,openrouter}`,
  `--key VALUE`, `--rotate` (opt-in overwrite of an existing key),
  `--no-validate` (skip the ping for offline / metered keys),
  `--non-interactive` (fail-fast for CI). Output masks the key as
  `<first4>...<last4>`. Registered between `init` and `doctor` so the
  discoverability order is `init -> auth -> doctor -> run`.
- **Auto-load `.devrel/.env`**: every CLI verb that builds Atlas now calls
  `python-dotenv` on `.devrel/.env` (preferred) and project-root `.env`
  (fallback) before reading env vars, so users no longer need to `export`
  anything in their shell. `override=False`: shell-exported keys still win
  for one-shot debugging. `python-dotenv` was already a core dep; no
  install footprint change.

### Changed

- **Missing-key error message points at the fix.** Instead of
  `"ANTHROPIC_API_KEY is required (or set OPENROUTER_API_KEY...)"`, the
  exit prints a 4-line block with `devrel auth`, the manual env-var
  alternative, and the OpenRouter free-credits link.
- **`devrel init` success message** is now a numbered list with
  `devrel auth` as step 1, then voice/style/slop edits, then
  `devrel doctor`. Calls out OpenRouter free credits as the easier
  onboarding.
- **`devrel doctor` failure details name the fix verb.** `state_db`
  missing -> "run `devrel init`"; `state_db` schema mismatch -> "run
  `devrel migrate`"; `llm_api_key` missing -> "run `devrel auth`". Doctor
  also auto-loads `.devrel/.env` before checking env vars so a user who
  ran `devrel auth` in another shell session still passes.

### Internal

- 13 new tests across `test_auth_command.py` (9), `test_common_helpers.py`
  (3 in TestEnvAutoLoad), `test_init_command.py` (1
  test_init_success_points_at_devrel_auth), plus 1 doctor assertion
  swap. Suite at 984 passed; ruff + format clean.

## 0.2.8: OpenRouter + remaining backlog (2026-05-08)

Bundles the OpenRouter multi-provider backend with the rest of the v0.2.7
follow-on backlog. The LLM API is back-compat additive: existing
`LLMClient(api_key=...)` callers keep using Anthropic with no changes; new
deployments can opt into OpenRouter with one env var or one toml line.

### Added

- **OpenRouter multi-provider backend**. New `core/llm_backends.py` with an
  `LLMBackend` abstraction and two implementations (`AnthropicBackend`,
  `OpenRouterBackend`). `OpenRouterBackend` posts to OpenRouter's
  OpenAI-compatible `/chat/completions` over the existing httpx core dep, so
  no new SDK dependencies. Switch backends via `[llm].provider = "openrouter"`
  in `.devrel/config.toml` plus `OPENROUTER_API_KEY` env, or auto-detect:
  if `OPENROUTER_API_KEY` is set and `ANTHROPIC_API_KEY` is not, the CLI
  picks OpenRouter.
- **Per-agent model overrides** via `[llm].agent_models` toml map, e.g.
  `{ argus = "openai/gpt-4o-mini", kai = "anthropic/claude-opus-4-0-20250514" }`.
  Resolution priority: budget downgrade > explicit `model=` arg > per-agent
  override > instance default. Concurrent agents under `asyncio.gather` each
  get their own model via the `agent_context()` ContextVar.
- **`devrel docs build` auto-persists Dex outputs to disk**: writes
  `dex-architecture.md`, `dex-api-reference.md`, `dex-summary.md`, and
  `dex-modules.json` to `.devrel/deliverables/` on success. Empty / missing
  fields are skipped; failed runs leave deliverables untouched. Closes the
  workaround where users had to invoke with `--json` and split the truncated
  blob manually.

### Changed

- **Cost model handles OpenRouter-style ids**. `MODEL_COSTS` keeps Anthropic
  bare ids and adds `gpt-4o` / `gpt-4o-mini` / `gpt-4-turbo` rates for
  OpenAI-via-OpenRouter; lookups strip the `provider/` prefix so the same
  table serves both backends.
- **Budget downgrade uses the backend's `cheap_model`** instead of
  hardcoding Haiku, so OpenRouter routes downgrades to
  `anthropic/claude-haiku-4-5-...` automatically.
- **`devrel doctor` LLM key check is now one-of**. Previously
  `ANTHROPIC_API_KEY` was the sole required env; now either
  `ANTHROPIC_API_KEY` or `OPENROUTER_API_KEY` satisfies the check (renamed
  from `ANTHROPIC_API_KEY` to `llm_api_key` in the doctor output).

### Fixed

- **Slop blocklist Haiku hallucinations**. The lint stage occasionally
  flagged phrases that didn't appear in the draft (e.g. "replace this
  blockquote" against a draft containing neither phrase), which then poisoned
  the force-rewrite list and tripped the post-rewrite abort-loud check even
  when the actual slop had been cleared. New `_verify_lint_hits` filters
  Haiku's output to phrases that appear in the source (case-insensitive
  substring match) and logs the hallucinated set at INFO so we can monitor
  the rate. The lint prompt also instructs Haiku to return only verbatim
  phrases.

### Internal

- **All 21 baseline-xfailed tests now pass** (suite is 971 passed / 0
  xfailed). Each was test-vs-prod drift accumulated across the v0.2.0 to
  v0.2.4 restructure: Sage area detection rewritten for the OpenClaw pivot
  vocabulary, search_devrel_docs renamed to search_devrel_ai_agents_docs and
  example.com URLs repointed at openclaw.ai, Kai code-validation tests now
  patch the editorial pipeline at the right level, Instantly bulk-add tests
  use the per-lead endpoint that replaced `/bulk-add`,
  CritiqueResult.revision_needed default flip is now asserted, TokenUsage
  to_dict assertions accept the new per_agent + total_cost_usd keys, Echo
  topic detection updated for the new keyword set. The
  `_BASELINE_XFAIL_NODEIDS` set is retired; the
  `pytest_collection_modifyitems` hook is gone.
- 25 new tests across `test_llm_backends.py`, `test_common_helpers.py`,
  `test_doctor_command.py`, plus 7 in `test_cyra.py`, 5 in `test_atlas.py`,
  4 in `test_llm_cost_sink.py`, 4 in `test_migrate_command.py`, 3 in
  `test_niche_verbs.py` (Dex persistence), 3 in `test_slop.py`
  (hallucination filter).

## 0.2.7: dogfood follow-on fixes (2026-05-08)

Closes the gap surfaced by a 2026-05-08 user run on PyPI 0.2.6 against a
PostHog-scale repo. Anyone on `0.2.6` running `devrel run`, `devrel run --health`,
`devrel run --agent ...`, or any verb that builds Atlas via `build_atlas_or_exit`
hit one or more of these. If you locally patched `cli/_common.py` to pass
config + tools or bumped a hardcoded timeout, you can drop those patches and
upgrade.

### Fixed

- **`cli/run.py` `--health` / `--agent`**: still printed `result.result` after
  `feb5cab` fixed the same typo in `cli/_common.render_result`. Two more sites
  in `run.py:28` and `:37` did the same on the watchdog and single-agent
  paths. Now use `DelegationResult.output`.
- **`cli/_common.build_atlas_or_exit` only passed 4 of Atlas's 9 init args**:
  `archive_dir` defaulted to a relative `context_archive` instead of
  `paths.context_dir`, `config` was never bridged from `.devrel/config.toml`
  (so the shipped `[orchestration].agent_timeouts` knob was effectively dead
  on the `devrel` CLI path), and `github_tools` / `search_tools` /
  `instantly_client` / `apollo_client` were always `None`, forcing every
  tool-using specialist (Sage / Echo / Rex / Vox / Pax / Mox) into its
  degraded no-tool branch even when the corresponding API key was set.
  New helpers `_load_agent_config` and `_resolve_github_repo` parse the
  project toml; optional clients are constructed only when their env key is
  present so projects without those integrations still degrade cleanly.
- **`core/llm._emit_cost` lost cost rows under outer cancellation**:
  `asyncio.CancelledError` is `BaseException` in Python 3.8+, so the existing
  `except Exception` didn't catch it. Outer Atlas timeouts firing between an
  Anthropic response returning and the SQLite cost write left the cost
  invisible to `devrel cost` even though Anthropic had already billed. Fixed
  with `asyncio.shield` around the sink so the SQLite commit completes even
  while the calling task is being cancelled.
- **`core/cyra._persist` never wrote `cro_funnel_metrics`**: `devrel cro
  history` and `devrel cro diff` returned empty rows even after Cyra ran
  end-to-end. New `_persist_funnel_metrics` writes one INSERT OR REPLACE per
  `FunnelStep` (PK funnel_id+step_index+period_end deduplicates same-period
  reruns).
- **`core/cyra._persist` would FK-violate on `report_id=0`**: Atlas's
  `_insert_cro_report_row` returns 0 when no project_paths or DB is
  available; the downstream `persist_recommendation` call would then violate
  the FK to `analytics_reports` once `PRAGMA foreign_keys=ON` was enforced.
  Now short-circuits cleanly with a warning log.
- **`core/atlas._insert_cro_report_row` wrote a duplicate
  `analytics_reports` row in Stage 5c**: alongside Argus's Stage 5b row for
  the same period, leaving two rows where one should be. Now does
  get-or-insert keyed on `period_end`; reuses Argus's row id and preserves
  its `report_json` blob untouched.
- **Editorial-agent timeouts (Kai / Mox / Pax) bumped 600s -> 1800s**: a
  PostHog-scale `devrel run` hit Kai timeout at 900s after the 0.2.6 default
  of 600s was already locally patched. The cost-budget cap in `config.toml`
  is a better safeguard than a tight timeout for these agents; override
  per-agent via `[orchestration].agent_timeouts` when needed.

### Added

- **`devrel migrate`**: idempotent CLI verb that upgrades `.devrel/state.db`
  to the current `SCHEMA_VERSION`. Same behavior as the internal
  `init_db()`, just discoverable as a real verb. No-op when already at the
  current version; reports `v{before} -> v{after}` on actual migration.

### Internal

- New tests: `tests/cli/test_common_helpers.py` (14 cases for the wiring
  bridge), `tests/cli/test_migrate_command.py` (4 cases including a v4 -> v5
  fixture upgrade), `tests/test_llm_cost_sink.py` (4 cases for the shield),
  `tests/test_cyra.py` (7 new persist/funnel-metrics cases), `tests/test_atlas.py`
  (5 new `_insert_cro_report_row` cases). Suite at 919 passed / 21 xfailed,
  ruff + format clean.

## 0.2.6: Wave 4 timeout polish (2026-05-08)

Addresses two production issues surfaced in 2026-05-08 dogfood runs against
a real Anthropic key. Anyone running editorial-pipeline agents (Kai, Mox,
Pax) on `0.2.5` likely saw both: every full-pipeline invocation wall-clocked
out at 5 minutes, and Atlas re-spent the same expensive tokens on each retry.

### Changed

- **Per-agent execution timeouts**: `Atlas.AGENT_TIMEOUT` (300s) is now a
  global default, and `Atlas.DEFAULT_AGENT_TIMEOUTS` overrides it to 600s for
  Kai, Mox, and Pax. Their 8-stage editorial pipeline (draft, developmental,
  line, copy, anti-slop, persona, readability, final) routinely exceeds 300s
  with revision loops. Override per-agent via the new
  `[orchestration].agent_timeouts` map in `config.toml`, e.g.
  `agent_timeouts = { kai = 1200.0, sage = 60.0 }`.

### Fixed

- **Atlas no longer retries on `TimeoutError`**: every retry restarted the
  agent from scratch, re-spending ~$0.30+ in editorial-pipeline tokens with
  no chance of a different outcome. With `MAX_RETRIES = 2`, a single timeout
  burned 3 attempts (~$0.90+) and 15+ minutes of wall time before surfacing
  the failure. Now a `TimeoutError` returns immediately. Network and
  transient errors continue to retry as before.

### Internal

- Suite now 886 passed / 21 xfailed (4 new tests in `tests/test_atlas.py`:
  skip-retry-on-timeout, default 600s for editorial agents, default 300s for
  others, config override resolution). Ruff + format clean.

## 0.2.5 — dogfood production fixes (2026-05-08)

Four production-path bugs surfaced during a dogfood session running the
weekly pipeline against a real cloned repo with a fresh Anthropic key. Every
agent CLI verb that goes through `build_atlas_or_exit` plus the entire
content-quality pipeline was broken on `0.2.4` and is now fixed. If you
installed `0.2.4` and saw `Atlas.__init__()` arity errors, `result.result`
AttributeErrors, or tuple-unpack TypeErrors mid-pipeline, upgrade to `0.2.5`.

### Fixed

- **`cli/_common.build_atlas_or_exit`**: was constructing `Atlas(llm_client=llm, project_paths=paths)`, but `Atlas.__init__` requires `api_client: PostHogClient` and `knowledge_base_path: Path` as positional args. The fallback `except TypeError: return Atlas(llm_client=llm)` was equally broken. Surfaced when any non-Cyra agent CLI verb (`devrel marketing`, `devrel sales`, `devrel triage`, `devrel listen`, `devrel synthesize`, `devrel experiment`, `devrel intel`, `devrel run`, etc.) was invoked. Fix: import `PostHogClient`, construct it with optional `POSTHOG_API_KEY`/`POSTHOG_PROJECT_ID` env vars (empty strings OK), and pass `paths.kb_dir` as `knowledge_base_path`.
- **`cli/_common.render_result`**: accessed `result.result` on a `DelegationResult`, but the dataclass field is `output` (lines 55 + 64-67). Same typo in both the JSON branch and the human-readable branch. Surfaced when any successfully-completed Atlas-routed verb tried to render its output. Affected every CLI verb that goes through `render_result` except `cro`.
- **`cli/content.draft`**: `draft, _ = await client.generate(...)` tuple-unpacked the return value, but `LLMClient.generate` returns plain `str`. Surfaced when `devrel content draft` was invoked with a real Anthropic key for the first time.
- **`quality/__init__`, `quality/persona`, `quality/slop`** (the editorial pipeline core): same tuple-unpack bug across multiple sites. Surfaced when `quality.editorial.run_pipeline` ran end-to-end with a real key, which is what every revision-looped agent (Kai/Mox/Pax) does. Test suite was also a closed loop: 22+ test mock sites in `tests/test_kai.py`, `tests/quality/test_persona.py`, `tests/quality/test_slop.py`, `tests/quality/test_integration.py`, `tests/quality/test_editorial.py`, and `tests/cli/test_content_command.py` all returned `(str, None)` tuples to match the buggy unpacks. Tests passed forever because they never exercised the real `LLMClient.generate`. Caught only via real-key invocation. Fix touched 9 files across `src/` and `tests/`.

### Internal

- Suite stays at 882 passed / 21 xfailed; ruff + format clean. No coverage delta.

## 0.2.4 — pre-publish polish (2026-05-04)

Final pre-publish pass across linting, packaging, and dependency footprint
landed the day of the PyPI tag. No agent or pipeline behavior changes for
pipx end users; the headline change is a lighter default install.

### Changed

- **Dependency footprint reduced**: `openai`, `playwright`, `pyautogui` moved from core dependencies into a new `[video]` optional extra. Default `pip install devrel-origin` now skips ~150MB of Playwright browsers + pyobjc + the OpenAI SDK. Vox users opt in with `pip install 'devrel-origin[video]'` (or `pipx install 'devrel-origin[video]'`). Calling `TTSEngine` without the extra raises a clear `ImportError` pointing at the install command.
- **`tts_engine`**: `openai.AsyncOpenAI` is now imported lazily inside `_require_openai()`. Module-load no longer touches `openai`. Locked in by `tests/core/test_video_lazy_imports.py`.
- **Dropped unused dependencies**: `requests`, `aiohttp`, and `ffmpeg-python` had zero imports across `src/` and `tests/` and have been removed from `pyproject.toml`. Pure cruft from earlier scaffolding; CLAUDE.md already mandates `httpx` for all HTTP work.
- **Codebase ruff-clean**: full lint pass + format pass; CI now enforces both `ruff check` and `ruff format --check` (the format gate had been deferred since the original ruff adoption).

### Fixed

- **`load_agent_prompt` actually loads on-disk prompts now**: `_OPTIMIZE_DIR` had been resolving to `src/devrel_origin/optimize/` (a path that never existed) since the Phase 1 src/-layout move, so every agent silently fell through to its inline default. Replaced with a `_resolve_optimize_dir()` walk-up that finds the repo root via `pyproject.toml`+`optimize/` co-location, returns `None` for installed users (preserving their current behavior), and accepts both layouts the repo currently uses (top-level `optimize/{agent}/` and nested `optimize/agents/{agent}/`). Dev-tree users will see the maintainer's optimized prompts taking effect for the first time. Coverage in `tests/core/test_load_agent_prompt.py`.
- **`tests/test_vox.py`**: `DesktopRecorder` tests now skip on headless Linux (CI was breaking because `pyautogui` needs an X11 `DISPLAY`). Marked with `_NEEDS_DISPLAY`.

### Internal

- 47-file ruff lint pass: `zip(..., strict=True)` everywhere, import sorting, unused-import removal, `list()` over copy-comprehensions.
- 92-file ruff format pass (whitespace-only).
- `pyproject.toml`: added `extend-exclude` for `examples/`/`optimize/`/`landing/` (script-style, not library code), per-file ignores for tests, and ignores for `C901`+`B008`.
- CLAUDE.md install instruction updated from the dead `pip install -r requirements.txt` to `pip install -e ".[dev]"` + the pipx end-user route. `output/` (Vox's default render dir) added to `.gitignore`.
- New regression coverage: 7 tests for `load_agent_prompt` + 8 tests for video lazy-import. Suite is now 815 pass / 21 xfail / 76% coverage.

## 0.2.4 — 2026-05-03

Argus — the 13th agent, plus a 20-item enhancement pass derived from a multi-lens code review of the v1 ship.

### Added

- **Argus**: post-publish content performance analyst. Pulls metrics from PostHog, GitHub, Instantly, and Echo's `social_mentions` table; ranks deterministically; emits structured `Recommendation` objects via a single Sonnet call with a closed action vocab (`double_down`, `retire`, `rewrite`, `retest`, `amplify`, `investigate`). Sits beside Watchdog (infra) and Sentinel (pre-publish quality) as the post-publish watcher.
- **`devrel analytics report`**: produce a performance report for the last `--since` window. `--push` sends to configured Telegram + email; `--push-on-partial` overrides the all-sources-green gate.
- **`devrel analytics history CONTENT_ID`**: metric trajectory of one piece of content across all reports. Markdown table or `--format json`.
- **`devrel analytics diff PERIOD_A PERIOD_B`**: side-by-side comparison of two reports, sorted by absolute %-delta. Surfaces top movers, plus `new` and `gone` classifications.
- **`devrel analytics calibration`**: scores past `double_down`/`retire` recommendations against subsequent metric history. Reports per-action hit rate, avg confidence, lift vs coin-flip, and high/low confidence calibration.
- **`devrel analytics summary`**: cross-project rollup. Walks every `.devrel/state.db` under `--root` (default `$HOME`) and aggregates total recommendations, metric history rows, last report period, and Argus spend per project.
- **Schema v3**: `metric_history(content_id, period_end, primary_metric, metric_name, content_type)` with composite PK and a `(content_id, period_end DESC)` index. Indexed time-series for week-over-week baselines; replaces O(N) JSON-blob deserialization.
- **Schema v4**: `analytics_recommendations(report_id, action, target, source_ids_json, confidence, first_seen_period, applied_at, ...)`. Per-rec rows queryable by action/target without parsing report blobs. The v2 closed-loop routing bus.
- **Recommendation lifecycle**: when `(action, target)` re-emerges in a later report, `first_seen_period` carries over from the earliest match. The markdown report tags recommendations stale ≥2 weeks as `[STALE Nw]`.
- **Content brief generation**: for each `double_down`/`amplify`/`rewrite` recommendation, Argus stages a Mox-ready brief at `.devrel/deliverables/argus-brief-<period>-<action>-<target>.md` with rationale, evidence, source IDs, and a tailored next-step shell command.
- **Optional Atlas Stage 5b**: `[orchestration].analytics_in_run = true|false` (default `true`) gates Argus inside `devrel run`. Failures surface as `argus_report = {"error": "<reason>"}` rather than aborting the cycle.
- **Structured logs**: five `logger.info` events at `gather_complete`, `baselines_loaded`, `scored`, `recommendations_generated`, `persisted`. An operator can reconstruct any run from log events alone.
- **Documentation**: new `docs/` tree — quickstart, Argus agent reference, analytics CLI reference, cookbook.

### Changed

- **InstantlyCollector**: now filters campaigns by `created_at` (or `updated_at`) within the requested period. Without this filter, `--since 7d` and `--since 90d` returned identical email metrics.
- **CLI cost-sink**: `devrel analytics report` standalone runs now register the cost-sink so `devrel cost` accurately reflects Argus spend.
- **Argus system prompt**: cached at `__init__` instead of re-read on every LLM call (matches the Phase 8 Kai/Mox/Pax/Rex pattern).
- **Argus SQLite I/O**: `_persist`, `_load_baselines`, and `SocialCollector.collect` now wrap blocking calls with `asyncio.to_thread`. Eliminates event-loop stalls during the weekly cycle.
- **`Recommendation.source_ids`**: new `list[str]` field (default `[]`) carrying the `content_id` values backing each recommendation.
- **Echo schema contract**: `SocialCollector` now `PRAGMA table_info`-checks `social_mentions` on first read. Schema drift now warns instead of silently producing partial data.
- **LLM prompt truncation**: when the 50-line cap fires, partial sections append `...(N more X items omitted)` and fully-dropped content types are listed under `### TRUNCATED`.
- **`AgentConfig.analytics_in_run`**: promoted from `getattr` fallback to a typed dataclass field with TOML-key documentation in `config/agent_config.yaml`.

### Fixed

- `--push` flow now constructs `NotificationService` via `NotificationConfig` from env vars (the previous `from_env()` and `send_digest(subject=, body=)` calls would have raised `AttributeError` and `TypeError` at runtime).
- `--push` is now gated on `sources_ok` all-green by default, with `--push-on-partial` to override.
- Atlas Stage 5b honors the `resume_stage` guard and writes a checkpoint, so a crash between Stage 5b and Stage 6 doesn't re-run Sentinel on resume.

### Performance

- **Baselines lookup**: O(N) JSON blob deserialization replaced with a single indexed `SELECT` against `metric_history`. At 2k content IDs the prior approach allocated ~500 KB per call; the new path scales linearly with N.

### Tests

- 800 pass / 21 baseline fail — exact parity with the documented Phase 7-8 baseline (no new failures introduced; +33 tests for Argus + collectors + CLI + Atlas in v1, +23 more in v2).

## 0.2.3 — 2026-05-01

Wave 3 polish — final batch from the 2026-04-29 agent code review. No behavior changes; pure cleanup of papercuts the next round of feature work would otherwise re-discover.

### Changed

- **Atlas**: `process_draft` now uses `subprocess.run([editor, path])` with `shutil.which` validation — no shell-injection surface, "editor not found" surfaces as a log line rather than a silent no-op. Self-improvement step splits `ImportError` (module not installed) from generic `Exception` (module crashed) for diagnosable logs.
- **Echo**: `search_limit` exposed as constructor parameter (default 20) — projects with high mention volume can scan more deeply, projects with low volume can be cheaper.
- **Iris**: theme-extraction prompt no longer hardcodes a single source value. The LLM now infers sources from the signal list, so multi-source feedback (Discourse, support tickets, etc.) is correctly labeled.
- **Sage**: classification keyword vocabularies extracted to module-level constants (`CHURN_SIGNALS`, `FRUSTRATION_SIGNALS`, `BUG_KEYWORDS`, `CRITICAL_KEYWORDS`). Single source of truth — no more silent divergence between sentiment, category, and priority logic.
- **Nova**: `from scipy import stats` moved to module-level (was deferred inside `calculate_sample_size`). MDE-severity logic now has an explanatory comment.
- **Vox**: desktop recorder FFmpeg stderr piped to `PIPE` and logged on non-zero exit (was: discarded silently, failures showed up as cryptic downstream errors). TTS `stream_to_file()` runs in `loop.run_in_executor` so the event loop isn't blocked during audio streaming.

### Performance

- **Kai/Mox/Pax/Rex**: `SYSTEM_PROMPT` cached at construction (was: file re-read on every `execute()` call). Removes a per-call stat+read from the hot path; meaningful for bulk operations like Pax's per-contact personalization.

## 0.2.2 — 2026-05-01

Wave 2 correctness gaps from the 2026-04-29 agent review. Each fix targets a silent diagnosability gap — wrong-but-plausible output that looked like success.

### Fixed

- **Atlas**: per-agent checkpoint flags + Stage 6 checkpoint. Resume from a partial-stage failure now re-runs only the failed agent (was: re-ran all agents in the stage). Instantly sync now checkpoints — a network failure after the brand audit no longer drops Mox/Pax campaign data.
- **Watchdog**: real `output_age_hours` parsed from agent timestamps (was: always 0 or 999). Budget alert as % of cap (was: hardcoded 500k tokens with no link to budget).
- **Sentinel**: split JSON-vs-API error logging — operators can now tell a malformed LLM response from a rate-limit error. Structural-audit scoring normalized to 1-100 scale (was: capped at ~70, incomparable with LLM path).
- **Iris**: `SIMILARITY_THRESHOLD` promoted to module constant with calibration comment. `_find_content_opportunities` now produces actionable briefs incorporating the theme's top recommended action (was: title echoes).
- **Nova**: funnel result includes `data_source` field marking values as `"default_estimates"` vs `"api"` (was: hardcoded mock counts presented as real).
- **Kai**: exception path now sets `status="error"` + `content=""` (was: silent empty-content with `status="generated"`). Uses `logger.exception` for tracebacks.
- **Mox**: `revision` schema unified with Kai's (`remaining_issues` key) — consistent contract across content agents.
- **Pax**: `_load_prompt` migrated to shared `base.load_agent_prompt` (was: hardcoded source-relative path that broke outside the source tree). `_execute_campaign` now None-guards `llm_client` with a clear `"skipped"` status return.
- **Dex**: class-body traversal switched to `ast.walk` so nested classes / decorated methods / staticmethods appear in symbol output. `repo_path` now defaults from `.devrel/` project root (was: process-cwd-dependent).
- **Rex**: JSON parse failure sets `status="parse_error"` + preserves `raw_content` (was: stored raw string under `content`, breaking consumers expecting a dict).

## 0.2.1 — 2026-05-01

Wave 1 bug sweep — 13 high-impact fixes from the 2026-04-29 agent review. Every fix targets a bug that ships to users today: silent broken features, race conditions under concurrent stages, dead alerting paths, and output collisions on parallel runs.

### Fixed

- **Atlas**: race-safe per-agent cost attribution under `asyncio.gather` via `agent_context()` ContextVar (was: shared mutable `_current_agent` clobbered by concurrent stages).
- **Watchdog**: integration alert now fires for any unhealthy status (was: dead-code condition checking for a status the agent never emits). Firecrawl probe now uses a GET-able endpoint (was: POST-only `/v1/scrape` always returned 405).
- **Sentinel**: `_collect_content` now reads each agent's primary content key (was: only `"content"`, missing 6 of 9 agents per cycle).
- **Sage**: `champion_signal` is now actually set (was: declared on `TriagedIssue` but never assigned True). `CHURNING` sentiment gets an empathetic response branch (was: generic "added to triage queue").
- **Echo**: `posted_at` parsed from search results instead of `datetime.now()`. `is_question` uses a dedicated `QUESTION_SIGNALS` constant (was: magic slice of `ENGAGEMENT_SIGNALS[:8]`). Fixed `OpenClaw'` typo.
- **Iris**: unmatched themes route to a new `"other"` journey stage (was: defaulted to `"onboarding"`, systematically inflating onboarding friction). Early-return paths now log distinguishably.
- **Nova**: experiment IDs use `hashlib.sha256` for stability across process restarts (was: Python's randomized `hash()`). `DAILY_SIGNUPS_ESTIMATE` clamped to a floor of 10.
- **Kai**: `content_type` parameter now flows from caller (was: hardcoded `"tutorial"` for all calls including changelogs). Pipeline issue filter handles `list[str]` correctly (was: `isinstance(i, dict)` silently dropped every editorial flag).
- **Mox**: `email_campaign` failure now falls through to the editorial pipeline with a clean prose prompt (was: JSON-format-contaminated prompt corrupted by editorial stages). `PIPELINE_CONTENT_TYPE_MAP` covers all 6 routed content types.
- **Pax**: shared `_extract_icp_criteria` helper deduplicates ICP-extraction prompt + normalization across `_execute_prospect` and `_execute_prospect_personalize` (was: two divergent copies with different exception handling).
- **Vox**: output filename slugged + timestamped (was: hardcoded `tutorial.mp4` collided on parallel runs). FFmpeg subprocess calls in `assembler.py` and `overlay_renderer.py` have a 300s timeout + kill (was: no timeout — hung FFmpeg could block the pipeline indefinitely).
- **Dex**: `ast.AnnAssign` constants now appear in the symbol table (was: only `ast.Assign` visited; modern annotated constants invisible).
- **Rex**: parallel web search bounded by `Semaphore(3)` (was: 10+ unbounded simultaneous requests reliably 429'd by Brave/Firecrawl free tiers). Apollo domain guess preserves existing TLDs (was: `pendo.io` → `pendo.io.com`).

## 0.2.0 — 2026-04-29

The CLI direction. `devrel-origin` is now a `pipx`-installable Python CLI that operates on a project repo (`.devrel/` per project, like `git`/`npm`/`cargo`).

### Added

- **CLI surface (18 verbs)**: `init`, `doctor`, `run`, `content {draft,audit,slop}`, `triage`, `listen`, `synthesize`, `experiment`, `intel`, `sales {outreach,battlecard,sequence}`, `marketing {blog,landing,social,campaign}`, `kb {add,list,refresh}`, `schedule {install,list,remove}`, `cost`, `deliverables {list,show}`, `config {get,set}`, `docs build`, `video record`.
- **8-stage editorial quality pipeline** (`quality.editorial.run_pipeline`): developmental → line → copy edit → anti-slop → reader-persona → readability → brand audit. Used by Kai, Mox, Pax for every content output.
- **Project bootstrap** (`devrel init`): `.devrel/` scaffold with `config.toml`, `voice.md`, `style.md`, `slop-blocklist.md`, `kb/`, `deliverables/`, `state.db`.
- **Cost ledger**: every LLM call records token usage + USD into `.devrel/state.db`'s `costs` table; `devrel cost [--month YYYY-MM]` aggregates.
- **`devrel doctor`**: project + env health checks with `--json` mode.
- **Console script entry-point**: `devrel = "devrel_origin.cli:app"` in `pyproject.toml`.

### Changed

- **Repo restructure**: `agents/` → `src/devrel_origin/core/`, `tools/` → `src/devrel_origin/tools/`. `agents/config.py` renamed to `core/agent_config.py`.
- **Content agents** (Kai, Mox, Pax): replaced single `generate_with_revision` call with `quality.editorial.run_pipeline`. Falls back to legacy revision when no `.devrel/` project exists.
- **Dependencies**: added Typer, Rich, tomli-w. `pyproject.toml` deps now match `requirements.txt`.

### Deprecated / removed

- The SaaS / per-instance Fly + Next.js central-app direction was abandoned. Its branch is preserved as the `archive/v0-agentic-alpha` tag for reference; the local branch was removed.

### Known issues

- 22 pre-existing test drift cases on `main` (`test_sage::TestSageProductAreaDetection`, `test_search_tools::*`, `test_mcp_server::*`, plus a few in `test_llm`/`test_echo`/`test_kai`/`test_code_validator`/`test_instantly_client`). Not introduced by Phases 1-5; deliberately preserved at parity through the migration. Cleanup is its own follow-up.

### Deferred

- `devrel ask` — natural-language router (spec defers to v1.1).
- BudgetGate cap enforcement — costs are recorded; caps are not yet enforced.
- `devrel run --devrel | --sales | --marketing` sub-cycle flag variants.
