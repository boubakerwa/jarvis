from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import settings

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_RELATIVE_DATE_PATTERNS = [
    re.compile(r"\bday after tomorrow\b", re.IGNORECASE),
    re.compile(r"\btomorrow\b", re.IGNORECASE),
    re.compile(r"\byesterday\b", re.IGNORECASE),
    re.compile(r"\btoday\b", re.IGNORECASE),
    re.compile(r"\b(?:next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE),
    re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE),
    re.compile(r"\bin\s+(\d+)\s+(day|days|week|weeks)\b", re.IGNORECASE),
]

_EXPLICIT_DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"),
]

_TIME_PATTERNS = [
    re.compile(r"\b(?:at\s+)?(\d{1,2}):(\d{2})\b", re.IGNORECASE),
    re.compile(r"\b(?:at\s+)?(\d{1,2})\s*(am|pm)\b", re.IGNORECASE),
    re.compile(r"\b(?:at\s+)?(\d{1,2})\b", re.IGNORECASE),
]


@dataclass
class ResolvedEventTime:
    start: str
    end: str
    all_day: bool


def get_local_timezone():
    tz_name = settings.JARVIS_TIMEZONE
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo


def get_local_now(now: Optional[datetime] = None) -> datetime:
    tz = get_local_timezone()
    base = now or datetime.now().astimezone()
    if base.tzinfo is None:
        return base.replace(tzinfo=tz)
    return base.astimezone(tz)


def get_current_time_context(now: Optional[datetime] = None) -> str:
    current = get_local_now(now)
    return (
        f"Current local datetime: {current.isoformat()} "
        f"({current.strftime('%A, %B %d, %Y %H:%M %Z')}). "
        "Use this as the source of truth for relative dates."
    )


def contains_explicit_date(text: str) -> bool:
    return any(pattern.search(text) for pattern in _EXPLICIT_DATE_PATTERNS)


def extract_relative_date_expression(text: str) -> Optional[str]:
    for pattern in _RELATIVE_DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def resolve_date_expression(value: str, *, now: Optional[datetime] = None) -> date:
    text = value.strip()
    if not text:
        raise ValueError("Empty date expression")

    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    current = get_local_now(now).date()
    lowered = text.lower().strip()

    if lowered == "today":
        return current
    if lowered == "tomorrow":
        return current + timedelta(days=1)
    if lowered == "yesterday":
        return current - timedelta(days=1)
    if lowered == "day after tomorrow":
        return current + timedelta(days=2)

    relative_match = re.fullmatch(r"in\s+(\d+)\s+(day|days|week|weeks)", lowered)
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2)
        delta_days = amount * (7 if "week" in unit else 1)
        return current + timedelta(days=delta_days)

    weekday_match = re.fullmatch(r"(?:next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", lowered)
    if weekday_match:
        target = _WEEKDAYS[weekday_match.group(1)]
        delta = (target - current.weekday()) % 7
        return current + timedelta(days=delta)

    if lowered in _WEEKDAYS:
        target = _WEEKDAYS[lowered]
        delta = (target - current.weekday()) % 7
        return current + timedelta(days=delta)

    raise ValueError(f"Unsupported date expression: {value}")


def _extract_time(value: str) -> Optional[tuple[int, int]]:
    lowered = value.lower()

    time_match = re.search(r"\b(?:at\s+)?(\d{1,2}):(\d{2})\b", lowered)
    if time_match:
        return int(time_match.group(1)), int(time_match.group(2))

    am_pm_match = re.search(r"\b(?:at\s+)?(\d{1,2})\s*(am|pm)\b", lowered)
    if am_pm_match:
        hour = int(am_pm_match.group(1)) % 12
        if am_pm_match.group(2) == "pm":
            hour += 12
        return hour, 0

    return None


def _strip_time_expression(value: str) -> str:
    text = re.sub(r"\b(?:at\s+)?\d{1,2}:\d{2}\b", "", value, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:at\s+)?\d{1,2}\s*(?:am|pm)\b", "", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def resolve_event_time(value: str, *, now: Optional[datetime] = None) -> ResolvedEventTime:
    text = value.strip()
    if not text:
        raise ValueError("Empty event time")

    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=get_local_timezone())
        end = dt + timedelta(hours=1)
        return ResolvedEventTime(start=dt.isoformat(), end=end.isoformat(), all_day=False)
    except ValueError:
        pass

    time_value = _extract_time(text)
    date_text = _strip_time_expression(text) if time_value is not None else text
    event_date = resolve_date_expression(date_text, now=now)
    if time_value is None:
        end_date = event_date + timedelta(days=1)
        return ResolvedEventTime(
            start=event_date.isoformat(),
            end=end_date.isoformat(),
            all_day=True,
        )

    hour, minute = time_value
    tz = get_local_timezone()
    start_dt = datetime.combine(event_date, time(hour=hour, minute=minute), tzinfo=tz)
    end_dt = start_dt + timedelta(hours=1)
    return ResolvedEventTime(start=start_dt.isoformat(), end=end_dt.isoformat(), all_day=False)


def day_bounds_for_calendar(target_date: date, *, now: Optional[datetime] = None) -> tuple[str, str]:
    tz = get_local_timezone() if now is None else get_local_now(now).tzinfo
    start_dt = datetime.combine(target_date, time.min, tzinfo=tz)
    end_dt = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=tz)
    return start_dt.isoformat(), end_dt.isoformat()
