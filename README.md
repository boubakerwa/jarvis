<p align="center">
  <img src="./dashboard/assets/marvis-mark.png" alt="Marvis logo" width="120" />
</p>

<h1 align="center">Marvis</h1>

<p align="center">
  <strong>Marvelous Jarvis</strong> is a local AI assistant that talks over Telegram, watches Gmail, files documents into Google Drive, remembers useful context, schedules reminders, tracks work in GitHub, and stays inspectable through a local dashboard.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/runtime-local-1f2937?style=flat-square" alt="Local runtime" />
  <img src="https://img.shields.io/badge/interface-Telegram-26A5E4?style=flat-square&logo=telegram&logoColor=white" alt="Telegram interface" />
  <img src="https://img.shields.io/badge/email-Gmail-EA4335?style=flat-square&logo=gmail&logoColor=white" alt="Gmail watcher" />
  <img src="https://img.shields.io/badge/storage-Google%20Drive-0F9D58?style=flat-square&logo=googledrive&logoColor=white" alt="Google Drive storage" />
  <img src="https://img.shields.io/badge/model-Claude%20style%20tools-CB785C?style=flat-square" alt="Claude-style tool loop" />
  <img src="https://img.shields.io/badge/routing-OpenRouter-111827?style=flat-square" alt="OpenRouter routing" />
</p>

<p align="center">
  <a href="./docs/index.html"><strong>Docs</strong></a>
  |
  <a href="./docs/medium-marvis-article.md"><strong>Medium Article Draft</strong></a>
  |
  <a href="http://127.0.0.1:8080/docs"><strong>Local Docs Route</strong></a>
</p>

> Marvis is designed as a real operator-facing personal assistant, not a toy chatbot. It keeps the agent loop close to Anthropic's Messages model, adds deterministic guardrails around dates and structured outputs, and exposes memory, Drive state, and activity through a local dashboard.

## Why Marvis

Most personal assistants look good in chat and fall apart when they touch real systems.

Marvis is built around a different idea:

- keep the model loop simple and Claude-shaped
- let the application own storage, time, validation, and side effects
- expose memory and recent actions so the system stays inspectable
- treat Gmail filing, calendar writes, and document handling like product workflows, not prompt stunts

That gives you an assistant that can actually do useful local work:

| Surface | What Marvis does |
|---|---|
| Telegram chat | answers questions, recalls memory, searches Drive context, creates tasks and reminders, checks calendar, and can inspect local source/log context |
| Gmail watcher | scans unread mail after a cutoff date, filters low-value mail, files useful attachments |
| Google Drive filing | classifies uploads and attachments into a fixed folder structure with predictable names |
| Memory | stores durable facts, preferences, decisions, and document references in SQLite plus ChromaDB |
| GitHub workflow | creates trackable feature or bug issues and reads pull requests and commits from the configured repo |
| Obsidian notes | writes collaborative Markdown notes into a shared vault with Marvis-chosen organization |
| LinkedIn composer | queues drafts from Telegram, stores generation state locally, and exposes an editor/retry workflow in the dashboard |
| Background schedulers | sends scheduled reminders, morning digests, and batched Gmail summaries over Telegram |
| Dashboard | shows overview, memory, Drive files, LinkedIn drafts, LLMOps telemetry, activity, and interactive docs |
| Docs | ships with local architecture docs plus a Medium-ready article draft |

## Architecture

Marvis has one long-lived chat agent and several specialized LLM-driven stages for background workflows.

```mermaid
flowchart LR
  subgraph User["User Surfaces"]
    TG["Telegram chat"]
    UP["Telegram uploads"]
    DASH["Dashboard + docs"]
  end

  subgraph Core["Marvis Core"]
    AG["Chat Agent"]
    MEM["Memory Manager<br/>SQLite + ChromaDB"]
    NOTES["Notes Manager<br/>Obsidian tools"]
    OPS["LLMOps + ops logging"]
    ROUTER["OpenRouter<br/>Anthropic-style Messages API"]
  end

  subgraph Pipeline["Background Pipelines"]
    GW["Gmail watcher"]
    PARSE["Parser + extraction"]
    REL["Relevance Agent"]
    CLS["Classification Agent"]
    FIN["Financial Agent"]
  end

  subgraph Google["Google Services"]
    DRIVE["Google Drive"]
    CAL["Google Calendar"]
  end

  subgraph Workspace["Shared Workspace"]
    OBS["Obsidian vault"]
  end

  subgraph Telemetry["Local Telemetry"]
    LLMLOG["data/llm_activity.jsonl"]
    OPSLOG["data/ops_activity.jsonl<br/>data/ops_issues.jsonl<br/>data/ops_audit.jsonl"]
  end

  TG --> AG
  AG --> ROUTER
  ROUTER --> AG
  ROUTER --> LLMLOG
  AG --> MEM
  AG --> NOTES
  AG --> DRIVE
  AG --> CAL
  AG --> OPS
  MEM --> AG
  NOTES --> AG
  NOTES --> OBS

  UP --> PARSE
  GW --> PARSE
  PARSE --> REL
  REL --> CLS
  CLS --> DRIVE
  CLS --> MEM
  PARSE --> FIN
  FIN --> MEM
  GW --> OPS
  OPS --> OPSLOG

  DASH --> MEM
  DASH --> DRIVE
  DASH --> OBS
  DASH --> LLMLOG
  DASH --> OPSLOG
```

