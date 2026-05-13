from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from time import monotonic
from typing import Any, TYPE_CHECKING

from config import settings
from core.opslog import new_op_id, operation_context, record_activity, record_audit, record_issue
from core.structured_output import generate_validated_json
from core.time_utils import day_bounds_for_calendar, get_local_now, get_local_timezone

if TYPE_CHECKING:
    from telegram_bot.bot import TelegramProactiveNotifier

logger = logging.getLogger(__name__)

_CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS daily_planner_sessions (
    id TEXT PRIMARY KEY,
    plan_date TEXT NOT NULL,
    status TEXT NOT NULL,
    raw_input TEXT,
    tasks_json TEXT,
    scheduled_json TEXT,
    unscheduled_json TEXT,
    plan_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_daily_planner_sessions_status ON daily_planner_sessions(status);
CREATE INDEX IF NOT EXISTS idx_daily_planner_sessions_plan_date ON daily_planner_sessions(plan_date);
"""

_ACTIVE_STATUSES = ("awaiting_tasks", "awaiting_prioritization")
_URGENCY_SCORE = {
    "critical": 4,
    "urgent": 4,
    "high": 3,
    "medium": 2,
    "normal": 2,
    "low": 1,
}
_COMPLEXITY_ESTIMATES = {
    "simple": 30,
    "easy": 30,
    "small": 30,
    "medium": 60,
    "moderate": 60,
    "complex": 120,
    "hard": 120,
    "large": 120,
}


@dataclass
class PlannerTask:
    title: str
    urgency: str
    estimate_minutes: int
    complexity: str = ""
    dependency: str = ""
    window_start: str = ""
    window_end: str = ""
    original_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "urgency": self.urgency,
            "estimate_minutes": self.estimate_minutes,
            "complexity": self.complexity,
            "dependency": self.dependency,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "original_index": self.original_index,
        }


@dataclass
class ScheduledBlock:
    task: PlannerTask
    start: datetime
    end: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


@dataclass
class PlannerResult:
    scheduled: list[ScheduledBlock] = field(default_factory=list)
    unscheduled: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PlannerMessageResult:
    handled: bool
    text: str = ""


def _utc_iso(now: datetime | None = None) -> str:
    return get_local_now(now).astimezone(timezone.utc).isoformat()


def _parse_clock(value: str, default: str) -> tuple[int, int]:
    raw = (value or default).strip() or default
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        logger.warning("Invalid planner clock value '%s', using %s", raw, default)
        raw = default
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    assert match is not None
    hour = max(0, min(23, int(match.group(1))))
    minute = max(0, min(59, int(match.group(2))))
    return hour, minute


def _local_datetime(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, dt_time(hour=hour, minute=minute), tzinfo=get_local_timezone())


def _time_from_hhmm(value: str, day: date) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return _local_datetime(day, hour, minute)


def _extract_dependency_window(text: str, day: date) -> tuple[str, str]:
    lowered = str(text or "").lower()

    opening = re.search(
        r"(?:opening hours|open|available|window)\D+(\d{1,2})(?::(\d{2}))?\D+(?:-|to|until|till)\D*(\d{1,2})(?::(\d{2}))?",
        lowered,
    )
    if opening:
        start = f"{int(opening.group(1)):02d}:{int(opening.group(2) or 0):02d}"
        end = f"{int(opening.group(3)):02d}:{int(opening.group(4) or 0):02d}"
        return start, end

    start = ""
    end = ""
    after = re.search(r"\b(?:after|from|not before)\s+(\d{1,2})(?::(\d{2}))?", lowered)
    before = re.search(r"\b(?:before|by|until|till)\s+(\d{1,2})(?::(\d{2}))?", lowered)
    if after:
        start = f"{int(after.group(1)):02d}:{int(after.group(2) or 0):02d}"
    if before:
        end = f"{int(before.group(1)):02d}:{int(before.group(2) or 0):02d}"
    return start, end


def _estimate_minutes(raw: Any, complexity: str) -> int | None:
    if isinstance(raw, (int, float)) and int(raw) > 0:
        return int(raw)
    text = str(raw or "").strip().lower()
    if text:
        hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", text)
        minutes = re.search(r"(\d+)\s*(?:m|min|mins|minute|minutes)\b", text)
        total = 0
        if hours:
            total += int(float(hours.group(1)) * 60)
        if minutes:
            total += int(minutes.group(1))
        if total > 0:
            return total
        if text.isdigit() and int(text) > 0:
            return int(text)

    complexity_key = str(complexity or "").strip().lower()
    return _COMPLEXITY_ESTIMATES.get(complexity_key)


def _validate_parsed_tasks(data: dict[str, Any]) -> list[PlannerTask]:
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("tasks must be a non-empty list")

    tasks: list[PlannerTask] = []
    problems: list[str] = []
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            problems.append(f"Task {index} is not an object")
            continue

        title = str(item.get("title") or item.get("task") or item.get("description") or "").strip()
        urgency = str(item.get("urgency") or "").strip().lower()
        complexity = str(item.get("complexity") or "").strip().lower()
        dependency = str(item.get("dependency") or item.get("window") or "").strip()
        estimate = _estimate_minutes(item.get("estimate_minutes") or item.get("estimate"), complexity)
        extracted_start, extracted_end = _extract_dependency_window(dependency, get_local_now().date())
        window_start = str(item.get("window_start") or extracted_start or "").strip()
        window_end = str(item.get("window_end") or extracted_end or "").strip()

        if not title:
            problems.append(f"Task {index} is missing a title")
        if urgency not in _URGENCY_SCORE:
            problems.append(f"Task {index} is missing urgency")
        if estimate is None:
            problems.append(f"Task {index} is missing estimate or complexity")
        if title and urgency in _URGENCY_SCORE and estimate is not None:
            tasks.append(
                PlannerTask(
                    title=title,
                    urgency="critical" if urgency == "urgent" else urgency,
                    estimate_minutes=max(5, int(estimate)),
                    complexity=complexity,
                    dependency=dependency,
                    window_start=window_start,
                    window_end=window_end,
                    original_index=index,
                )
            )

    if problems:
        raise ValueError("; ".join(problems))
    return tasks


def parse_tasks_from_text(text: str) -> list[PlannerTask]:
    return generate_validated_json(
        task="daily_planner",
        max_tokens=1400,
        system=(
            "Extract a user's daily task list for scheduling. Return JSON only with "
            "{\"tasks\":[{\"title\":\"...\",\"urgency\":\"critical|high|medium|low\","
            "\"estimate_minutes\":60,\"complexity\":\"simple|medium|complex\","
            "\"dependency\":\"...\",\"window_start\":\"HH:MM\",\"window_end\":\"HH:MM\"}]}. "
            "Every task must have urgency and either estimate_minutes or complexity. "
            "Use window_start/window_end only for explicit timing constraints."
        ),
        messages=[{"role": "user", "content": text}],
        validator=_validate_parsed_tasks,
        allow_fallback=True,
    )


def seconds_until_next_planner_run(now: datetime | None = None) -> float:
    current = get_local_now(now)
    hour, minute = _parse_clock(settings.JARVIS_DAILY_PLANNER_TIME, "08:30")
    target_day = current.date()
    for _ in range(8):
        target = _local_datetime(target_day, hour, minute)
        if target_day.weekday() < 6 and current < target:
            return (target - current).total_seconds()
        target_day += timedelta(days=1)
    return 24 * 60 * 60


class DailyPlannerManager:
    def __init__(
        self,
        *,
        memory_manager=None,
        reminder_manager=None,
        calendar_client=None,
        db_path: str | None = None,
        parser=parse_tasks_from_text,
    ) -> None:
        path = db_path or settings.JARVIS_DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_CREATE_SESSIONS_TABLE)
        self._db.commit()
        self._memory = memory_manager
        self._reminders = reminder_manager
        self._calendar = calendar_client
        self._parser = parser

    def start_today_session(self, *, now: datetime | None = None) -> dict | None:
        current = get_local_now(now)
        if current.weekday() >= 6:
            return None

        plan_date = current.date().isoformat()
        existing = self._latest_session(plan_date)
        if existing and existing["status"] == "scheduled":
            return None
        if existing and existing["status"] in {"awaiting_tasks", "awaiting_prioritization"}:
            return existing

        now_iso = _utc_iso(current)
        session = {
            "id": str(uuid.uuid4()),
            "plan_date": plan_date,
            "status": "awaiting_tasks",
            "raw_input": None,
            "tasks_json": None,
            "scheduled_json": None,
            "unscheduled_json": None,
            "plan_message": None,
            "created_at": now_iso,
            "updated_at": now_iso,
            "completed_at": None,
        }
        self._db.execute(
            """
            INSERT INTO daily_planner_sessions (
                id, plan_date, status, raw_input, tasks_json, scheduled_json, unscheduled_json,
                plan_message, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["id"],
                session["plan_date"],
                session["status"],
                session["raw_input"],
                session["tasks_json"],
                session["scheduled_json"],
                session["unscheduled_json"],
                session["plan_message"],
                session["created_at"],
                session["updated_at"],
                session["completed_at"],
            ),
        )
        self._db.commit()
        record_audit(
            event="daily_planner_session_started",
            component="daily_planner",
            summary="Started daily planning session",
            metadata={"session_id": session["id"], "plan_date": plan_date},
        )
        return session

    def build_prompt(self, *, now: datetime | None = None) -> str:
        current = get_local_now(now)
        end_hour, end_minute = _parse_clock(settings.JARVIS_DAILY_PLANNER_END_TIME, "18:00")
        lines = [
            f"Good morning. Think through what you want to achieve today ({current.strftime('%A, %B %-d')}).",
            f"Send today's tasks in this format. I will schedule what is realistic until {end_hour:02d}:{end_minute:02d}.",
            "",
            "Task | urgency | estimate or complexity | dependency/window",
            "Example: Call tax office | high | 30m | opening hours 09:00-12:00",
            "",
        ]
        context = self._today_context(current)
        if context:
            lines.extend(["Today context:", *context, ""])
        lines.append("You can also reply `skip`, `cancel`, or `done planning`.")
        return "\n".join(lines)

    def handle_user_message(self, text: str, *, now: datetime | None = None) -> PlannerMessageResult:
        session = self.get_active_session()
        if session is None:
            return PlannerMessageResult(False)

        current = get_local_now(now)
        cleaned = (text or "").strip()
        lowered = cleaned.lower()
        if lowered in {"skip", "cancel", "done planning"}:
            status = "skipped" if lowered == "skip" else "cancelled"
            self._update_session(session["id"], status=status, completed_at=_utc_iso(current))
            return PlannerMessageResult(True, "Daily planning closed. No reminders were scheduled.")

        if session["status"] == "awaiting_prioritization":
            return PlannerMessageResult(True, self._handle_prioritization(session, cleaned, now=current))
        return PlannerMessageResult(True, self._handle_task_intake(session, cleaned, now=current))

    def get_active_session(self) -> dict | None:
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        row = self._db.execute(
            f"""
            SELECT * FROM daily_planner_sessions
            WHERE status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT 1
            """,
            _ACTIVE_STATUSES,
        ).fetchone()
        return dict(row) if row else None

    def build_plan(self, tasks: list[PlannerTask], *, now: datetime | None = None) -> PlannerResult:
        current = get_local_now(now)
        start_hour, start_minute = _parse_clock(settings.JARVIS_DAILY_PLANNER_TIME, "08:30")
        end_hour, end_minute = _parse_clock(settings.JARVIS_DAILY_PLANNER_END_TIME, "18:00")
        buffer_minutes = max(0, int(settings.JARVIS_DAILY_PLANNER_BUFFER_MINUTES))
        day = current.date()
        work_start = _local_datetime(day, start_hour, start_minute)
        work_end = _local_datetime(day, end_hour, end_minute)
        cursor = max(current.replace(second=0, microsecond=0), work_start)
        remaining = list(tasks)
        scheduled: list[ScheduledBlock] = []
        unscheduled: list[dict[str, Any]] = []

        while remaining and cursor < work_end:
            eligible: list[tuple[int, int, PlannerTask, datetime, datetime]] = []
            future_starts: list[datetime] = []
            for task in remaining:
                window_start = _time_from_hhmm(task.window_start, day) or work_start
                window_end = _time_from_hhmm(task.window_end, day) or work_end
                if window_end > work_end:
                    window_end = work_end
                earliest = max(cursor, window_start)
                finish = earliest + timedelta(minutes=task.estimate_minutes)
                if window_start > cursor:
                    future_starts.append(window_start)
                    continue
                if earliest >= work_end or finish > window_end or finish > work_end:
                    continue
                eligible.append((-_URGENCY_SCORE[task.urgency], task.original_index, task, earliest, finish))

            if not eligible:
                future = [value for value in future_starts if value > cursor]
                if not future:
                    break
                cursor = min(future)
                continue

            _, _, task, start, end = sorted(eligible, key=lambda item: (item[0], item[1]))[0]
            scheduled.append(ScheduledBlock(task=task, start=start, end=end))
            remaining.remove(task)
            cursor = end + timedelta(minutes=buffer_minutes)

        for task in remaining:
            unscheduled.append(
                {
                    "task": task.to_dict(),
                    "reason": f"Does not fit before {end_hour:02d}:{end_minute:02d} or inside its dependency window.",
                }
            )
        return PlannerResult(scheduled=scheduled, unscheduled=unscheduled)

    def _handle_task_intake(self, session: dict, text: str, *, now: datetime) -> str:
        try:
            tasks = self._parser(text)
        except Exception as exc:
            return (
                "I could not turn that into a schedule yet. "
                "Please include each task with urgency and an estimate or complexity.\n"
                f"Problem: {exc}"
            )

        result = self.build_plan(tasks, now=now)
        self._update_session(
            session["id"],
            raw_input=text,
            tasks_json=json.dumps([task.to_dict() for task in tasks], ensure_ascii=False),
            scheduled_json=json.dumps([block.to_dict() for block in result.scheduled], ensure_ascii=False),
            unscheduled_json=json.dumps(result.unscheduled, ensure_ascii=False),
        )

        if result.unscheduled:
            message = self._format_prioritization_request(tasks, result, now=now)
            self._update_session(session["id"], status="awaiting_prioritization", plan_message=message)
            return message

        message = self._commit_plan(session["id"], result, now=now)
        return message

    def _handle_prioritization(self, session: dict, text: str, *, now: datetime) -> str:
        indexes = [int(value) for value in re.findall(r"\d+", text)]
        tasks = [PlannerTask(**task) for task in json.loads(session.get("tasks_json") or "[]")]
        selected = [task for task in tasks if task.original_index in indexes]
        if not selected:
            return "Reply with the task numbers you want to keep today, for example `1, 3, 4`, or `cancel`."

        result = self.build_plan(selected, now=now)
        if result.unscheduled:
            message = self._format_prioritization_request(selected, result, now=now)
            self._update_session(
                session["id"],
                scheduled_json=json.dumps([block.to_dict() for block in result.scheduled], ensure_ascii=False),
                unscheduled_json=json.dumps(result.unscheduled, ensure_ascii=False),
                plan_message=message,
            )
            return message

        return self._commit_plan(session["id"], result, now=now)

    def _commit_plan(self, session_id: str, result: PlannerResult, *, now: datetime) -> str:
        scheduled_json = json.dumps([block.to_dict() for block in result.scheduled], ensure_ascii=False)
        unscheduled_json = json.dumps(result.unscheduled, ensure_ascii=False)
        can_schedule_reminders = bool(self._memory and self._reminders)
        message = self._format_final_plan(result, reminders_scheduled=can_schedule_reminders)

        if can_schedule_reminders:
            for block in result.scheduled:
                task = self._memory.create_task(block.task.title, now.date().isoformat(), source="daily_planner")
                reminder_start = block.start
                if reminder_start.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
                    reminder_start = now + timedelta(minutes=1)
                self._reminders.schedule_message(
                    f"Start: {block.task.title}",
                    reminder_start.isoformat(),
                    task_id=task["id"],
                    until_task_done=True,
                    now=now,
                )

        self._update_session(
            session_id,
            status="scheduled",
            scheduled_json=scheduled_json,
            unscheduled_json=unscheduled_json,
            plan_message=message,
            completed_at=_utc_iso(now),
        )
        record_audit(
            event="daily_plan_scheduled",
            component="daily_planner",
            summary="Daily plan committed and reminders scheduled",
            metadata={"session_id": session_id, "scheduled_count": len(result.scheduled)},
        )
        return message

    def _format_prioritization_request(self, tasks: list[PlannerTask], result: PlannerResult, *, now: datetime) -> str:
        lines = [
            "This is more than realistically fits today. No reminders have been scheduled yet.",
            "",
            "Current best fit:",
        ]
        if result.scheduled:
            lines.extend(self._format_blocks(result.scheduled, include_original_index=True))
        else:
            lines.append("- Nothing fits with the current estimates/windows.")
        lines.extend(["", "Not fitting today:"])
        for item in result.unscheduled:
            task = item["task"]
            lines.append(f"- {task['original_index']}. {task['title']} ({task['estimate_minutes']}m, {task['urgency']})")
        lines.extend(["", "Reply with the task numbers to keep today, for example `1, 3, 4`, or `cancel`."])
        return "\n".join(lines)

    def _format_final_plan(self, result: PlannerResult, *, reminders_scheduled: bool = True) -> str:
        end_hour, end_minute = _parse_clock(settings.JARVIS_DAILY_PLANNER_END_TIME, "18:00")
        lines = [f"Today's realistic plan until {end_hour:02d}:{end_minute:02d}:", ""]
        if result.scheduled:
            lines.extend(self._format_blocks(result.scheduled))
        else:
            lines.append("- No tasks scheduled.")
        if result.unscheduled:
            lines.extend(["", "Not scheduled today:"])
            for item in result.unscheduled:
                task = item["task"]
                lines.append(f"- {task['title']}: {item['reason']}")
        if reminders_scheduled:
            lines.extend(["", "I scheduled start-time reminders with Done/Later buttons."])
        else:
            lines.extend(["", "Reminder scheduling is unavailable, so I only saved the plan."])
        return "\n".join(lines)

    def _format_blocks(self, blocks: list[ScheduledBlock], *, include_original_index: bool = False) -> list[str]:
        lines = []
        for block in blocks:
            task = block.task
            prefix = f"{task.original_index}. " if include_original_index else ""
            lines.append(
                f"- {block.start.strftime('%H:%M')}-{block.end.strftime('%H:%M')}: "
                f"{prefix}{task.title} ({task.urgency}, {task.estimate_minutes}m)"
            )
        return lines

    def _today_context(self, now: datetime) -> list[str]:
        lines: list[str] = []
        if self._memory:
            try:
                tasks = self._memory.list_tasks("pending")[:5]
                if tasks:
                    lines.append("Open tasks: " + "; ".join(str(task.get("description")) for task in tasks))
            except Exception as exc:
                logger.debug("Daily planner task context unavailable: %s", exc)
        if self._reminders:
            try:
                reminders = self._reminders.list_reminders("scheduled")[:5]
                if reminders:
                    lines.append("Active reminders: " + "; ".join(str(reminder.get("message")) for reminder in reminders))
            except Exception as exc:
                logger.debug("Daily planner reminder context unavailable: %s", exc)
        if self._calendar:
            try:
                time_min, time_max = day_bounds_for_calendar(now.date(), now=now)
                events = self._calendar.get_events(time_min, time_max, max_results=5)
                if events:
                    event_text = []
                    for event in events:
                        start = str(event.get("start") or "")
                        label = start[11:16] if len(start) >= 16 else "time?"
                        event_text.append(f"{label} {event.get('summary') or '(no title)'}")
                    lines.append("Calendar today: " + "; ".join(event_text))
            except Exception as exc:
                logger.debug("Daily planner calendar context unavailable: %s", exc)
        return lines

    def _latest_session(self, plan_date: str) -> dict | None:
        row = self._db.execute(
            """
            SELECT * FROM daily_planner_sessions
            WHERE plan_date=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (plan_date,),
        ).fetchone()
        return dict(row) if row else None

    def _update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _utc_iso()
        assignments = ", ".join(f"{key}=?" for key in fields)
        self._db.execute(
            f"UPDATE daily_planner_sessions SET {assignments} WHERE id=?",
            (*fields.values(), session_id),
        )
        self._db.commit()


class DailyPlannerRunner:
    def __init__(
        self,
        *,
        manager: DailyPlannerManager,
        notifier: "TelegramProactiveNotifier",
    ) -> None:
        self._manager = manager
        self._notifier = notifier

    def run_forever(self) -> None:
        logger.info("Daily planner scheduled for %s local time", settings.JARVIS_DAILY_PLANNER_TIME)
        while True:
            sleep_secs = seconds_until_next_planner_run()
            logger.debug("Daily planner sleeping %.0fs until next prompt", sleep_secs)
            time.sleep(sleep_secs)
            self._send_prompt()

    def _send_prompt(self) -> None:
        op_id = new_op_id("daily-planner")
        started = monotonic()
        with operation_context(op_id):
            try:
                session = self._manager.start_today_session()
                if session is None:
                    return
                sent = self._notifier.send_message(self._manager.build_prompt())
                duration_ms = (monotonic() - started) * 1000
                if sent:
                    record_activity(
                        event="daily_planner_prompt_sent",
                        component="daily_planner",
                        summary="Daily planning prompt sent via Telegram",
                        duration_ms=duration_ms,
                        metadata={"session_id": session["id"]},
                    )
                else:
                    record_issue(
                        level="WARNING",
                        event="daily_planner_prompt_send_failed",
                        component="daily_planner",
                        status="warning",
                        summary="Daily planning prompt could not be delivered",
                        duration_ms=duration_ms,
                        metadata={"session_id": session["id"]},
                    )
            except Exception as exc:
                logger.exception("Daily planner prompt failed: %s", exc)
                record_issue(
                    level="ERROR",
                    event="daily_planner_prompt_error",
                    component="daily_planner",
                    status="error",
                    summary="Daily planner prompt raised an unexpected error",
                    duration_ms=(monotonic() - started) * 1000,
                    metadata={"error": str(exc)},
                )
