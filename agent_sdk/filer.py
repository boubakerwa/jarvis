"""
Attachment classifier using the Anthropic-compatible Messages API via OpenRouter.
Sends a classification prompt to the configured model and parses the JSON response.

Note: The local package name stays `agent_sdk` for compatibility, but this code
uses the standard Anthropic SDK routed through OpenRouter rather than the
claude-agent-sdk package.
"""
import os
import re
from dataclasses import dataclass

from config import settings
from core.structured_output import generate_validated_json
from storage.schema import TOP_LEVEL_FOLDERS, build_classification_prompt


@dataclass
class ClassificationResult:
    top_level: str
    sub_folder: str
    filename: str
    summary: str


def _sanitize_sub_folder(value: str) -> str:
    text = str(value or "").strip().replace("/", " - ").replace("\\", " - ")
    text = re.sub(r"\s+", " ", text).strip(" -")
    if not text:
        raise ValueError("sub_folder is missing")
    return text[:120]


def _sanitize_filename(value: str, original_filename: str) -> str:
    proposed = str(value or "").strip()
    proposed_ext = os.path.splitext(proposed)[1].lower()
    original_ext = os.path.splitext(original_filename)[1].lower()
    ext = proposed_ext or original_ext

    stem = os.path.splitext(proposed)[0].strip().lower()
    if not stem:
        fallback_stem = os.path.splitext(original_filename)[0].strip().lower() or "document"
        stem = fallback_stem

    stem = stem.replace(" ", "_")
    stem = re.sub(r"[^a-z0-9_-]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")
    if not stem:
        stem = "document"

    return f"{stem}{ext}"


def _validate_classification_payload(data: dict, original_filename: str) -> ClassificationResult:
    top_level = str(data.get("top_level", "")).strip()
    if top_level not in TOP_LEVEL_FOLDERS:
        raise ValueError(f"Invalid top_level: {top_level!r}")

    summary = str(data.get("summary", "")).strip()
    if not summary:
        raise ValueError("summary is missing")

    return ClassificationResult(
        top_level=top_level,
        sub_folder=_sanitize_sub_folder(data.get("sub_folder", "")),
        filename=_sanitize_filename(data.get("filename", ""), original_filename),
        summary=summary[:280],
    )


def build_review_classification(
    original_filename: str,
    *,
    summary: str = "Document stored for manual review because anonymization-safe processing was unavailable.",
) -> ClassificationResult:
    return ClassificationResult(
        top_level="Misc",
        sub_folder="Needs Review",
        filename=_sanitize_filename("needs_review_document", original_filename),
        summary=summary[:280],
    )


def classify_attachment(
    original_filename: str,
    mime_type: str,
    text_content: str,
    raw_data: bytes = b"",
) -> ClassificationResult:
    """
    Classify a file attachment using the configured LLM.
    For images, uses vision to generate a description if text_content is empty.
    Returns a ClassificationResult with the target Drive path and filename.
    """
    if mime_type.startswith("image/") and not text_content and raw_data:
        if settings.JARVIS_ANONYMIZATION_ENABLED:
            raise ValueError("image-only documents require a local-safe review fallback while anonymization is enabled")
        from utils.text_extraction import describe_image
        text_content = describe_image(raw_data, mime_type)

    prompt = build_classification_prompt(original_filename, mime_type, text_content)

    return generate_validated_json(
        task="classification",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
        validator=lambda data: _validate_classification_payload(data, original_filename),
        allow_fallback=True,
    )
