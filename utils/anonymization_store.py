from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config import settings

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS anonymized_documents (
    drive_file_id TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    sanitized_text TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    replacement_counts TEXT NOT NULL DEFAULT '{}',
    truncated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anonymized_documents_sha ON anonymized_documents(content_sha256);
"""


@dataclass
class StoredAnonymizedDocument:
    drive_file_id: str
    content_sha256: str
    original_filename: str
    mime_type: str
    sanitized_text: str
    backend: str
    model: str
    replacement_counts: dict[str, int]
    truncated: bool
    created_at: str
    updated_at: str


def get_anonymized_document(drive_file_id: str, *, db_path: str | None = None) -> Optional[StoredAnonymizedDocument]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM anonymized_documents WHERE drive_file_id=?",
            (drive_file_id,),
        ).fetchone()
    if not row:
        return None
    return _from_row(row)


def upsert_anonymized_document(
    *,
    drive_file_id: str,
    content_sha256: str,
    original_filename: str,
    mime_type: str,
    sanitized_text: str,
    backend: str,
    model: str,
    replacement_counts: dict[str, int],
    truncated: bool = False,
    db_path: str | None = None,
) -> StoredAnonymizedDocument:
    now = _utc_now()
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT created_at FROM anonymized_documents WHERE drive_file_id=?",
            (drive_file_id,),
        ).fetchone()
        created_at = str(existing["created_at"]) if existing else now
        conn.execute(
            """
            INSERT INTO anonymized_documents (
                drive_file_id,
                content_sha256,
                original_filename,
                mime_type,
                sanitized_text,
                backend,
                model,
                replacement_counts,
                truncated,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(drive_file_id) DO UPDATE SET
                content_sha256=excluded.content_sha256,
                original_filename=excluded.original_filename,
                mime_type=excluded.mime_type,
                sanitized_text=excluded.sanitized_text,
                backend=excluded.backend,
                model=excluded.model,
                replacement_counts=excluded.replacement_counts,
                truncated=excluded.truncated,
                updated_at=excluded.updated_at
            """,
            (
                drive_file_id,
                content_sha256,
                original_filename,
                mime_type,
                sanitized_text,
                backend,
                model,
                json.dumps(replacement_counts, ensure_ascii=True, sort_keys=True),
                1 if truncated else 0,
                created_at,
                now,
            ),
        )
        conn.commit()

    return StoredAnonymizedDocument(
        drive_file_id=drive_file_id,
        content_sha256=content_sha256,
        original_filename=original_filename,
        mime_type=mime_type,
        sanitized_text=sanitized_text,
        backend=backend,
        model=model,
        replacement_counts=dict(replacement_counts),
        truncated=bool(truncated),
        created_at=created_at,
        updated_at=now,
    )


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    target_path = db_path or settings.JARVIS_DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_CREATE_TABLE)
    return conn


def _from_row(row: sqlite3.Row) -> StoredAnonymizedDocument:
    raw_counts = str(row["replacement_counts"] or "{}")
    try:
        replacement_counts = json.loads(raw_counts)
    except json.JSONDecodeError:
        replacement_counts = {}
    return StoredAnonymizedDocument(
        drive_file_id=str(row["drive_file_id"]),
        content_sha256=str(row["content_sha256"]),
        original_filename=str(row["original_filename"]),
        mime_type=str(row["mime_type"]),
        sanitized_text=str(row["sanitized_text"]),
        backend=str(row["backend"]),
        model=str(row["model"]),
        replacement_counts={str(k): int(v) for k, v in dict(replacement_counts).items()},
        truncated=bool(row["truncated"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
