import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import chromadb

from config import settings
from core.opslog import record_audit
from memory.schema import MemoryCategory, MemoryRecord

logger = logging.getLogger(__name__)

_CREATE_FINANCIAL_TABLE = """
CREATE TABLE IF NOT EXISTS financial_records (
    id TEXT PRIMARY KEY,
    drive_file_id TEXT,
    vendor TEXT,
    amount REAL,
    currency TEXT DEFAULT 'EUR',
    category TEXT,
    date TEXT,
    description TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_financial_vendor ON financial_records(vendor);
CREATE INDEX IF NOT EXISTS idx_financial_date ON financial_records(date);
CREATE INDEX IF NOT EXISTS idx_financial_category ON financial_records(category);
"""

_CREATE_TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date);
"""

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
        self._db.executescript(_CREATE_TASKS_TABLE)
        self._db.executescript(_CREATE_FINANCIAL_TABLE)
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
        record_audit(
            event="memory_upserted",
            component="memory",
            summary="Stored or updated memory record",
            metadata={"topic": record.topic, "category": record.category.value},
        )
        return record

    def forget(self, topic: str) -> bool:
        """Soft-delete all active memories with the given topic."""
        existing = self._get_active_by_topic(topic)
        if not existing:
            return False
        self._soft_delete(existing.id)
        self._chroma_delete(existing.id)
        logger.info("Memory forgotten: topic=%s", topic)
        record_audit(
            event="memory_forgotten",
            component="memory",
            summary="Forgot memory record by topic",
            metadata={"topic": topic},
        )
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
    # Tasks
    # ------------------------------------------------------------------

    def create_task(self, description: str, due_date: Optional[str] = None) -> dict:
        import uuid
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "INSERT INTO tasks (id, description, due_date, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (task_id, description, due_date, now),
        )
        self._db.commit()
        logger.info("Task created: %s", description[:60])
        record_audit(
            event="task_created",
            component="memory",
            summary="Created task",
            metadata={"due_date": due_date or ""},
        )
        return {"id": task_id, "description": description, "due_date": due_date, "status": "pending"}

    def list_tasks(self, status: str = "pending") -> list[dict]:
        if status == "all":
            rows = self._db.execute(
                "SELECT * FROM tasks ORDER BY due_date, created_at"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY due_date, created_at",
                (status,),
            ).fetchall()
        return [dict(r) for r in rows]

    def complete_task(self, task_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        cursor = self._db.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=? AND status='pending'",
            (now, task_id),
        )
        self._db.commit()
        if cursor.rowcount > 0:
            record_audit(
                event="task_completed",
                component="memory",
                summary="Completed task",
                metadata={"task_id": task_id},
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Financial records
    # ------------------------------------------------------------------

    def add_financial_record(
        self,
        vendor: str,
        amount: float,
        currency: str,
        category: str,
        date: str,
        description: str,
        drive_file_id: str,
        source: str,
    ) -> dict:
        import uuid
        record_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            """INSERT INTO financial_records
               (id, drive_file_id, vendor, amount, currency, category, date, description, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record_id, drive_file_id, vendor, amount, currency, category, date, description, source, now),
        )
        self._db.commit()
        logger.info("Financial record added: %s %.2f %s from %s", category, amount, currency, vendor)
        record_audit(
            event="financial_record_added",
            component="memory",
            summary="Stored financial record",
            metadata={"vendor": vendor[:80], "category": category, "currency": currency},
        )
        return {
            "id": record_id,
            "vendor": vendor,
            "amount": amount,
            "currency": currency,
            "category": category,
            "date": date,
        }

    def query_financials(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        vendor: Optional[str] = None,
        category: Optional[str] = None,
    ) -> list[dict]:
        query = "SELECT * FROM financial_records WHERE 1=1"
        params: list = []
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        if vendor:
            query += " AND vendor LIKE ?"
            params.append(f"%{vendor}%")
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY date DESC"
        rows = self._db.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def financial_summary(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> dict:
        records = self.query_financials(start_date=start_date, end_date=end_date)
        by_category: dict[str, float] = {}
        by_vendor: dict[str, float] = {}
        total = 0.0
        for r in records:
            amt = r.get("amount") or 0.0
            cat = r.get("category", "other")
            vendor = r.get("vendor", "unknown")
            by_category[cat] = by_category.get(cat, 0.0) + amt
            by_vendor[vendor] = by_vendor.get(vendor, 0.0) + amt
            total += amt
        return {
            "total": total,
            "by_category": by_category,
            "by_vendor": by_vendor,
            "record_count": len(records),
        }

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
