import logging
import os
from time import monotonic
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload
import io

from config import settings
from core.opslog import record_activity, record_audit, record_issue
from storage.schema import DRIVE_STRUCTURE, JARVIS_ROOT

logger = logging.getLogger(__name__)


class DriveClient:
    def __init__(self):
        self._service = self._build_service()
        self._folder_cache: dict[str, str] = {}  # path -> folder ID

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

        return build("drive", "v3", credentials=creds)

    # ------------------------------------------------------------------
    # Folder management
    # ------------------------------------------------------------------

    def init_drive_structure(self) -> str:
        """Create the Jarvis root and all top-level + default sub-folders. Returns root ID."""
        started = monotonic()
        try:
            root_id = self._get_or_create_folder(JARVIS_ROOT, parent_id=None)
            logger.info("Jarvis root folder ID: %s", root_id)

            for top_level, sub_folders in DRIVE_STRUCTURE.items():
                top_id = self._get_or_create_folder(top_level, parent_id=root_id)
                for sub in sub_folders:
                    self._get_or_create_folder(sub, parent_id=top_id)

            record_activity(
                event="drive_structure_ready",
                component="drive",
                summary="Drive folder structure ensured",
                duration_ms=(monotonic() - started) * 1000,
            )
            return root_id
        except Exception as exc:
            record_issue(
                level="ERROR",
                event="drive_structure_init_failed",
                component="drive",
                status="error",
                summary="Failed to initialise Drive folder structure",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"error": str(exc)},
            )
            raise

    def get_or_create_folder_path(self, top_level: str, sub_folder: str) -> str:
        """Return folder ID for Jarvis/{top_level}/{sub_folder}, creating if needed."""
        started = monotonic()
        try:
            root_id = self._get_folder_id(JARVIS_ROOT, parent_id=None)
            if not root_id:
                root_id = self.init_drive_structure()

            top_id = self._get_or_create_folder(top_level, parent_id=root_id)
            sub_id = self._get_or_create_folder(sub_folder, parent_id=top_id)
            record_activity(
                event="drive_folder_resolved",
                component="drive",
                summary="Resolved managed Drive folder path",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"top_level": top_level, "sub_folder": sub_folder},
            )
            return sub_id
        except Exception as exc:
            record_issue(
                level="ERROR",
                event="drive_folder_resolution_failed",
                component="drive",
                status="error",
                summary="Failed to resolve managed Drive folder path",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"top_level": top_level, "sub_folder": sub_folder, "error": str(exc)},
            )
            raise

    def _get_or_create_folder(self, name: str, parent_id: Optional[str]) -> str:
        existing = self._get_folder_id(name, parent_id)
        if existing:
            return existing

        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]

        folder = self._service.files().create(body=metadata, fields="id").execute()
        folder_id = folder["id"]
        logger.info("Created folder '%s' (ID: %s)", name, folder_id)
        record_audit(
            event="drive_folder_created",
            component="drive",
            summary="Created Drive folder",
            metadata={"name": name, "parent_id": parent_id or ""},
        )
        return folder_id

    def _get_folder_id(self, name: str, parent_id: Optional[str]) -> Optional[str]:
        query = (
            f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        if parent_id:
            query += f" and '{parent_id}' in parents"

        results = self._service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get("files", [])
        return files[0]["id"] if files else None

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------

    def upload_file(
        self,
        file_path: str,
        filename: str,
        folder_id: str,
        mime_type: str = "application/octet-stream",
    ) -> str:
        """Upload a local file to Drive. Returns the file ID."""
        started = monotonic()
        try:
            metadata = {"name": filename, "parents": [folder_id]}
            media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
            file = self._service.files().create(
                body=metadata, media_body=media, fields="id"
            ).execute()
            file_id = file["id"]
            logger.info("Uploaded '%s' to folder %s (ID: %s)", filename, folder_id, file_id)
            record_activity(
                event="drive_upload_completed",
                component="drive",
                summary="Uploaded file to Drive",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"filename": filename, "mime_type": mime_type},
            )
            record_audit(
                event="drive_file_uploaded",
                component="drive",
                summary="Uploaded file to Drive",
                metadata={"filename": filename, "mime_type": mime_type},
            )
            return file_id
        except Exception as exc:
            record_issue(
                level="ERROR",
                event="drive_upload_failed",
                component="drive",
                status="error",
                summary="Drive upload failed",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"filename": filename, "mime_type": mime_type, "error": str(exc)},
            )
            raise

    def upload_bytes(
        self,
        data: bytes,
        filename: str,
        folder_id: str,
        mime_type: str = "application/octet-stream",
    ) -> str:
        """Upload bytes directly to Drive. Returns the file ID."""
        started = monotonic()
        try:
            metadata = {"name": filename, "parents": [folder_id]}
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=True)
            file = self._service.files().create(
                body=metadata, media_body=media, fields="id"
            ).execute()
            file_id = file["id"]
            logger.info("Uploaded '%s' (bytes) to folder %s (ID: %s)", filename, folder_id, file_id)
            record_activity(
                event="drive_upload_completed",
                component="drive",
                summary="Uploaded bytes to Drive",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"filename": filename, "mime_type": mime_type},
            )
            record_audit(
                event="drive_file_uploaded",
                component="drive",
                summary="Uploaded file bytes to Drive",
                metadata={"filename": filename, "mime_type": mime_type},
            )
            return file_id
        except Exception as exc:
            record_issue(
                level="ERROR",
                event="drive_upload_failed",
                component="drive",
                status="error",
                summary="Drive byte upload failed",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"filename": filename, "mime_type": mime_type, "error": str(exc)},
            )
            raise

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def download_file(self, file_id: str) -> tuple[bytes, str, str]:
        """Download a file from Drive. Returns (data, filename, mime_type)."""
        started = monotonic()
        try:
            meta = self._service.files().get(
                fileId=file_id, fields="name,mimeType"
            ).execute()
            filename = meta.get("name", "unknown")
            mime_type = meta.get("mimeType", "application/octet-stream")

            # Google-native formats: export to a readable format
            google_export_map = {
                "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
                "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
                "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
            }
            if mime_type in google_export_map:
                export_mime, ext = google_export_map[mime_type]
                data = self._service.files().export(
                    fileId=file_id, mimeType=export_mime
                ).execute()
                record_activity(
                    event="drive_download_completed",
                    component="drive",
                    summary="Exported Google-native Drive file",
                    duration_ms=(monotonic() - started) * 1000,
                    metadata={"mime_type": export_mime},
                )
                return bytes(data), filename + ext, export_mime

            request = self._service.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            record_activity(
                event="drive_download_completed",
                component="drive",
                summary="Downloaded Drive file",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"mime_type": mime_type},
            )
            return buf.getvalue(), filename, mime_type
        except Exception as exc:
            record_issue(
                level="ERROR",
                event="drive_download_failed",
                component="drive",
                status="error",
                summary="Drive download failed",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"file_id": file_id, "error": str(exc)},
            )
            raise

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search Drive files by name. Returns list of {id, name, mimeType}."""
        started = monotonic()
        try:
            safe_query = query.replace("'", "\\'")
            drive_query = (
                f"(name contains '{safe_query}' or fullText contains '{safe_query}') "
                f"and trashed=false"
            )
            results = self._service.files().list(
                q=drive_query,
                pageSize=max_results,
                fields="files(id, name, mimeType, parents)",
            ).execute()
            files = results.get("files", [])
            record_activity(
                event="drive_search_completed",
                component="drive",
                summary="Drive search completed",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"result_count": len(files)},
            )
            return files
        except Exception as exc:
            record_issue(
                level="ERROR",
                event="drive_search_failed",
                component="drive",
                status="error",
                summary="Drive search failed",
                duration_ms=(monotonic() - started) * 1000,
                metadata={"error": str(exc)},
            )
            raise

    def get_file_web_link(self, file_id: str) -> str:
        try:
            file = self._service.files().get(fileId=file_id, fields="webViewLink").execute()
            record_activity(
                event="drive_link_resolved",
                component="drive",
                summary="Resolved Drive web link",
                metadata={"file_id": file_id},
            )
            return file.get("webViewLink", "")
        except Exception as exc:
            record_issue(
                level="ERROR",
                event="drive_link_resolution_failed",
                component="drive",
                status="error",
                summary="Failed to resolve Drive web link",
                metadata={"file_id": file_id, "error": str(exc)},
            )
            raise
