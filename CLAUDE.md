# CLAUDE.md — Project Handoff for Claude Code

> **Note:** This repository moved to a `src/devrel_origin/` layout in Phase 1 of the CLI direction. See `docs/superpowers/specs/2026-04-29-devrel-origin-cli-design.md`.

## Identity

This is **`devrel-origin`**, a `pipx`-installable Python CLI that runs a 15-agent DevRel + Sales + Marketing system against any project repo. Operates on `cwd` like `git` / `npm` — `devrel init` scaffolds a `.devrel/` directory with config, voice/style/slop files, knowledge base, and state DB. Every CLI verb (`devrel run`, `devrel content draft`, `devrel triage`, etc.) wraps a single-agent or pipeline call.

Every piece of content flows through an 8-stage editorial quality pipeline (`quality.editorial.run_pipeline`) before being shipped: developmental edit → line edit → copy edit → anti-slop → reader-persona → readability → brand audit.

The system is retargetable per project: each `.devrel/config.toml` carries the product identity (name, URL, github_repo), and `.devrel/kb/` carries the harvested docs.

---

## Architecture

Hub-and-spoke with 15 agents. Atlas orchestrates, 14 specialists execute across the pipelines.

```
Atlas (Orchestrator)
│
├── Health Pipeline
│   ├── Watchdog → System Health (pre-flight checks, budget, integration status)
│   ├── Sentinel → Brand Auditor (pre-publish voice, ICP, messaging audit)
│   ├── Cyra     → CRO Auditor (opt-in landing-page conversion audit; Stage 5c)
│   └── Argus    → Content Performance Analyst (post-publish: PostHog, GitHub,
│                  Instantly, Echo's social_mentions — structured Recommendations)
│
├── DevRel Pipeline
│   ├── Sage  → Community Manager (GitHub issue triage, sentiment, churn risk)
│   ├── Echo  → Social Media Listener (Reddit, HN, Twitter/X — LLM batch sentiment)
│   ├── Iris  → Feedback Synthesizer (chunked theme extraction, pain point ranking)
│   ├── Nova  → Growth Strategist (experiments, funnels, cohort segmentation)
│   ├── Kai   → Content Creator (revision-looped tutorials, content dedup)
│   ├── Vox   → Video Producer (screen-recorded tutorials, TTS narration)
│   └── Dex   → Documentation Generator (AST-based architecture docs & API refs)
│
└── Sales Pipeline
    ├── Rex   → Competitive Intelligence (parallel search, Apollo enrichment)
    ├── Pax   → Sales Enablement (revision-looped outreach, battle cards)
    └── Mox   → Campaign Marketing (revision-looped content, parallel analytics)
```

### Weekly pipeline (parallelized)

```
Stage 0: Watchdog (health check)
Stage 1: Sage + Echo + Dex ──── parallel
Stage 2: Rex + Iris ──────────── parallel
Stage 3: Nova + Kai ──────────── parallel
Stage 4: Vox
Stage 5: Sentinel (brand audit)
Stage 6: Instantly sync
Stage 7: OKR compilation
Stage 8: Sheets publish + Telegram/Email digest
```

### Cross-agent data flow

```
Sage (triage)  ──┐
Echo (social)  ──┼→ Rex (competitive) ─→ Pax (sales) → Mox (campaigns)
                 │
                 └→ Iris (themes) ─→ Nova (experiments)
                                  ─→ Kai (content) → Vox (video) → Dex (docs)
                                        ↑
                                  previous_weeks (dedup + trends)
```

### Cross-run memory

- `WeeklyMemory` summaries extracted from last 4 archived contexts
- Kai uses `previous_content_titles` to avoid repetition
- Recurring themes flagged for deeper coverage
- Full context archived as `context_YYYY-WNN.json`

---

## File Map

