# Marvis

> Marvis (Marvelous Jarvis) is a personal AI assistant running locally on a Mac Mini, accessible via Telegram.

Marvis monitors your Gmail inbox, analyses emails and attachments, organises documents into a structured Google Drive library, and maintains a persistent memory about you. It answers questions about anything you've shared with it through a simple Telegram chat.

Local docs: [docs/index.html](./docs/index.html) or, when the dashboard is running, [http://127.0.0.1:8080/docs](http://127.0.0.1:8080/docs).

---

## Features

| Capability | Details |
|---|---|
| **Conversational AI** | Hand-rolled Anthropic-format agent loop routed through OpenRouter |
| **Persistent Memory** | SQLite (structured) + ChromaDB (semantic search) — remembers facts, preferences, decisions, documents |
| **Gmail Monitoring** | Polls inbox every 5 min, parses emails + attachments (PDF, DOCX, images) |
| **Email Relevance Filter** | OpenRouter-backed Claude evaluates each email and skips newsletters, OTPs, and notifications — only real documents get filed |
| **Smart Document Filing** | OpenRouter-backed Claude classifies each attachment and files it to the right Google Drive folder automatically |
| **Telegram Interface** | Secure single-user bot with commands for memory management |

---

## Architecture

```
Telegram Message
      │
      ▼
 Marvis agent  ──── tools ────► MemoryManager (SQLite + ChromaDB)
      │                   └──► DriveClient (Google Drive)
      │
Gmail Watcher (background)
      │
      ▼
 EmailParser ──► RelevanceFilter (Claude via OpenRouter) ──► AttachmentClassifier (Claude via OpenRouter) ──► DriveClient ──► MemoryManager
                        │
                   skip (newsletters,
                   OTPs, notifications)
```

**Five subsystems:**

- **Agent Loop** (`core/agent.py`) — orchestrates all reasoning, tool calls, and responses via the Anthropic Messages format routed through OpenRouter
- **Memory Manager** (`memory/`) — SQLite source of truth + ChromaDB vector index; deduplicates by topic with a full audit trail
- **Telegram Bot** (`telegram_bot/bot.py`) — long-polling bot; single allowed user ID; handles file uploads
- **Gmail Watcher** (`gmail/`) — polls unread mail, filters by relevance, extracts text from attachments, triggers the filing pipeline
- **Drive Filer** (`storage/` + `agent_sdk/filer.py`) — the configured model classifies each file and places it in the right folder

---

## Memory System

Each memory is a structured record:

| Field | Description |
|---|---|
| `topic` | Dedup key — one record per topic |
| `summary` | 1–2 sentence description |
| `category` | `preference` · `fact` · `decision` · `document_ref` · `project` · `household` · `finance` · `health` |
| `source` | `telegram` · `email` · `document` · `manual` |
| `confidence` | `high` · `medium` · `low` |
| `document_ref` | Google Drive file ID (for filed documents) |
| `supersedes` | UUID of replaced record (audit trail) |

Before each model call, the top-N most semantically relevant memories are retrieved from ChromaDB and injected into the system prompt.

---

## Google Drive Structure

All files are organised under the existing Google Drive root folder (currently `Jarvis/`) with fixed top-level folders:

```
Jarvis/
├── Finances/          (Banking, Investments, Tax)
├── Insurance/         (Health, Liability, Vehicle)
├── Legal & Contracts/ (Employment, Rental, Service Agreements)
├── Travel/            (Bookings, Visas & Docs)
├── Health/            (Records, Prescriptions)
├── Subscriptions/
├── Real Estate/
├── Vehicles/
├── Projects & Side Hustles/ (Sufra, Other)
├── Personal Development/    (Courses & Certificates, Books & Resources)
├── Household/         (Appliances & Warranties, Repairs & Services, Utilities)
└── Misc/
```

Files are named `YYYY-MM_description.ext` for chronological sorting.

---

## Email Relevance Filtering

Not every email triggers a filing action. Before any attachment is processed, the model evaluates the email and decides whether it's worth storing.

**Filed:**
- Contracts, agreements, legal documents
- Invoices, receipts, payment confirmations
- Insurance documents or policies
- Travel bookings, tickets, itineraries
- Official correspondence (government, tax, bank, employer)
- Health records, prescriptions, medical documents
- Certificates, credentials, licences

**Skipped:**
- Newsletters and marketing emails
- OTPs, verification codes, security alerts
- Social notifications (likes, follows, comments)
- Automated system notifications and status updates

If the relevance check fails for any reason, Marvis defaults to **filing the email** rather than silently losing a document.

---

## Telegram Commands

| Command | Description |
|---|---|
| Any message | Passed to the agent — ask anything |
| `/memories` | List all stored memories grouped by category |
| `/forget <topic>` | Delete a memory by topic |
| `/reset` | Clear in-session conversation history (long-term memories are preserved) |
| `/status` | Show memory count, Drive status, model info |
| File / photo upload | Classified by the configured model and filed to Drive automatically |

---

## Setup

### 1. Clone & install

```bash
git clone https://github.com/boubakerwa/jarvis.git
cd jarvis
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Marvis now targets Python 3.12. If you are upgrading an existing checkout, recreate the virtual environment before reinstalling dependencies.

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
OPENROUTER_API_KEY=sk-or-...
TELEGRAM_BOT_TOKEN=...          # from @BotFather
TELEGRAM_ALLOWED_USER_ID=...    # your numeric Telegram user ID
GOOGLE_CREDENTIALS_PATH=.credentials.json
GOOGLE_TOKEN_PATH=token.json
OPENROUTER_BASE_URL=https://openrouter.ai/api
OPENROUTER_MODEL=anthropic/claude-sonnet-4.6
JARVIS_TIMEZONE=Europe/Berlin
# Optional lower-risk Gemma routing:
# OPENROUTER_MODEL_RELEVANCE=google/gemma-4-31b-it
# OPENROUTER_MODEL_FINANCIAL=google/gemma-4-31b-it
```

### 3. Google OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project, enable **Gmail API** and **Google Drive API**
3. Create OAuth 2.0 credentials (Desktop app), download as `.credentials.json`
4. On first run, a browser window opens for consent — `token.json` is created automatically

### 4. Run

```bash
python main.py
```

---

## Project Structure

```
jarvis/
├── main.py                   # Entry point
├── requirements.txt
├── .env.example
├── config/
│   └── settings.py           # Environment variable loading
├── core/
│   ├── agent.py              # Main agent loop + tool execution
│   ├── llm_client.py         # Shared OpenRouter-backed Anthropic client
│   └── prompts.py            # System prompt builder + memory injection
├── memory/
│   ├── schema.py             # MemoryRecord dataclass
│   └── manager.py            # SQLite + ChromaDB CRUD
├── storage/
│   ├── schema.py             # Drive folder constants + classification prompt
│   └── drive.py              # Google Drive API client
├── agent_sdk/
│   └── filer.py              # OpenRouter-backed attachment classifier
├── gmail/
│   ├── parser.py             # Email + attachment parsing
│   ├── relevance.py          # OpenRouter-backed email relevance filter
│   └── watcher.py            # Gmail polling loop
└── telegram_bot/
    └── bot.py                # Telegram bot handler
```

---

## Deployment (Mac Mini)

Run Marvis as a persistent background service with `launchd`:

```bash
# Coming in Phase 6 — launchd plist for auto-start on login
```

Logs are written to `logs/jarvis.log` and `logs/jarvis_error.log`.

Telegram uses **long-polling** — no public IP or inbound port required. Works behind NAT out of the box.

---

## Build Phases

| Phase | Status |
|---|---|
| 1 — Foundation (project structure, memory, agent loop, Drive schema) | ✅ Done |
| 2 — Google Drive (OAuth, folder init, file upload) | ✅ Done |
| 3 — Telegram Bot (polling, commands, file upload handler) | ✅ Done |
| 4 — Gmail Watcher (polling loop, email parsing, attachments) | ✅ Done |
| 5 — Attachment Pipeline (classify → Drive → memory) | ✅ Done |
| 6 — Integration & Polish (launchd, end-to-end testing, logging) | 🔜 Next |
| 7 — UI (web dashboard — future) | 🔮 Planned |

---

## Stack

| Component | Technology |
|---|---|
| Language | Python 3.12+ |
| AI | OpenRouter, defaulting to Anthropic Claude (`anthropic/claude-sonnet-4.6`) |
| Memory (structured) | SQLite |
| Memory (semantic) | ChromaDB |
| Messaging | python-telegram-bot |
| Email | Gmail API |
| Storage | Google Drive API |
| Auth | Google OAuth 2.0 |

## Provider Notes

- Validated on April 5, 2026: Claude via OpenRouter is compatible with Marvis's current Anthropic-style tool loop.
- Gemma 4 also produced valid tool calls through OpenRouter in a live smoke test, but it showed slower latency and JSON/schema drift, so it is not documented as drop-in production support yet.
- Recommended selective rollout: keep chat, document classification, and vision on Claude; use Gemma first for `OPENROUTER_MODEL_RELEVANCE` and optionally `OPENROUTER_MODEL_FINANCIAL`.
- Marvis now validates prompt-based JSON after parsing and supports task-specific model overrides with fallback to the default model on critical structured-output paths.

---

*Built by Wess — personal use only.*