Interactive architecture docs live in [docs/index.html](./docs/index.html) and on the dashboard route [http://127.0.0.1:8080/docs](http://127.0.0.1:8080/docs).

### Runtime Roles

- **Chat Agent** (`core/agent.py`) handles the Anthropic-format tool loop and assembles final replies.
- **Reminder runner** (`reminders/service.py`) persists scheduled reminders and delivers them via Telegram in the background.
- **GitHub client** (`github_issues/client.py`) reads repository issues, pull requests, and commits and can create issues when configured.
- **Notes Manager** (`notes/service.py`) creates, updates, and appends collaborative Markdown notes in the shared Obsidian vault.
- **LLMOps recorder** (`core/llmops.py`) captures per-call latency, token usage, and estimated model cost in local JSONL.
- **Ops logger** (`core/opslog.py`) records heartbeats, issues, and audit events for note writes, Drive uploads, and other mutations.
- **Relevance Agent** (`gmail/relevance.py`) decides whether a Gmail message is worth filing.
- **Classification Agent** (`agent_sdk/filer.py`) picks the Drive path, filename, and summary for attachments and uploads.
- **Vision Agent** (`utils/text_extraction.py`) describes image-heavy documents when plain extraction is not enough.
- **Financial Agent** (`utils/financial_extraction.py`) extracts vendor, amount, category, and date for finance-oriented documents.
- **Morning digest runner** (`morning_digest/digest.py`) sends a daily Telegram digest with open backlog context.

## What Makes It Reliable

The model is allowed to reason. The app is responsible for reality.

Marvis hardens the risky parts of agent behavior with deterministic code:

- relative dates like `today`, `tomorrow`, and `Monday` are resolved locally before calendar or task actions run
- structured outputs are parsed and validated before they can mutate storage
- Gmail backfills are bounded by a configured cutoff date
- document filing prefers preserving useful documents over silently dropping them
- memory is externalized into structured records instead of hidden in conversation state
- model calls and side-effectful operations are logged into retention-aware JSONL streams for dashboard and `/llmops` inspection

This turned out to matter more than prompt polish. The biggest failures in agent systems are usually plausible outputs that are just wrong enough to cause trouble.

## Features

| Capability | Details |
|---|---|
| Claude-style agent loop | Hand-rolled `system + messages + tools` loop with `tool_use` / `tool_result` round trips |
| OpenRouter routing | Anthropic-compatible transport with Claude as the safe default and optional task-level overrides |
| Persistent memory | SQLite for source of truth plus ChromaDB for semantic retrieval |
| Proactive reminders | Schedules one-off or recurring Telegram reminders with cancellation and list support |
| Gmail monitoring | Polls unread mail every 5 minutes and starts only after the configured cutoff date |
| Morning digest | Sends a daily Telegram digest with open GitHub issues and a suggested focus item |
| Smart filing | Classifies documents and uploads them into a structured Google Drive library |
| Financial extraction | Pulls vendor, amount, date, and category from finance-oriented documents |
| GitHub integration | Creates GitHub issues and reads pull requests or commits from the configured repository |
| Local introspection tools | Lets the agent read project files and structured ops logs in a sandboxed, read-only way |
| Telegram bot | Single-user bot with slash commands, uploads, reminder views, LinkedIn drafting, and long-polling deployment |
| LinkedIn composer | Queues drafts from Telegram text or X URLs and supports dashboard editing plus retry flows |
| Obsidian integration | Creates and updates collaborative Markdown notes in a configurable vault path |
| LLMOps and ops audit | Tracks token usage, estimated cost, heartbeats, warnings, errors, and mutation audit events in local JSONL |
| Dashboard | Overview, memory browser, Drive mirror, LinkedIn workspace, LLMOps telemetry, activity log, and interactive docs |
| Article-ready docs | Includes a Medium draft that explains the architecture and tradeoffs |

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/boubakerwa/jarvis.git
cd jarvis
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Marvis now targets Python 3.12. If you are upgrading an older checkout, recreate the virtual environment first.

### 2. Configure `.env`

```bash
cp .env.example .env
```

Fill in the main settings:

```env
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_BASE_URL=https://openrouter.ai/api
OPENROUTER_MODEL=anthropic/claude-sonnet-4.6

TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_ID=...

GOOGLE_CREDENTIALS_PATH=.credentials.json
GOOGLE_TOKEN_PATH=token.json

JARVIS_TIMEZONE=Europe/Berlin
OBSIDIAN_VAULT_PATH=/absolute/path/to/your/Obsidian/vault
OBSIDIAN_ROOT_FOLDER=.
JARVIS_GITHUB_REPOSITORY=owner/repo
# JARVIS_GITHUB_TOKEN=ghp_xxx
JARVIS_MORNING_DIGEST_ENABLED=true
JARVIS_MORNING_TIME=09:00

# Optional lower-risk Gemma routing:
# OPENROUTER_MODEL_RELEVANCE=google/gemma-4-31b-it
# OPENROUTER_MODEL_FINANCIAL=google/gemma-4-31b-it
```

### 3. Set up Google OAuth

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project and enable **Gmail API**, **Google Drive API**, and **Google Calendar API**.
3. Create OAuth 2.0 desktop credentials and save them as `.credentials.json`.
4. On first run, complete the consent flow in your browser. Marvis will create `token.json` automatically.

### 4. Run the app

```bash
python main.py
```

### 5. Run the dashboard

```bash
python -m dashboard
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080).

## Telegram Commands

| Command | Description |
|---|---|
| Any message | Goes through the chat agent and replies include the running total estimated LLM cost footer |
| `/status` | Shows memory count, Drive status, LinkedIn queue status, and configured model |
| `/llmops` | Shows recent token usage, estimated LLM cost, latency, top LLM tasks, and short-horizon ops health |
| `/memories` | Lists stored memories grouped by category |
| `/reminders [scheduled|cancelled|completed|all]` | Lists reminders currently stored in the local reminder scheduler |
| `/forget <topic>` | Deletes a memory by topic |
| `/reset` | Clears in-session chat history while preserving long-term memory |
| `/linkedin ...` | Queues, lists, rewrites, or processes LinkedIn drafts from text or X/Twitter URLs |
| File or photo upload | Runs the classification and filing pipeline |

## Memory Model

Each memory is stored as a structured record:

| Field | Description |
|---|---|
| `topic` | Deduplication key |
| `summary` | Short description of the fact or decision |
| `category` | `preference`, `fact`, `decision`, `document_ref`, `project`, `household`, `finance`, or `health` |
| `source` | `telegram`, `email`, `document`, or `manual` |
| `confidence` | `high`, `medium`, or `low` |
| `document_ref` | Google Drive file ID for filed documents |
| `supersedes` | UUID of the record it replaced |

Before each model call, the top relevant memories are retrieved from ChromaDB and injected into the system prompt.

## Obsidian Notes

If you set `OBSIDIAN_VAULT_PATH`, Marvis writes into that vault as a shared notes workspace. Set `OBSIDIAN_ROOT_FOLDER=.` if you want it to write directly at the vault root, or set a folder name if you want everything grouped under a subfolder. Marvis can create new notes, append to them, and revise existing note content while still writing plain Markdown files. This works especially well with iCloud on Apple devices.

Marvis is not locked into a preset folder taxonomy. The agent can choose the note title, folder, and structure that best fit the request, then reuse or revise existing notes through search, update, and append operations.

Once enabled, you can ask things like:

- `Please add a leather weekender bag as a gift idea for my wife`
- `Please write a new article draft for local-first assistants`
- `What are my hottest project ideas right now?`

Trackable engineering work no longer needs to live in Obsidian notes. Feature requests, implementation prompts, and bug reports can now be created as GitHub issues through the agent when `JARVIS_GITHUB_REPOSITORY` is configured, while Obsidian stays focused on collaborative drafting and scratch work.

## Drive Filing Layout

Files are organized under the existing Drive root folder `Jarvis/` for backward compatibility.

<details>
<summary><strong>Current folder structure</strong></summary>

```text
Jarvis/
|- Finances/          (Banking, Investments, Tax)
|- Insurance/         (Health, Liability, Vehicle)
|- Legal & Contracts/ (Employment, Rental, Service Agreements)
|- Travel/            (Bookings, Visas & Docs)
|- Health/            (Records, Prescriptions)
|- Subscriptions/
|- Real Estate/
|- Vehicles/
|- Projects & Side Hustles/ (Sufra, Other)
|- PR/                 (LinkedIn Composer)
|- Personal Development/    (Courses & Certificates, Books & Resources)
|- Household/         (Appliances & Warranties, Repairs & Services, Utilities)
`- Misc/
```

</details>

Files are named `YYYY-MM_description.ext` for chronological sorting.

## Dashboard and Docs

The local dashboard gives Marvis an operator surface instead of a black box:

- **Overview** for system status and recent activity
- **Memory** to inspect what Marvis currently retains about you
- **Drive** to mirror the Google Drive files Marvis can see
- **LinkedIn** to open, edit, save, and re-trigger queued LinkedIn drafts
- **LLMOps** for token usage, estimated model cost, inline charts, heartbeat freshness, issue breakdowns, and recent audit events
- **Docs** for architecture walkthroughs and setup help

The dashboard also detects stale runtime processes and surfaces a warning banner when the running dashboard code is older than the repo checkout.

Observability data now uses retention-aware JSONL streams:

- `data/llm_activity.jsonl` for model call telemetry
- `data/ops_activity.jsonl` for positive activity and heartbeats, retained for 5 minutes
- `data/ops_issues.jsonl` for warnings and errors, retained for 3 days
- `data/ops_audit.jsonl` for low-volume mutation events such as task creation, note writes, uploads, and calendar writes

It also ships with:

- [docs/index.html](./docs/index.html): interactive architecture and operations docs
- [docs/medium-marvis-article.md](./docs/medium-marvis-article.md): a Medium-ready article draft about Marvis

## Provider Notes

- As of April 5, 2026, Claude via OpenRouter is validated for Marvis's Anthropic-style tool loop.
- Gemma 4 also worked in live smoke tests, but showed slower latency and more JSON/schema drift.
- Recommended rollout: keep chat, document classification, and vision on Claude first.
- If you want to experiment, route Gemma only into lower-risk paths like relevance or financial extraction.

## Project Layout

<details>
<summary><strong>Repository map</strong></summary>

```text
jarvis/
|- main.py
|- requirements.txt
|- .env.example
|- config/
|  `- settings.py
|- core/
|  |- agent.py
|  |- log_reader.py
|  |- llmops.py
|  |- llm_client.py
|  |- opslog.py
|  |- prompts.py
|  |- source_reader.py
|  |- structured_output.py
|  `- time_utils.py
|- github_issues/
|  |- client.py
|  |- intents.py
|  |- models.py
|  `- service.py
|- calendar_api/
|  `- client.py
|- reminders/
|  |- __init__.py
|  `- service.py
|- memory/
|  |- manager.py
|  `- schema.py
|- gmail/
|  |- parser.py
|  |- relevance.py
|  `- watcher.py
|- morning_digest/
|  |- __init__.py
|  `- digest.py
|- linkedin/
|  |- composer.py
|  |- drive_store.py
|  |- obsidian_store.py
|  |- processor.py
|  |- sqlite_store.py
|  `- x_resolver.py
|- storage/
|  |- drive.py
|  `- schema.py
|- notes/
|  |- obsidian.py
|  `- service.py
|- agent_sdk/
|  `- filer.py
|- utils/
|  |- financial_extraction.py
|  `- text_extraction.py
|- telegram_bot/
|  `- bot.py
|- dashboard/
|  |- app.py
|  `- assets/
|- docs/
|  |- index.html
|  `- medium-marvis-article.md
`- tests/
```

</details>

## Validation

The branch includes automated coverage for:

- OpenRouter client wiring
- Anthropic-format tool loop behavior
- structured output validation
- date resolution and calendar safety
- Gmail watcher cutoff behavior
- note workspace creation, append, and search behavior
- reminder scheduling and delivery flows
- GitHub issue / PR / commit integration
- read-only source and log introspection tools
- LLMOps telemetry summaries and ops audit logging
- dashboard rendering and client-side interactions
- Telegram command publishing

Run the suite with:

```bash
python -m unittest discover -s tests
```

## Current Status

| Area | Status |
|---|---|
| Chat agent and tools | Done |
| Gmail watcher and filing pipeline | Done |
| Memory system | Done |
| Dashboard and docs | Done |
| Python 3.12 project setup | Done |
| Launchd packaging | Not yet shipped |
| PR polish and broader production hardening | In progress |

---

<p align="center">
  Built by Wess for personal use. Marvis keeps the assistant local, useful, and inspectable.
</p>