```
src/devrel_origin/core/
  atlas.py      — Orchestrator. SharedContext + WeeklyMemory dataclasses,
                   DelegationResult, retry with exponential backoff + jitter,
                   run_weekly_cycle() with parallelized stages,
                   _publish_and_notify(), _compile_okrs(), CLI entry point.
  watchdog.py   — System Health. AgentHealthCheck, SystemHealthReport.
                   Checks output freshness, budget via per-agent TokenUsage,
                   integration connectivity. Pre-flight in weekly cycle.
  argus.py      — Content Performance Analyst. PerformanceMetric/Recommendation/
                   PerformanceReport dataclasses, deterministic _score_metrics,
                   single-call Sonnet recommender with closed action vocab.
  sentinel.py   — Brand Auditor. AuditItem, BrandAuditReport.
                   LLM-powered 6-dimension audit (voice, ICP, accuracy, CTA,
                   formatting, consistency). Structural fallback without LLM.
  sage.py       — Community Manager. IssuePriority/SentimentScore enums,
                   TriagedIssue dataclass, rule-based sentiment with churn signals.
  echo.py       — Social Listener. SocialMention, PlatformSummary, SocialListeningReport.
                   LLM batch sentiment classification (50/call) with rule-based fallback.
  iris.py       — Feedback Synthesizer. FeedbackTheme (composite_score = freq x severity),
                   DeveloperJourneyStage. Chunked extraction (30 signals/batch) with
                   theme merging by normalized title. No signal caps.
  nova.py       — Growth Strategist. ExperimentDesign, FunnelAnalysis, CohortSegment.
                   scipy-based power analysis, Bayesian evaluation.
  kai.py        — Content Creator. ContentPiece dataclass. TF-IDF KB search,
                   upstream pain point integration, content dedup via previous_weeks,
                   revision loop (generate → critique → revise, min score 7/10).
  vox.py        — Video Producer. ScriptParser → TTSEngine → BrowserRecorder →
                   OverlayRenderer → VideoAssembler. Consumes Kai's output.
  dex.py        — Documentation Generator. ParsedSymbol/ParsedModule/RepoAnalysis.
                   AST-based Python parser, heuristic JS/TS parser.
  rex.py        — Competitive Intelligence. CompetitorProfile/Threat/Opportunity.
                   Parallel web search + Apollo enrichment via asyncio.gather().
  pax.py        — Sales Enablement. OutreachEmail/BattleCard/NurtureSequence.
                   Revision-looped generation. Apollo prospect + Instantly campaign support.
  mox.py        — Campaign Marketing. BlogPost/LandingPageCopy/SocialBatch/CampaignBrief.
                   Revision-looped, parallel campaign analytics via asyncio.gather().
  base.py       — Shared utilities. TF-IDF KnowledgeBaseSearch (replaces keyword overlap),
                   load_agent_prompt() for file-based prompt management, strip_markdown_fences().
  llm.py        — LLM client. generate(), generate_with_revision() (critique-revise loop),
                   critique(), CritiqueResult, RevisionTrace. TokenUsage with per_agent
                   breakdown. set_agent() for cost attribution.
  agent_config.py — YAML config loader. AgentConfig with product_name/product_url fields.
  types.py      — Shared TypedDict definitions for agent results.
  video/        — Vox sub-modules (script parser, TTS, recorder, overlays, assembler).

src/devrel_origin/tools/
  api_client.py     — Async PostHog API v2 client. Typed DTOs, retry-enabled.
  github_tools.py   — Async GitHub client. Issues, comments, profiles, labels.
  search_tools.py   — Web search (Firecrawl + Brave fallback), official docs via GitMCP.
  code_validator.py — Syntax validation: ast.parse() Python, delimiter JS, json.loads() JSON.
  notifications.py  — Telegram + email. NotificationConfig, NotificationService.
                      send_telegram(), send_email(), send_digest() (auto-formats context).
  sheets.py         — Google Sheets. SheetsConfig, ContentCalendar.
                      publish_content(), get_pending_content(), ensure_headers().
  scheduler.py      — Cron manager. ScheduleEntry, Scheduler.
                      install_cron(), remove_cron(), list_entries(). CLI for digest sending.
  kb_harvester.py   — KB auto-population. HarvestSource, HarvestedDoc, KBHarvester.
                      harvest_all(), harvest_url(). Firecrawl + direct HTTP fallback.
                      Supports website, github, substack, sitemap source types.
  instantly_client.py — Instantly AI client. Parallel bulk lead upload with semaphore-bounded
                        concurrency (10 concurrent). Campaign CRUD, analytics, reply triage.
  apollo_client.py  — Apollo.io client. Organization enrichment, contact search/match.
  analytics.py      — Argus collectors: PostHog, GitHub, Instantly, Social.
                      Each isolates failures (returns []), Argus marks sources_ok.
  mcp_server.py     — MCP server. 14 tools via JSON-RPC over stdio transport.

src/devrel_origin/cli/      Typer app + per-command modules. 24 verb / verb-
                           group modules wired into a single Typer app.
src/devrel_origin/cli/_common.py    Shared CLI helpers (find_paths_or_exit,
                                   build_atlas_or_exit, render_result,
                                   _build_llm_client, _load_project_env).
src/devrel_origin/cli/run.py + 23 more  One file per verb / verb group:
                                   init, auth, doctor, migrate, run,
                                   triage, listen, synthesize, experiment,
                                   intel, cost, content, sales, marketing,
                                   kb, schedule, deliverables, config,
                                   docs, video, growth, cro, argus,
                                   analytics.
src/devrel_origin/project/  Project bootstrap. paths.py walks cwd to find
                           .devrel/. config.py loads config.toml. state.py
                           manages SQLite state DB. init.py scaffolds
                           .devrel/ idempotently. templates/ holds the
                           starter content for voice.md, style.md,
                           slop-blocklist.md, config.toml, .gitignore.
src/devrel_origin/project/cost_sink.py  Builds an async sink that writes
                                   LLM cost events into .devrel/state.db.
                                   Atlas registers it on construction
                                   when project_paths is provided.
src/devrel_origin/quality/  8-stage editorial pipeline. voice.py loads
                           voice.md; style.py loads + parses targets;
                           slop.py runs regex + LLM lint + force-rewrite;
                           persona.py scores via skeptical-dev persona;
                           readability.py computes Flesch + sentence
                           stats; editorial.py orchestrates the 8 stages
                           with copy-edit fallback on persona/readability
                           failures.

knowledge_base/   — Curated product docs (auto-harvestable via kb_harvester)
optimize/         — Per-agent prompt files. Drop optimize/{agent}/system_prompt.txt to override.
tests/            — Test suite (pytest + pytest-asyncio + respx)
config/           — env.example, agent_config.yaml (product_name, schedule, retry, API clients)
deliverables/     — Agent-generated output artifacts
context_archive/  — Weekly SharedContext JSON snapshots
```

