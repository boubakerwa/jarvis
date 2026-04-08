"""
Gmail polling loop. Checks for new unread emails every GMAIL_POLL_INTERVAL seconds.
Marks emails as read after processing. Persists last-processed message ID to avoid
re-processing on restart.
"""
import json
import logging
import os
import time
from datetime import date, datetime, time as dt_time
from time import monotonic
from typing import Callable, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import settings
from core.opslog import new_op_id, operation_context, record_activity, record_issue
from gmail.parser import ParsedEmail, parse_message

logger = logging.getLogger(__name__)

_STATE_FILE = "./data/gmail_state.txt"


class GmailWatcher:
    def __init__(self, on_email: Callable[[ParsedEmail], None]):
        """
        on_email: callback invoked for each new unread email.
        """
        self._on_email = on_email
        self._service = self._build_service()
        self._cutoff_date, self._last_history_id = self._load_state()
        logger.info("Gmail watcher cutoff date: %s", self._cutoff_date)
        record_activity(
            event="gmail_watcher_initialised",
            component="gmail",
            summary="Gmail watcher initialised",
            metadata={"cutoff_date": self._cutoff_date},
        )

    def run_forever(self) -> None:
        """Block and poll Gmail indefinitely."""
        logger.info("Gmail watcher started. Poll interval: %ds", settings.GMAIL_POLL_INTERVAL)
        record_activity(
            event="gmail_watcher_started",
            component="gmail",
            summary="Gmail watcher started",
            metadata={"poll_interval_seconds": settings.GMAIL_POLL_INTERVAL},
        )
        while True:
            op_id = new_op_id("gmail-poll")
            started = monotonic()
            try:
                with operation_context(op_id):
                    unread_count = self._poll()
                record_activity(
                    event="gmail_poll_completed",
                    component="gmail",
                    summary="Gmail poll completed",
                    duration_ms=(monotonic() - started) * 1000,
                    op_id=op_id,
                    metadata={"unread_count": unread_count},
                )
            except Exception:
                logger.exception("Error during Gmail poll")
                record_issue(
                    level="ERROR",
                    event="gmail_poll_failed",
                    component="gmail",
                    status="error",
                    summary="Gmail poll failed",
                    duration_ms=(monotonic() - started) * 1000,
                    op_id=op_id,
                )
            time.sleep(settings.GMAIL_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _poll(self) -> int:
        unread = self._fetch_unread_ids()
        if not unread:
            logger.debug("No new unread emails")
            record_activity(
                event="gmail_poll_empty",
                component="gmail",
                summary="No new unread emails",
            )
            return 0

        logger.info("Processing %d new email(s)", len(unread))
        for message_id in unread:
            try:
                email = parse_message(self._service, message_id)
                self._on_email(email)
                self._mark_read(message_id)
                self._save_state(message_id)
            except Exception:
                logger.exception("Failed to process message %s", message_id)
                record_issue(
                    level="ERROR",
                    event="gmail_message_processing_failed",
                    component="gmail",
                    status="error",
                    summary="Failed to process Gmail message",
                    metadata={"message_id": message_id},
                )
        return len(unread)

    def _fetch_unread_ids(self) -> list[str]:
        cutoff = date.fromisoformat(self._cutoff_date)
        query_anchor = cutoff.strftime("%Y/%m/%d")
        query = f"is:unread after:{query_anchor}"

        page_token = None
        unread_ids: list[str] = []
        ignored_count = 0

        while True:
            result = self._service.users().messages().list(
                userId="me",
                q=query,
                maxResults=100,
                pageToken=page_token,
                fields="messages(id),nextPageToken",
            ).execute()
            messages = result.get("messages", [])

            for message in messages:
                if self._is_on_or_after_cutoff(message["id"]):
                    unread_ids.append(message["id"])
                else:
                    ignored_count += 1

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        if ignored_count:
            logger.info(
                "Ignoring %d unread email(s) before cutoff date %s",
                ignored_count,
                self._cutoff_date,
            )
        return unread_ids

    def _is_on_or_after_cutoff(self, message_id: str) -> bool:
        meta = self._service.users().messages().get(
            userId="me",
            id=message_id,
            format="minimal",
            fields="id,internalDate",
        ).execute()
        internal_date_ms = int(meta.get("internalDate", "0"))
        return internal_date_ms >= self._cutoff_timestamp_ms()

    def _mark_read(self, message_id: str) -> None:
        self._service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self, last_id: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(_STATE_FILE)), exist_ok=True)
        with open(_STATE_FILE, "w") as f:
            json.dump(
                {
                    "cutoff_date": self._cutoff_date,
                    "last_message_id": last_id,
                },
                f,
            )
        self._last_history_id = last_id

    def _load_state(self) -> tuple[str, Optional[str]]:
        cutoff_date = self._default_cutoff_date()
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE) as f:
                raw = f.read().strip()
            if not raw:
                return cutoff_date, None

            if raw.startswith("{"):
                try:
                    data = json.loads(raw)
                    return cutoff_date, data.get("last_message_id")
                except json.JSONDecodeError:
                    logger.warning("Invalid gmail state file detected, resetting state.")
                    return cutoff_date, None

            # Legacy state file: preserve the last message id, but initialize the cutoff fresh.
            return cutoff_date, raw
        return cutoff_date, None

    def _default_cutoff_date(self) -> str:
        if settings.GMAIL_START_DATE:
            return settings.GMAIL_START_DATE
        return datetime.now().astimezone().date().isoformat()

    def _cutoff_timestamp_ms(self) -> int:
        cutoff = date.fromisoformat(self._cutoff_date)
        tz = datetime.now().astimezone().tzinfo
        cutoff_dt = datetime.combine(cutoff, dt_time.min, tzinfo=tz)
        return int(cutoff_dt.timestamp() * 1000)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _build_service(self):
        creds = None
        token_path = settings.GOOGLE_TOKEN_PATH
        creds_path = settings.GOOGLE_CREDENTIALS_PATH

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, settings.GOOGLE_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(creds_path, settings.GOOGLE_SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds, cache_discovery=False)
