# GitHub Issues Agent Scaffold

This scaffold adds a dedicated `github_issues/` package to support a future Telegram-driven issue workflow without coupling it to existing bot/runtime code yet.

## What Exists Now

- `github_issues/intents.py`
  - Parses Telegram-style text commands into structured intents.
  - Supported command surface:
    - `/gh create <title> | <body> | labels=bug,ops`
    - `/gh status <issue-number>`
    - `/gh list [open|closed|all] [limit]`
    - `/gh update <issue-number> | title=... | body=... | labels=... | state=open|closed`
    - `/gh help`

- `github_issues/client.py`
  - Minimal GitHub Issues REST client (`GET/POST/PATCH`) with:
    - lazy env loading via `load_github_client_config()` and `GitHubIssuesClient.from_env()`
    - injectable request transport for offline tests
    - auth gating for write operations
    - response normalization into `IssueSummary`

- `github_issues/service.py`
  - Orchestrates command handling (`parse -> execute -> user-facing text`).
  - Returns structured `IssueAgentResponse` objects for easy Telegram integration.

## Environment Contract

- Required: `JARVIS_GITHUB_REPOSITORY=owner/repo`
- Optional API base override: `JARVIS_GITHUB_API_BASE=https://api.github.com`
- Token (for create/update): `JARVIS_GITHUB_TOKEN` (fallback: `GITHUB_TOKEN`)

Read-only operations (`status`, `list`) can run without a token for public repositories.

## Integration Steps (Deferred)

1. Wire Telegram command routing in `telegram_bot/bot.py` (or a dedicated command handler module) to call `GitHubIssuesService.handle_message(...)`.
2. Add activity + issue logs in `core/opslog.py` around GitHub mutations.
3. Add optional status push notifications to Telegram once background sync or polling strategy is selected.
4. Add richer intent parsing (free-form natural language to command plan) once command-mode is stable.