---

## Tech Stack

| Component | Choice | Notes |
|-----------|--------|-------|
| Language | Python 3.12+ | async/await, type hints, dataclasses throughout |
| Agent SDK | Claude Agent SDK | `query()`, `ClaudeAgentOptions` |
| Tool protocol | MCP (Model Context Protocol) | JSON-RPC over stdio transport |
| HTTP client | httpx (async) | All tool modules use httpx.AsyncClient |
| Model | Claude Sonnet 4.6 | |
| Stats | scipy | Power analysis, Bayesian experiment evaluation |
| Testing | pytest + pytest-asyncio + respx | respx for httpx mocking |

---

## Key Patterns

### Content revision loop
```python
content, trace = await llm_client.generate_with_revision(
    system_prompt=..., user_prompt=...,
    max_rounds=2, min_score=7,  # critique-then-revise if score < 7
)
# trace.revision_rounds, trace.final_score, trace.critiques
```

### TF-IDF knowledge base search
```python
kb = KnowledgeBaseSearch(knowledge_base_path)
results = kb.search("feature flags setup", limit=5)
# Returns docs scored by TF-IDF relevance, not keyword overlap
```

### Per-agent cost tracking
```python
llm_client.set_agent("kai")  # Called by Atlas.delegate()
# After cycle: llm_client.usage.per_agent → {"kai": {input_tokens, output_tokens, calls}, ...}
```

