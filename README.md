# AI Workflows

A personal automation monorepo built on GitHub Actions and Claude. Each project has its own `uv` virtual environment and runs on its own independent cron schedule.

## Projects

| Project | Description | Schedule | Slack channel |
|---|---|---|---|
| [`stock_analyzer/`](./stock_analyzer/) | Short-term Japanese equity analysis (technical + fundamental + news + margin-trading data). Self-improving loop: log predictions → verify against outcomes → update strategy weights | Weekdays 8:00 / 16:00 JST, Saturday 10:00 JST for the weekly strategy review | `SLACK_CHANNEL_STOCK` |
| [`tech_catchup/`](./tech_catchup/) | Daily AI-industry digest sourced from Hacker News, GitHub Trending, arXiv, AI-company blogs, and Reddit | Daily 7:30 JST | `SLACK_CHANNEL_TECH` |
| [`point_sites/`](./point_sites/) | Japanese reward-site (ポイ活) and lottery automation, adapter-based. Production-active: moppy / hapitas / amefuri / pointtown / getmoney / fruitmail / warau / sugutama (Gmail / on-site inbox / endpoint-poll sources) + lottery: chanceit / fruitmail_lottery / dreammail. pointincome runs in extract-only mode because the site geofences non-JP IPs (the bot extracts click URLs from Gmail and posts them to Slack so the user can click manually). Includes persistent cookie rotation, ID/PW login fallback via Playwright, three-layer credit verification, and Playwright `DailyWizard` flows | Per-site, staggered 7:30–21:45 JST | `SLACK_CHANNEL_<SITE>` |
| [`verify/`](./verify/) + [`scripts/pending_verify.py`](./scripts/pending_verify.py) | Deferred mechanical-verification system: YAML files + GitHub Issues schedule checks (cron-run log greps, workflow triggers, etc.) that fire on a future date. The daily runner reports results to Slack and the related issue; failures optionally attempt auto-fix via Claude Code Action | Daily 7:30 JST | `SLACK_CHANNEL_VERIFY` |

> See [`CLAUDE.md`](./CLAUDE.md) and [`point_sites/CLAUDE.md`](./point_sites/CLAUDE.md) for design rationale and operational policies.

## Prerequisites

- **Claude authentication**: Register a `CLAUDE_CODE_OAUTH_TOKEN` issued by `claude setup-token` (not an API key). The action runs against your Claude Pro/Max subscription, no per-call API billing.
- **Claude GitHub App**: Install <https://github.com/apps/claude> on the repo.
- **Slack notifications**: One shared Bot User OAuth Token (`SLACK_BOT_TOKEN` = `xoxb-...`) plus a per-project channel secret (`SLACK_CHANNEL_<PROJECT>`). Invite the bot into each channel. No incoming webhook required — adding a new project means adding a single `SLACK_CHANNEL_<NAME>` secret.
- **Language / runtime**: Python 3.12+. **Every project uses `uv` + `pyproject.toml` + `uv.lock` for an isolated venv.** Direct `pip install` into the system Python is forbidden.
- **Gmail authentication**: Migrated from IMAP to **Gmail API + OAuth2 (read-only scope)** on 2026-05-17. All Gmail-dependent jobs share `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN`.

## Required secrets

| Secret | Purpose |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Shared authentication for Claude Code Action runs (Pro/Max subscription) |
| `SLACK_BOT_TOKEN` | Slack Bot User OAuth Token, shared across all projects |
| `SLACK_CHANNEL_<PROJECT>` | Per-project channel ID or `#name` (`_TECH` / `_STOCK` / `_VERIFY` / `_MOPPY` / `_HAPITAS` / `_POINTINCOME` / `_AMEFURI` / `_POINTTOWN` / `_GETMONEY` / `_FRUITMAIL` / `_WARAU` / `_SUGUTAMA` / `_CHANCEIT` / `_FRUITMAIL_LOTTERY` / `_DREAMMAIL`) |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | Gmail API OAuth2 (read-only). Obtain via `scripts/get_refresh_token.py` |
| `<SITE>_COOKIES` | Cookie JSON for each point_sites adapter (exported via Cookie-Editor) |
| `<SITE>_USER` / `<SITE>_PASS` | (Optional) Credentials for the ID/PW login fallback. When set, a stale cookie triggers a fresh Playwright login and the rotated cookies are merged back |

## Local execution

From inside each project directory: `uv sync` then `uv run`.

```bash
# Stock analysis
cd stock_analyzer && uv sync
uv run python -m stock_analyzer.main prepare    # fetch data + compute indicators
uv run python -m stock_analyzer.main notify     # Slack notification

# AI tech catch-up
cd tech_catchup && uv sync
uv run python -m tech_catchup.main gather       # collect
uv run python -m tech_catchup.main notify       # notify

# Point sites
cd point_sites && uv sync
uv run python -m point_sites.main run --site moppy
uv run python -m point_sites.main gmail_dump --site moppy --query 'from:moppy.jp newer_than:7d'  # debug helper
```

