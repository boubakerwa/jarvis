# Jarvis

> A personal AI assistant running locally on a Mac Mini, accessible via Telegram.

Jarvis monitors your Gmail inbox, analyses emails and attachments, organises documents into a structured Google Drive library, and maintains a persistent memory about you. It answers questions about anything you've shared with it — all through a simple Telegram chat.

---

## Features

| Capability | Details |
|---|---|
| **Conversational AI** | Hand-rolled Claude agent loop with tool use |
| **Persistent Memory** | SQLite (structured) + ChromaDB (semantic search) — remembers facts, preferences, decisions, documents |
| **Gmail Monitoring** | Polls inbox every 5 min, parses emails + attachments (PDF, DOCX, images) |
| **Smart Document Filing** | Claude classifies each attachment and files it to the right Google Drive folder automatically |
| **Telegram Interface** | Secure single-user bot with commands for memory management |

---

## Architecture

```
Telegram Message
      │
      ▼
 JarvisAgent  ──── tools ────► MemoryManager (SQLite + ChromaDB)
      │                   └──► DriveClient (Google Drive)
      │
Gmail Watcher (background)
      │
      ▼
 EmailParser ──► AttachmentClassifier (Claude) ──► DriveClient ──► MemoryManager
```

**Five subsystems:**

- **Agent Loop** (`core/agent.py`) — orchestrates all reasoning, tool calls, and responses via the Anthropic SDK
- **Memory Manager** (`memory/`) — SQLite source of truth + ChromaDB vector index; deduplicates by topic with a full audit trail
- **Telegram Bot** (`telegram/bot.py`) — long-polling bot; single allowed user ID; handles file uploads
- **Gmail Watcher** (`gmail/`) — polls unread mail, extracts text from attachments, triggers the filing pipeline
- **Drive Filer** (`storage/` + `agent_sdk/filer.py`) — Claude classifies each file and places it in the right folder

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

Before each Claude API call, the top-N most semantically relevant memories are retrieved from ChromaDB and injected into the system prompt.

---

## Google Drive Structure

All files are organised under a `Jarvis/` root with fixed top-level folders:

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

## Telegram Commands

| Command | Description |
|---|---|
| Any message | Passed to the agent — ask anything |
| `/memories` | List all stored memories grouped by category |
| `/forget <topic>` | Delete a memory by topic |
| `/reset` | Clear in-session conversation history (long-term memories are preserved) |
| `/status` | Show memory count, Drive status, model info |
| File / photo upload | Classified by Claude and filed to Drive automatically |

---

## Setup

### 1. Clone & install

```bash
git clone https://github.com/boubakerwa/jarvis.git
cd jarvis
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=...          # from @BotFather
TELEGRAM_ALLOWED_USER_ID=...    # your numeric Telegram user ID
GOOGLE_CREDENTIALS_PATH=credentials.json
GOOGLE_TOKEN_PATH=token.json
```

### 3. Google OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project, enable **Gmail API** and **Google Drive API**
3. Create OAuth 2.0 credentials (Desktop app), download as `credentials.json`
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
│   └── prompts.py            # System prompt builder + memory injection
├── memory/
│   ├── schema.py             # MemoryRecord dataclass
│   └── manager.py            # SQLite + ChromaDB CRUD
├── storage/
│   ├── schema.py             # Drive folder constants + classification prompt
│   └── drive.py              # Google Drive API client
├── agent_sdk/
│   └── filer.py              # Claude-powered attachment classifier
├── gmail/
│   ├── parser.py             # Email + attachment parsing
│   └── watcher.py            # Gmail polling loop
└── telegram/
    └── bot.py                # Telegram bot handler
```

---

## Deployment (Mac Mini)

Run Jarvis as a persistent background service with `launchd`:

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
| Language | Python 3.11+ |
| AI | Anthropic Claude (`claude-sonnet-4-6`) |
| Memory (structured) | SQLite |
| Memory (semantic) | ChromaDB |
| Messaging | python-telegram-bot |
| Email | Gmail API |
| Storage | Google Drive API |
| Auth | Google OAuth 2.0 |

---

*Built by Wess — personal use only.*
