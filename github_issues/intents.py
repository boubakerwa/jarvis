from __future__ import annotations

import re

from github_issues.models import ParsedIssueCommand

_PREFIX_RE = re.compile(r"^(?:/gh|gh|github)\b", re.IGNORECASE)
_STATE_VALUES = {"open", "closed", "all"}


def parse_issue_command(text: str) -> ParsedIssueCommand:
    raw_text = text
    stripped = text.strip()
    if not stripped:
        return ParsedIssueCommand(intent="help", raw_text=raw_text)

    prefix_match = _PREFIX_RE.match(stripped)
    if not prefix_match:
        return ParsedIssueCommand(intent="unknown", raw_text=raw_text)

    rest = stripped[prefix_match.end() :].strip()
    if not rest:
        return ParsedIssueCommand(intent="help", raw_text=raw_text)

    verb, _, tail = rest.partition(" ")
    verb = verb.lower().strip()
    tail = tail.strip()

    if verb in {"help", "commands"}:
        return ParsedIssueCommand(intent="help", raw_text=raw_text)
    if verb in {"create", "new"}:
        return _parse_create(raw_text, tail)
    if verb in {"status", "show", "get"}:
        return _parse_status(raw_text, tail)
    if verb in {"list", "ls"}:
        return _parse_list(raw_text, tail)
    if verb in {"update", "edit"}:
        return _parse_update(raw_text, tail)
    return ParsedIssueCommand(intent="unknown", raw_text=raw_text)


def _parse_create(raw_text: str, tail: str) -> ParsedIssueCommand:
    parts = [part.strip() for part in tail.split("|")] if tail else []
    title = parts[0] if parts else None
    body = parts[1] if len(parts) > 1 else None
    labels: tuple[str, ...] | None = None
    if len(parts) > 2:
        labels = _parse_labels_segment(parts[2])
    return ParsedIssueCommand(
        intent="create",
        raw_text=raw_text,
        title=title,
        body=body,
        labels=labels,
    )


def _parse_status(raw_text: str, tail: str) -> ParsedIssueCommand:
    token = tail.split(maxsplit=1)[0].strip() if tail else ""
    if not token.isdigit():
        return ParsedIssueCommand(intent="status", raw_text=raw_text)
    return ParsedIssueCommand(intent="status", raw_text=raw_text, number=int(token))


def _parse_list(raw_text: str, tail: str) -> ParsedIssueCommand:
    state = "open"
    limit = 5
    if not tail:
        return ParsedIssueCommand(intent="list", raw_text=raw_text, state=state, limit=limit)

    tokens = tail.split()
    for token in tokens:
        lowered = token.lower().strip()
        if lowered in _STATE_VALUES:
            state = lowered
            continue
        if lowered.isdigit():
            limit = max(1, min(int(lowered), 50))
    return ParsedIssueCommand(intent="list", raw_text=raw_text, state=state, limit=limit)


def _parse_update(raw_text: str, tail: str) -> ParsedIssueCommand:
    if not tail:
        return ParsedIssueCommand(intent="update", raw_text=raw_text)

    segments = [segment.strip() for segment in tail.split("|") if segment.strip()]
    if not segments:
        return ParsedIssueCommand(intent="update", raw_text=raw_text)

    first = segments[0].split(maxsplit=1)[0]
    if not first.isdigit():
        return ParsedIssueCommand(intent="update", raw_text=raw_text)

    number = int(first)
    title: str | None = None
    body: str | None = None
    labels: tuple[str, ...] | None = None
    state: str | None = None

    for segment in segments[1:]:
        key, sep, value = segment.partition("=")
        if not sep:
            if body is None:
                body = segment
            continue
        normalized_key = key.strip().lower()
        normalized_value = value.strip()
        if normalized_key == "title":
            title = normalized_value
        elif normalized_key == "body":
            body = normalized_value
        elif normalized_key == "labels":
            labels = _normalize_labels(normalized_value)
        elif normalized_key == "state":
            lowered = normalized_value.lower()
            if lowered in {"open", "closed"}:
                state = lowered

    return ParsedIssueCommand(
        intent="update",
        raw_text=raw_text,
        number=number,
        title=title,
        body=body,
        labels=labels,
        state=state,
    )


def _parse_labels_segment(segment: str) -> tuple[str, ...]:
    lowered = segment.strip().lower()
    if lowered.startswith("labels="):
        return _normalize_labels(segment.split("=", 1)[1])
    return _normalize_labels(segment)


def _normalize_labels(raw: str) -> tuple[str, ...]:
    labels = [label.strip() for label in raw.split(",")]
    cleaned = [label for label in labels if label]
    return tuple(cleaned)