## GitHub Actions

The key files under `.github/workflows/`:

### Equity / news / verification

| File | Purpose | Schedule (JST) |
|---|---|---|
| `stock-analysis.yml` | Predict held tickers + screen for new candidates | Weekdays 8:00 / 16:00 |
| `weekly-review.yml` | Weekly review of the equity strategy | Saturday 10:00 |
| `tech-catchup.yml` | AI news digest | Daily 7:30 |
| `pending-verify.yml` | Iterate over `verify/**/*.yml` checks, post results to Slack and the linked issue | Daily 7:30 |
| `ci.yml` / `point_sites-ci.yml` | ruff + mypy + pytest (repo-wide / point_sites-specific) | On pull request |

### point_sites (reward-site / lottery)

Each per-site YAML is a thin wrapper around `_site-runner.yml` (a reusable workflow). The wrapper exposes `workflow_dispatch` inputs such as `extract_links`, `inspect_url`, `discover`, `force_fresh_cookies`, `force_password_login_test`, and `gmail_dump_query` for debugging.

| File | Type | Schedule (JST) | Notes |
|---|---|---|---|
| `moppy.yml` | reward (Gmail) | 7:30 | |
| `pointincome.yml` | reward (Gmail, extract-only) | 8:15 | Geofences non-JP IPs → auto-click impossible; extracts URLs and posts to Slack for manual clicking |
| `chanceit.yml` | lottery (Gmail + on-site) | 8:00 | Auto-entry for easy-entry style prizes |
| `dreammail.yml` | reward + lottery | 8:45 | Includes a gacha / precam wizard |
| `amefuri.yml` | reward (endpoint poll) | 9:15 | SPA login bonus driven via a Playwright wizard |
| `pointtown.yml` | reward (on-site inbox) | 9:30 + 21:30 (keepalive) | |
| `getmoney.yml` | reward (on-site inbox) | 9:45 + 21:45 (keepalive) | game1000 line=1 only |
| `fruitmail_lottery.yml` | lottery | 9:30 | Auto-entry across 5 prize categories |
| `hapitas.yml` | reward (Gmail) | 11:30 | Daily wizard for the 宝くじ交換券 exchange |
| `fruitmail.yml` | reward (Gmail) | 15:00 | Slot / bingo / login bonus / CM viewing wizards |
| `warau.yml` | reward (Gmail) | 18:30 | |
| `sugutama.yml` | reward (Gmail) | 21:30 | |
| `gendama.yml` | (paused) | — | Scaffold only; the site enforces a 180-day inactivity rule, so the cron is disabled |

`_site-runner.yml` uses a 15-minute `timeout-minutes`. If a site exceeds it (heavy SPA waits or too many wizards), trim wizards or raise the per-site timeout.

## Shared architecture patterns

### Claude-driven analysis (stock_analyzer / tech_catchup)

```
[Phase 1: Python]   collect data → write JSON
       ↓
[Phase 2: Claude]   read JSON → AI analysis → write JSON
                    (claude-code-action invokes this on GitHub Actions)
       ↓
[Phase 3: Python]   read result → post to Slack
```

### Pure Python automation (point_sites / pending-verify)

No Claude involvement — straight Python: click-mail processing, Playwright `DailyWizard` execution, scheduled verifications, and so on.

### The mandatory three-layer autonomy contract (point_sites)

| Layer | Role | Implementation |
|---|---|---|
| Detection (verification) | Confirm whether the side effect actually occurred | Balance scrape / click HTTP status |
| Telemetry (recording) | Persist outcomes as a time series (JSONL) | `OutcomeTracker` |
| Decision + notification (escalation) | Fire a Slack alert with a concrete user action when N consecutive runs breach a threshold | Degradation alerts: credit-ratio / HTTP-failure / balance-stagnation |

Full design in [`CLAUDE.md`](./CLAUDE.md) and [`point_sites/CLAUDE.md`](./point_sites/CLAUDE.md).

## Cost

- **Claude**: Covered by the Pro/Max subscription (no per-call API billing)
- **GitHub Actions**: Repo is public, so Linux runners are free with no cap (the 2,000 min/month private-repo quota does not apply)
- **External APIs**: yfinance (equity prices), Gmail API, public RSS / blog feeds — all free-tier only

## License and disclaimer

The implementations are for personal reference. Any action involving investment decisions or financial transactions is your own responsibility. `point_sites` automates access to third-party reward sites whose terms of service may prohibit such automation — review each site's TOS before running.
