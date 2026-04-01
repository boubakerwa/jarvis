"""
Email and attachment parsing from Gmail API message payloads.
"""
import base64
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Attachment:
    filename: str
    mime_type: str
    data: bytes
    text_content: str = ""  # Extracted text if possible


@dataclass
class ParsedEmail:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    body: str
    attachments: list[Attachment] = field(default_factory=list)


def parse_message(gmail_service, message_id: str) -> ParsedEmail:
    """Fetch and parse a Gmail message by ID."""
    msg = gmail_service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()

    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}

    sender = headers.get("From", "")
    subject = headers.get("Subject", "(no subject)")
    date = headers.get("Date", "")
    thread_id = msg.get("threadId", "")

    body = _extract_body(msg["payload"])
    attachments = _extract_attachments(gmail_service, message_id, msg["payload"])

    return ParsedEmail(
        message_id=message_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        date=date,
        body=body,
        attachments=attachments,
    )


# ------------------------------------------------------------------
# Body extraction
# ------------------------------------------------------------------

def _extract_body(payload: dict) -> str:
    """Prefer plain text; fall back to HTML stripped of tags."""
    text = _find_part(payload, "text/plain")
    if text:
        return text

    html = _find_part(payload, "text/html")
    if html:
        return _strip_html(html)

    return ""


def _find_part(payload: dict, mime_type: str) -> str:
    """Recursively search payload parts for a given MIME type."""
    if payload.get("mimeType") == mime_type:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        result = _find_part(part, mime_type)
        if result:
            return result

    return ""


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ------------------------------------------------------------------
# Attachment extraction
# ------------------------------------------------------------------

def _extract_attachments(gmail_service, message_id: str, payload: dict) -> list[Attachment]:
    attachments = []
    _collect_attachments(gmail_service, message_id, payload, attachments)
    return attachments


def _collect_attachments(
    gmail_service, message_id: str, payload: dict, out: list[Attachment]
) -> None:
    filename = payload.get("filename", "")
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})

    if filename and mime_type not in ("text/plain", "text/html"):
        data = _get_attachment_data(gmail_service, message_id, body)
        if data:
            text_content = _extract_text(data, mime_type, filename)
            out.append(Attachment(
                filename=filename,
                mime_type=mime_type,
                data=data,
                text_content=text_content,
            ))

    for part in payload.get("parts", []):
        _collect_attachments(gmail_service, message_id, part, out)


def _get_attachment_data(gmail_service, message_id: str, body: dict) -> Optional[bytes]:
    if "data" in body:
        return base64.urlsafe_b64decode(body["data"] + "==")

    attachment_id = body.get("attachmentId")
    if attachment_id:
        result = gmail_service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ).execute()
        return base64.urlsafe_b64decode(result["data"] + "==")

    return None


def _extract_text(data: bytes, mime_type: str, filename: str) -> str:
    """Extract text content from attachment bytes based on MIME type."""
    try:
        if mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
            return _extract_pdf_text(data)
        elif mime_type in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ) or filename.lower().endswith(".docx"):
            return _extract_docx_text(data)
        elif mime_type.startswith("text/"):
            return data.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("Text extraction failed for %s: %s", filename, e)
    return ""


def _extract_pdf_text(data: bytes) -> str:
    import PyPDF2
    import io
    reader = PyPDF2.PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)
    return "\n".join(pages)


def _extract_docx_text(data: bytes) -> str:
    import docx
    import io
    doc = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