### Prompt file loading
```python
# Agents load prompts from optimize/{agent}/system_prompt.txt if it exists
# Falls back to inline _DEFAULT_SYSTEM_PROMPT
prompt = load_agent_prompt("kai", "system_prompt.txt", default_prompt)
```

### Cross-run memory
```python
ctx = SharedContext.load_with_history(archive_dir, history_weeks=4)
# ctx.previous_weeks → [WeeklyMemory(content_titles, pain_points, competitors, ...)]
```

### SharedContext (cross-agent state)
```python
@dataclass
class SharedContext:
    week_of: str
    sage_triage: dict        # Stage 1
    echo_social: dict        # Stage 1
    dex_docs: dict           # Stage 1
    rex_competitive: dict    # Stage 2
    iris_themes: dict        # Stage 2
    nova_experiments: dict   # Stage 3
    kai_content: dict        # Stage 3
    vox_video: dict          # Stage 4
    okr_progress: dict       # Stage 7 (includes brand_audit, pre_health)
    previous_weeks: list[WeeklyMemory]  # Cross-run memory (transient)
```

---

## Environment Variables

```
# Required
ANTHROPIC_API_KEY       — Anthropic API key for Claude calls

# GitHub
GITHUB_TOKEN            — GitHub PAT with repo read access

# Search
FIRECRAWL_API_KEY       — Firecrawl API key (fc-...)
BRAVE_API_KEY           — Brave Search API key (fallback)

# Sales integrations
INSTANTLY_API_KEY       — Instantly AI API key
APOLLO_API_KEY          — Apollo.io API key

# Notifications
TELEGRAM_BOT_TOKEN      — Telegram bot token
TELEGRAM_CHAT_ID        — Telegram chat ID for alerts
EMAIL_SENDER            — Gmail address for digests
EMAIL_PASSWORD           — Gmail app password
EMAIL_RECIPIENTS        — Comma-separated recipient list

# Content calendar
SHEETS_SPREADSHEET_ID   — Google Sheets spreadsheet ID
SHEETS_ACCESS_TOKEN     — OAuth access token for Sheets API

# Optional
POSTHOG_API_KEY         — PostHog API key (for analytics integration)
POSTHOG_PROJECT_ID      — PostHog project ID
OPENAI_API_KEY          — OpenAI API key (for Vox TTS narration)
```

Copy `config/env.example` to `.env` and fill in values.

---

## Commands

```bash
# Install (editable, with dev extras for tests)
pip install -e ".[dev]"

# Or from PyPI (end users)
pipx install devrel-origin

# Bootstrap a project (Phase 2)
devrel init --name openclaw --url https://openclaw.ai --github-repo openclaw/openclaw

# Run project health checks
devrel doctor
devrel doctor --json

# Generate content via the 8-stage editorial pipeline
devrel content draft "tutorial on feature flags" --type tutorial

# Audit an existing draft
devrel content audit ./draft.md --type blog_post

# Pipelines (Phase 4)
devrel run                                    # full weekly cycle
devrel run --health                           # health check only
devrel run --agent kai --task "Write tutorial"

# DevRel verbs
devrel triage --days 7
devrel listen --platforms reddit,hn
devrel synthesize
devrel experiment "Hypothesis text"

# Sales / Marketing
devrel intel <competitor>
devrel sales {outreach|battlecard|sequence} <arg>
devrel marketing {blog|landing|social|campaign} <arg>

# KB / Schedule
devrel kb {add|list|refresh}
devrel schedule {install|list|remove}

# Utilities
devrel cost [--month YYYY-MM]
devrel deliverables {list|show <name>}
devrel config {get|set} <key> [value]
devrel content slop <file>

# Niche
devrel docs build
devrel video record <script>

# Run full weekly cycle (legacy module entry point)
python -m devrel_origin.core.atlas --weekly-cycle

# Run single agent task
python -m devrel_origin.core.atlas --agent kai --task "Write a tutorial on feature flags"
python -m devrel_origin.core.atlas --agent watchdog --task "Check system health"
python -m devrel_origin.core.atlas --agent sentinel --task "Audit content quality"

# Knowledge base harvesting
python -m devrel_origin.tools.kb_harvester --url "https://example.com/docs" --category docs
python -m devrel_origin.tools.kb_harvester  # Harvest all configured sources

# Scheduling
python -m devrel_origin.tools.scheduler --action install   # Install cron jobs
python -m devrel_origin.tools.scheduler --action list      # Show schedule
python -m devrel_origin.tools.scheduler --action remove    # Remove cron jobs
python -m devrel_origin.tools.scheduler --action digest --mode weekly  # Send digest

# Start MCP server (stdio transport)
python -m devrel_origin.tools.mcp_server

# Run tests
pytest tests/ -v
```

