"""
Shared text and image extraction utilities.
Used by gmail/parser.py, telegram_bot/bot.py, agent_sdk/filer.py, and core/agent.py.
"""
import base64
import io
import logging

from core.llm_client import create_llm_client, get_model_name
from core.structured_output import response_text

logger = logging.getLogger(__name__)


def extract_text(data: bytes, mime_type: str, filename: str) -> str:
    """Extract text content from file bytes based on MIME type."""
    try:
        if mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
            return extract_pdf_text(data)
        elif (
            mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or filename.lower().endswith(".docx")
        ):
            return extract_docx_text(data)
        elif mime_type.startswith("text/"):
            return data.decode("utf-8", errors="replace")
        elif (
            mime_type in (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel",
            )
            or filename.lower().endswith((".xlsx", ".xls"))
        ):
            return extract_spreadsheet_text(data, filename)
        elif filename.lower().endswith(".csv"):
            return extract_spreadsheet_text(data, filename)
    except Exception as e:
        logger.warning("Text extraction failed for %s: %s", filename, e)
    return ""


def extract_pdf_text(data: bytes) -> str:
    import PyPDF2
    reader = PyPDF2.PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)
    return "\n".join(pages)


def extract_docx_text(data: bytes) -> str:
    import docx
    doc = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def extract_spreadsheet_text(data: bytes, filename: str) -> str:
    if filename.lower().endswith(".csv"):
        return data.decode("utf-8", errors="replace")
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        lines = []
        for sheet in wb.worksheets:
            lines.append(f"[Sheet: {sheet.title}]")
            for row in sheet.iter_rows(values_only=True):
                row_text = "\t".join(str(c) if c is not None else "" for c in row)
                if row_text.strip():
                    lines.append(row_text)
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Spreadsheet extraction failed for %s: %s", filename, e)
        return ""


def describe_image(image_data: bytes, mime_type: str) -> str:
    """
    Use the configured multimodal model to describe an image.
    Returns a text description including any visible text, amounts, dates, names.
    """
    try:
        b64 = base64.standard_b64encode(image_data).decode("utf-8")
        client = create_llm_client()
        response = client.messages.create(
            model=get_model_name("vision"),
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Describe this image. If it is a document, extract all visible text. "
                                "Note any key details such as amounts, dates, names, addresses, "
                                "or document type. Be thorough."
                            ),
                        },
                    ],
                }
            ],
        )
        return response_text(response)[:4000]
    except Exception as e:
        logger.warning("Image description failed: %s", e)
        return ""
