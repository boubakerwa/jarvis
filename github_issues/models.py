from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

IssueIntent = Literal["create", "update", "status", "list", "help", "unknown"]


@dataclass(frozen=True)
class ParsedIssueCommand:
    intent: IssueIntent
    raw_text: str
    number: int | None = None
    title: str | None = None
    body: str | None = None
    labels: tuple[str, ...] | None = None
    state: str | None = None
    limit: int | None = None


@dataclass(frozen=True)
class IssueSummary:
    number: int
    title: str
    state: str
    url: str
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    updated_at: str | None = None


@dataclass(frozen=True)
class IssueAgentResponse:
    text: str
    intent: IssueIntent
    issue: IssueSummary | None = None
    issues: tuple[IssueSummary, ...] = ()