---

## Coding Conventions

- **All new code must be async** — every agent and tool uses async/await
- **Type hints everywhere** — use `dict[str, Any]`, `list[str]`, `X | None` (Python 3.12 style)
- **Dataclasses for DTOs** — never raw dicts for structured data passed between components
- **httpx for HTTP** — never requests or aiohttp for new code
- **Logging over print** — use `logger = logging.getLogger(__name__)` pattern
- **Line length 100** — enforced by ruff and black
- **Test with respx** — mock httpx calls, never hit real APIs in tests
- **Knowledge base is markdown** — one .md file per topic, searched via TF-IDF
- **Prompts from files** — use `load_agent_prompt()` for overridable prompts
- **Parallel when independent** — use `asyncio.gather()` for independent operations
- **Content quality** — new content-producing agents must call `quality.editorial.run_pipeline`,
  not `generate_with_revision` directly. The single legacy revision loop
  is for fallback only (no .devrel/ project, or pipeline AbortLoud).

---

## Retargeting to Another Product

To point this system at a different open-source DevTools product:

1. **Set `product_name`** in `config/agent_config.yaml` — auto-flows to Rex, Pax, Mox
2. **Harvest KB** — `python -m devrel_origin.tools.kb_harvester --url "https://newproduct.com/docs"`
3. **Update `src/devrel_origin/tools/github_tools.py`** — Change `OWNER/REPO` constants
4. **Optionally customize prompts** — Drop into `optimize/{agent}/system_prompt.txt`
5. **Run** — `python -m devrel_origin.core.atlas --weekly-cycle`

---

## Quick Reference for Common Tasks

**Add a new agent:**
1. Create `src/devrel_origin/core/new_agent.py` with `execute(task, context)` async method
2. Register in `Atlas.__init__()` and `Atlas._agents` dict
3. Add to weekly cycle in `Atlas.run_weekly_cycle()`
4. Update SharedContext with new agent's output field
5. Export from `src/devrel_origin/core/__init__.py`

**Add an integration:**
1. Create `src/devrel_origin/tools/new_tool.py` with async client class
2. Wire into Atlas via `_publish_and_notify()` or a dedicated stage
3. Add env vars to `config/env.example`
4. Graceful degradation: check env vars before using

**Override an agent's prompt:**
1. Create `optimize/{agent_name}/system_prompt.txt`
2. Agent auto-loads it via `load_agent_prompt()` — no code changes needed

**Add knowledge base content:**
1. `python -m devrel_origin.tools.kb_harvester --url "URL" --category CATEGORY`
2. Or manually create `.md` file in `knowledge_base/{category}/`
3. TF-IDF index rebuilds automatically on next agent run

**Run a single agent in isolation:**
```bash
python -m devrel_origin.core.atlas --agent sage --task "Triage issues labeled 'bug' from last 3 days"
```
