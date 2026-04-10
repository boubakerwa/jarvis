# Marvis Dashboard

Run the local dashboard with:

```bash
python -m dashboard
```

Default address:

```bash
http://127.0.0.1:8080
```

Use `/?tab=memory` for the memory view, `/?tab=drive` for the Drive listing, `/?tab=linkedin` for the LinkedIn workspace, and `/?tab=llmops` for model and ops telemetry.

Docs are available at `http://127.0.0.1:8080/docs` when the dashboard is running.

It reads from `data/jarvis_memory.db`, `data/gmail_state.txt`, `data/gmail_activity.jsonl`, `data/llm_activity.jsonl`, `data/ops_activity.jsonl`, `data/ops_issues.jsonl`, and `data/ops_audit.jsonl`. It also falls back to `logs/jarvis.log` when older plain-text log context exists. Most dashboard surfaces are read-only, but the LinkedIn tab can save Obsidian-backed draft edits and re-trigger draft generation.

Tabs:

- `/?tab=overview` for app status, Gmail activity, and recent operational events
- `/?tab=memory` for active Marvis memories
- `/?tab=drive` for a read-only listing of files under the managed Google Drive root, including clickable links
- `/?tab=linkedin` for the LinkedIn draft queue, post editor, save flow, and re-trigger action
- `/?tab=llmops` for token usage, estimated costs, inline SVG charts, latency, retention policy, heartbeat freshness, issue breakdowns, and recent audit events such as note writes, uploads, and calendar mutations

The dashboard now switches tabs client-side without reloading the whole page and exposes `/api/version` so the UI can warn when the running dashboard process is stale relative to the repo.

The dashboard logo asset lives at `dashboard/assets/marvis-mark.svg` and can also be reused as the bot avatar source.
