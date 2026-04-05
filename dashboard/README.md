# Marvis Dashboard

Run the local dashboard with:

```bash
python -m dashboard
```

Default address:

```bash
http://127.0.0.1:8080
```

Use `/?tab=memory` for the memory view and `/?tab=drive` for the Drive listing.

Docs are available at `http://127.0.0.1:8080/docs` when the dashboard is running.

It reads from `logs/jarvis.log`, `data/jarvis_memory.db`, `data/gmail_state.txt`, and `data/gmail_activity.jsonl`. It does not modify the main app.

Tabs:

- `/?tab=overview` for app status, Gmail activity, and recent logs
- `/?tab=memory` for active Marvis memories
- `/?tab=drive` for a read-only listing of files under the managed Google Drive root, including clickable links

The dashboard now switches tabs client-side without reloading the whole page.

The dashboard logo asset lives at `dashboard/assets/marvis-mark.svg` and can also be reused as the bot avatar source.
