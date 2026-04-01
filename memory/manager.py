import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import chromadb

from config import settings
from memory.schema import MemoryCategory, MemoryRecord

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    summary TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    document_ref TEXT,
    supersedes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_memories_topic ON memories(topic);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_active ON memories(active);
"""


class MemoryManager:
    def __init__(self):
        os.makedirs(os.path.dirname(os.path.abspath(settings.JARVIS_DB_PATH)), exist_ok=True)
        self._db = sqlite3.connect(settings.JARVIS_DB_PATH, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_CREATE_TABLE)
        self._db.commit()

        self._chroma = chromadb.PersistentClient(path=settings.JARVIS_CHROMA_PATH)
        self._collection = self._chroma.get_or_create_collection(
            name="memories",
            metadata={"hnsw:space": "cosine"},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert(self, record: MemoryRecord) -> MemoryRecord:
        """Insert or update a memory by topic. Soft-deletes the previous record."""
        existing = self._get_active_by_topic(record.topic)
        if existing:
            self._soft_delete(existing.id)
            record.supersedes = existing.id
            record.created_at = existing.created_at  # preserve original creation time

        record.updated_at = datetime.now(timezone.utc).isoformat()
        self._sqlite_insert(record)
        self._chroma_upsert(record)
        logger.info("Memory upserted: topic=%s id=%s", record.topic, record.id)
        return record

    def forget(self, topic: str) -> bool:
        """Soft-delete all active memories with the given topic."""
        existing = self._get_active_by_topic(topic)
        if not existing:
            return False
        self._soft_delete(existing.id)
        self._chroma_delete(existing.id)
        logger.info("Memory forgotten: topic=%s", topic)
        return True

    def search(self, query: str, n_results: int = 8) -> list[MemoryRecord]:
        """Semantic search over active memories."""
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(n_results, self._collection.count()),
                where={"active": 1},
            )
        except Exception:
            return []

        records = []
        for doc_id in (results["ids"][0] if results["ids"] else []):
            row = self._sqlite_get(doc_id)
            if row:
                records.append(row)
        return records

    def list_all(self, category: Optional[MemoryCategory] = None) -> list[MemoryRecord]:
        """Return all active memories, optionally filtered by category."""
        if category:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE active=1 AND category=? ORDER BY topic",
                (category.value,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM memories WHERE active=1 ORDER BY category, topic"
            ).fetchall()
        return [MemoryRecord.from_dict(dict(r)) for r in rows]

    def get_by_topic(self, topic: str) -> Optional[MemoryRecord]:
        return self._get_active_by_topic(topic)

    def count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM memories WHERE active=1").fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_active_by_topic(self, topic: str) -> Optional[MemoryRecord]:
        row = self._db.execute(
            "SELECT * FROM memories WHERE topic=? AND active=1", (topic,)
        ).fetchone()
        return MemoryRecord.from_dict(dict(row)) if row else None

    def _sqlite_get(self, record_id: str) -> Optional[MemoryRecord]:
        row = self._db.execute(
            "SELECT * FROM memories WHERE id=?", (record_id,)
        ).fetchone()
        return MemoryRecord.from_dict(dict(row)) if row else None

    def _sqlite_insert(self, record: MemoryRecord) -> None:
        d = record.to_dict()
        self._db.execute(
            """INSERT INTO memories
               (id, topic, summary, category, source, confidence,
                document_ref, supersedes, created_at, updated_at, active)
               VALUES (:id, :topic, :summary, :category, :source, :confidence,
                       :document_ref, :supersedes, :created_at, :updated_at, :active)""",
            d,
        )
        self._db.commit()

    def _soft_delete(self, record_id: str) -> None:
        self._db.execute(
            "UPDATE memories SET active=0, updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), record_id),
        )
        self._db.commit()

    def _chroma_upsert(self, record: MemoryRecord) -> None:
        self._collection.upsert(
            ids=[record.id],
            documents=[record.summary],
            metadatas=[{"topic": record.topic, "category": record.category.value, "active": 1}],
        )

    def _chroma_delete(self, record_id: str) -> None:
        try:
            self._collection.update(ids=[record_id], metadatas=[{"active": 0}])
        except Exception:
            pass
