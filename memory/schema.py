from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import uuid


class MemoryCategory(str, Enum):
    PREFERENCE = "preference"
    FACT = "fact"
    DECISION = "decision"
    DOCUMENT_REF = "document_ref"
    PROJECT = "project"
    HOUSEHOLD = "household"
    FINANCE = "finance"
    HEALTH = "health"


class MemorySource(str, Enum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    DOCUMENT = "document"
    MANUAL = "manual"


class MemoryConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class MemoryRecord:
    topic: str
    summary: str
    category: MemoryCategory
    source: MemorySource
    confidence: MemoryConfidence = MemoryConfidence.HIGH
    document_ref: Optional[str] = None      # Google Drive file ID
    supersedes: Optional[str] = None        # UUID of replaced record
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "topic": self.topic,
            "summary": self.summary,
            "category": self.category.value,
            "source": self.source.value,
            "confidence": self.confidence.value,
            "document_ref": self.document_ref,
            "supersedes": self.supersedes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active": 1 if self.active else 0,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryRecord":
        return cls(
            id=d["id"],
            topic=d["topic"],
            summary=d["summary"],
            category=MemoryCategory(d["category"]),
            source=MemorySource(d["source"]),
            confidence=MemoryConfidence(d["confidence"]),
            document_ref=d.get("document_ref"),
            supersedes=d.get("supersedes"),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            active=bool(d.get("active", 1)),
        )
