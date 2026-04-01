"""
Gmail polling loop. Checks for new unread emails every GMAIL_POLL_INTERVAL seconds.
Marks emails as read after processing. Persists last-processed message ID to avoid
re-processing on restart.
"""
import logging
import os
import time
from typing import Callable, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import settings
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
        self._last_history_id: Optional[str] = self._load_state()

    def run_forever(self) -> None:
        """Block and poll Gmail indefinitely."""
        logger.info("Gmail watcher started. Poll interval: %ds", settings.GMAIL_POLL_INTERVAL)
        while True:
            try:
                self._poll()
            except Exception:
                logger.exception("Error during Gmail poll")
            time.sleep(settings.GMAIL_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        unread = self._fetch_unread_ids()
        if not unread:
            logger.debug("No new unread emails")
            return

        logger.info("Processing %d new email(s)", len(unread))
        for message_id in unread:
            try:
                email = parse_message(self._service, message_id)
                self._on_email(email)
                self._mark_read(message_id)
                self._save_state(message_id)
            except Exception:
                logger.exception("Failed to process message %s", message_id)

    def _fetch_unread_ids(self) -> list[str]:
        result = self._service.users().messages().list(
            userId="me",
            q="is:unread",
            maxResults=50,
        ).execute()
        messages = result.get("messages", [])
        return [m["id"] for m in messages]

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
            f.write(last_id)
        self._last_history_id = last_id

    def _load_state(self) -> Optional[str]:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE) as f:
                return f.read().strip() or None
        return None

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

        return build("gmail", "v1", credentials=creds)
