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


@dataclass(frozen=True)
class PullRequestSummary:
    number: int
    title: str
    state: str
    url: str
    author: str | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    updated_at: str | None = None
    body: str | None = None
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    commit_count: int = 0


@dataclass(frozen=True)
class CommitSummary:
    sha: str
    short_sha: str
    message: str
    url: str
    author: str | None = None
    committed_at: str | None = None
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    files: tuple[str, ...] = ()
