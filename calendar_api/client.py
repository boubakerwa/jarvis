"""
Google Calendar API client.
Uses the same OAuth token as Drive and Gmail (shared token.json).
NOTE: Adding calendar scope requires deleting token.json and re-authenticating.
"""
import logging
import os
from datetime import date, datetime, timedelta, timezone
from time import monotonic

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import settings
from core.opslog import record_activity, record_audit, record_issue

logger = logging.getLogger(__name__)


class CalendarClient:
    def __init__(self):
        self._service = self._build_service()

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

        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def get_events(
        self,
        time_min: str,
        time_max: str,
        max_results: int = 20,
    ) -> list[dict]:
        """
        Query upcoming calendar events between time_min and time_max (ISO 8601).
        Returns list of {summary, start, end, description, location, id}.
        """
        started = monotonic()
        try:
            result = (
                self._service.events()
                .list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            events = []
            for item in result.get("items", []):
                start = item.get("start", {})
                end = item.get("end", {})
                events.append(
                    {
                        "id": item.get("id", ""),
                        "summary": item.get("summary", "(no title)"),
                        "start": start.get("dateTime", start.get("date", "")),
                        "end": end.get("dateTime", end.get("date", "")),
                        "description": item.get("description", ""),
                        "location": item.get("location", ""),
                    }
                )
            record_activity(
                event="calendar_query_completed",
                component="calendar",
                summary="Calendar query completed",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"result_count": len(events)},
            )
            return events
        except Exception as exc:
            record_issue(
                level="ERROR",
                event="calendar_query_failed",
                component="calendar",
                status="error",
                summary="Calendar query failed",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"error": str(exc)},
            )
            raise

    def create_event(
        self,
        summary: str,
        start: str,
        end: str = "",
        description: str = "",
        location: str = "",
        all_day: bool = False,
    ) -> dict:
        """
        Create a calendar event. start/end must be ISO 8601 with timezone.
        If end is omitted, defaults to 1 hour after start.
        Returns {id, summary, htmlLink}.
        """
        if not end and not all_day:
            # Parse start and add 1 hour
            try:
                dt = datetime.fromisoformat(start)
                end = (dt + timedelta(hours=1)).isoformat()
            except ValueError:
                end = start  # fallback: same time

        body = {"summary": summary}
        if all_day:
            end_date = end
            if not end_date:
                end_date = (date.fromisoformat(start) + timedelta(days=1)).isoformat()
            body["start"] = {"date": start}
            body["end"] = {"date": end_date}
        else:
            body["start"] = {"dateTime": start}
            body["end"] = {"dateTime": end}
        if description:
            body["description"] = description
        if location:
            body["location"] = location

        started = monotonic()
        try:
            event = self._service.events().insert(calendarId="primary", body=body).execute()
            logger.info("Created calendar event: %s (ID: %s)", summary, event.get("id"))
            record_activity(
                event="calendar_event_created",
                component="calendar",
                summary="Created calendar event",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"all_day": all_day},
            )
            record_audit(
                event="calendar_event_created",
                component="calendar",
                summary="Created calendar event",
                metadata={"all_day": all_day},
            )
            return {
                "id": event.get("id", ""),
                "summary": event.get("summary", ""),
                "htmlLink": event.get("htmlLink", ""),
                "start": event.get("start", {}).get("dateTime", event.get("start", {}).get("date", start)),
                "end": event.get("end", {}).get("dateTime", event.get("end", {}).get("date", end)),
            }
        except Exception as exc:
            record_issue(
                level="ERROR",
                event="calendar_event_create_failed",
                component="calendar",
                status="error",
                summary="Failed to create calendar event",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"error": str(exc), "all_day": all_day},
            )
            raise
