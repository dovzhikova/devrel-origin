# devrel-origin

**A developer-first CLI for AI-powered DevRel, sales, and marketing.**
**Website:** [useorigin.co](https://useorigin.co)

`devrel-origin` is a `pipx`-installable command-line tool that runs a 15-agent system against any project — community triage, social listening, theme extraction, growth experiments, content production, video tutorials, documentation, competitive intel, sales outreach, brand-consistent campaigns, and post-publish content performance analysis. Operates on a project repo the way `git`, `npm`, and `cargo` do.

Every piece of content the system produces flows through an 8-stage editorial pipeline (developmental edit → line edit → copy edit → anti-slop → reader-persona test → readability check → brand audit) so output reads like senior-editor work, not generic AI prose.

> Every deliverable in this repository was produced by the agent system itself.

---

## Quick start

First content in five minutes. `devrel init` is now an interactive wizard
that walks you through scaffold → LLM key → health check → voice tuning →
first draft in one session.

```bash
pipx install devrel-origin

cd /path/to/your/project
devrel init                # interactive wizard, all the way to first draft
```

The wizard:

1. **Scaffolds `.devrel/`** with `config.toml`, `voice.md`, `style.md`,
   `slop-blocklist.md`, `kb/`, `deliverables/`, `state.db`
2. **Configures an LLM key** — pick Anthropic or OpenRouter (recommended:
   free credits, no waitlist), validates with a one-token ping, writes to
   `.devrel/.env` (chmod 600)
3. **Runs a health check** — confirms env, scaffold, schema
4. **Opens `voice.md` in `$EDITOR`** so you can drop in 3-5 sample passages
   from your best published content
5. **Generates your first content draft** — prompts for topic + type, calls
   Kai through the full editorial pipeline, persists the draft +
   grounding/code-validation trace

Skip flags for non-default flows:

```bash
devrel init --skip-draft        # wizard through voice edit, no LLM call
devrel init --skip-chain        # scaffold only, you run auth/doctor/draft yourself
devrel init --non-interactive --name myproj --url ... --github-repo ...
                                 # CI shape: scaffold only, no prompts
```

After onboarding:

```bash
devrel run                       # ad-hoc weekly pipeline (all 15 agents)
devrel schedule install          # cron it (Mondays 09:00 UTC default)
```

> **Why OpenRouter?** Lower onboarding barrier than Anthropic API access (no
> waitlist, free monthly credits) and supports per-agent model routing.
> The wizard recommends it.

Stuck? See [docs/troubleshooting.md](docs/troubleshooting.md) for the
common failures (OpenRouter 400, missing keys, ungrounded content,
quality-gate aborts) and their fixes.

After `devrel init`, your repo has a `.devrel/` directory with:

```
.devrel/
  config.toml          # product identity, model selection, budget caps
  voice.md             # tone profile + sample passages    (commit)
  style.md             # house style + per-content targets (commit)
  slop-blocklist.md    # banned phrases                    (commit)
  kb/                  # knowledge base, TF-IDF indexed
  deliverables/        # generated outputs
  state.db             # SQLite: jobs, costs, checkpoints
```

The four committed files (`config.toml`, `voice.md`, `style.md`, `slop-blocklist.md`) encode the editorial contract. Diff them like any other source.

---

## Commands

```
# Bootstrap & health
devrel init                      bootstrap .devrel/ in cwd
devrel doctor [--json]           check env, API keys, KB freshness
devrel cost [--month YYYY-MM]    token + USD report from state.db

# Pipelines
devrel run                       full weekly cycle
devrel run --health              health check only (Watchdog)
devrel run --agent NAME --task T run a single agent ad-hoc

# DevRel
devrel triage [--days N]         GitHub issue triage (Sage)
devrel listen [--platforms ...]  Reddit / HN / X (Echo)
devrel synthesize                theme extraction (Iris)
devrel experiment HYPOTHESIS     A/B + power analysis (Nova)

# Content
devrel content draft PROMPT      revision-looped + 5-lever quality (Kai)
devrel content audit FILE        run quality pipeline on existing draft
devrel content slop FILE         run only the anti-slop pass
devrel docs build                AST-based docs (Dex)
devrel video record SCRIPT       screen-recorded tutorial (Vox)

# Sales
devrel intel COMPETITOR
devrel sales outreach COMPANY
devrel sales battlecard COMPETITOR
devrel sales sequence CAMPAIGN

# Marketing
devrel marketing blog TOPIC
devrel marketing landing TOPIC
devrel marketing social TOPIC
devrel marketing campaign BRIEF

# Knowledge base
devrel kb add URL [--category C]
devrel kb list
devrel kb refresh

# Config & schedule
devrel config get KEY
devrel config set KEY VALUE
devrel schedule install | list | remove

# Outputs
devrel deliverables list
devrel deliverables show NAME

# Analytics (Argus)
devrel analytics report [--since 7d] [--push] [--push-on-partial]
devrel analytics history CONTENT_ID
devrel analytics diff PERIOD_A PERIOD_B
devrel analytics calibration
devrel analytics summary [--root PATH]
```

Global flags on most verbs: `--json` (machine-readable output) and `--quiet`.

---

## Editorial quality pipeline

Every content-producing run (`devrel content draft`, `devrel content audit`, plus internal calls from `marketing`, `sales`, `kb`-driven tutorials) flows through 8 stages:

```
1. Generate          KB-grounded; voice.md + style.md in prompt
2. Developmental     critique+revise (structure, argument, hook)
3. Line edit         critique+revise (rhythm, voice fidelity)
4. Copy edit         critique+revise (grammar, code, consistency)
5. Anti-slop         regex blocklist + LLM lint; force-rewrite on hit;
                       second failure aborts loud with a phrase report
6. Reader persona    "skeptical senior backend dev" scores 1-10
7. Readability       Flesch + sentence variance + jargon density
                       checked against per-content-type targets
8. Brand audit       Sentinel (existing 6-dimension audit)

→ deliverables/ + revision-trace.json (every stage's score + diff)
```

Stages 5-7 use Haiku for cost; stages 2-4 use Sonnet. Total cost ≈ 2.5-4× a single revision loop, with prompt caching pulling toward the lower bound. BudgetGate guardrails (configurable in `.devrel/config.toml`) track spend; `devrel cost --month YYYY-MM` reports it.

---

## How it works internally

Hub-and-spoke with 15 agents. Atlas orchestrates; specialists execute across the pipelines.

```
Atlas (Orchestrator)
├── Health: Watchdog (pre-flight) + Sentinel (pre-publish brand audit) + Cyra (CRO audit, opt-in) + Argus (post-publish performance analyst)
├── DevRel: Sage, Echo, Iris, Nova, Kai, Vox, Dex
└── Sales:  Rex, Pax, Mox
```

The weekly cycle (driven by `devrel run`):

```
Stage 0: Watchdog         (health + budget check)
Stage 1: Sage + Echo + Dex     parallel
Stage 2: Rex + Iris            parallel
Stage 3: Nova + Kai            parallel (Kai routes through quality pipeline)
Stage 4: Vox
Stage 5: Sentinel              brand audit
Stage 5b: Argus                post-publish content performance analysis
Stage 5c: Cyra                 CRO audit (opt-in via [orchestration].cro_in_run)
Stage 6: Instantly sync, OKR compilation, Sheets publish, digest
```

Argus is config-gated by `[orchestration].analytics_in_run` (default `true`); set to `false` to skip the stage. Standalone use via `devrel analytics report` is unaffected. Cyra is opt-in via `[orchestration].cro_in_run` (default `false`); standalone use via `devrel cro audit` is unaffected.

The `Atlas.delegate()` API also dispatches single-agent tasks, which is what every non-`run` verb wraps. So `devrel triage` is `Atlas.delegate("sage", "Triage GitHub issues from the last 7 days")` — the agents never appear in the public CLI surface, only the verbs.

---

## Configuration

`.devrel/config.toml` example:

```toml
[project]
name = "openclaw"
url = "https://openclaw.ai"
github_repo = "openclaw/openclaw"

[model]
default = "claude-sonnet-4-6"
cheap = "claude-haiku-4-5-20251001"
opus_opt_in = true

[budget]
monthly_usd = 100.0
warn_at_pct = 80
```

Edit with `devrel config set <key> <value>` or directly in your editor.

### Environment variables

| Variable | Required | Used by |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | yes | every LLM-using verb |
| `GITHUB_TOKEN` | for triage | Sage |
| `FIRECRAWL_API_KEY` | for KB harvest + intel | `kb add`, Echo, Rex |
| `BRAVE_API_KEY` | optional fallback | search |
| `INSTANTLY_API_KEY` | for cold-email sync | Pax, Mox |
| `APOLLO_API_KEY` | for lead enrichment | Rex, Pax |
| `TELEGRAM_BOT_TOKEN` | for digests | Atlas pipeline |
| `EMAIL_SENDER` / `EMAIL_PASSWORD` | for digests | Atlas pipeline |
| `OPENAI_API_KEY` | for video TTS | Vox |

`.env` files at the project root are loaded automatically. Cross-project shared keys can live at `~/.devrel/secrets.env`.

---

## Retargeting to another product

```bash
cd /path/to/other-project
devrel init --name otherproduct --url https://otherproduct.dev --github-repo owner/otherproduct
devrel kb add https://otherproduct.dev/docs --category docs
# edit voice.md / style.md / slop-blocklist.md to match the other product's voice
devrel doctor
devrel run
```

The agent system is product-agnostic. Per-project config + KB + voice files do all the targeting.

---

## Documentation

The user-facing docs live in [`docs/`](docs/):

- [`docs/quickstart.md`](docs/quickstart.md) — install, configure an LLM key, ship your first grounded draft in 5 minutes
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — common failures and fixes (OpenRouter 400, missing keys, ungrounded content, quality-gate aborts)
- [`docs/agents/argus.md`](docs/agents/argus.md) — content performance analyst, the 13th agent
- [`docs/cli/analytics.md`](docs/cli/analytics.md) — full reference for the `devrel analytics` subgroup
- [`docs/cookbook.md`](docs/cookbook.md) — common recipes (calibration, weekly cron, multi-project rollups)

Internal docs (architecture specs, implementation plans) live in [`docs/superpowers/`](docs/superpowers/).

---

## Tech stack

| Component | Choice |
|---|---|
| Language | Python 3.12+ |
| CLI framework | Typer + Rich |
| Agent SDK | Claude Agent SDK |
| HTTP | httpx (async) |
| Default model | Claude Sonnet 4.6 (Haiku for cheap quality stages, Opus opt-in) |
| Stats | scipy (power analysis, Bayesian eval) |
| Video | Playwright + FFmpeg + OpenAI TTS |
| Storage | SQLite per project (.devrel/state.db) |
| Tests | pytest + pytest-asyncio + respx |

---

## Author

**Daria Dovzhikova** — DevTools Growth Strategist & AI Agent Builder
- 12+ years in DevTools (JetBrains, Huawei, Lightrun, Odigos)
- [dariadovzhikova.com](https://dariadovzhikova.com)

---

MIT License
