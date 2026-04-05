import logging
import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload
import io

from config import settings
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
        root_id = self._get_or_create_folder(JARVIS_ROOT, parent_id=None)
        logger.info("Jarvis root folder ID: %s", root_id)

        for top_level, sub_folders in DRIVE_STRUCTURE.items():
            top_id = self._get_or_create_folder(top_level, parent_id=root_id)
            for sub in sub_folders:
                self._get_or_create_folder(sub, parent_id=top_id)

        return root_id

    def get_or_create_folder_path(self, top_level: str, sub_folder: str) -> str:
        """Return folder ID for Jarvis/{top_level}/{sub_folder}, creating if needed."""
        root_id = self._get_folder_id(JARVIS_ROOT, parent_id=None)
        if not root_id:
            root_id = self.init_drive_structure()

        top_id = self._get_or_create_folder(top_level, parent_id=root_id)
        sub_id = self._get_or_create_folder(sub_folder, parent_id=top_id)
        return sub_id

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
        metadata = {"name": filename, "parents": [folder_id]}
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file = self._service.files().create(
            body=metadata, media_body=media, fields="id"
        ).execute()
        file_id = file["id"]
        logger.info("Uploaded '%s' to folder %s (ID: %s)", filename, folder_id, file_id)
        return file_id

    def upload_bytes(
        self,
        data: bytes,
        filename: str,
        folder_id: str,
        mime_type: str = "application/octet-stream",
    ) -> str:
        """Upload bytes directly to Drive. Returns the file ID."""
        metadata = {"name": filename, "parents": [folder_id]}
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=True)
        file = self._service.files().create(
            body=metadata, media_body=media, fields="id"
        ).execute()
        file_id = file["id"]
        logger.info("Uploaded '%s' (bytes) to folder %s (ID: %s)", filename, folder_id, file_id)
        return file_id

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def download_file(self, file_id: str) -> tuple[bytes, str, str]:
        """Download a file from Drive. Returns (data, filename, mime_type)."""
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
            return bytes(data), filename + ext, export_mime

        # Regular files
        request = self._service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue(), filename, mime_type

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search Drive files by name. Returns list of {id, name, mimeType}."""
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
        return results.get("files", [])

    def get_file_web_link(self, file_id: str) -> str:
        file = self._service.files().get(fileId=file_id, fields="webViewLink").execute()
        return file.get("webViewLink", "")
