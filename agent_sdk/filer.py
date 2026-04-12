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

_LOCAL_CLASSIFICATION_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Finances", "Tax", ("tax", "steuer", "finanzamt", "steuerbescheid", "vat", "irs")),
    ("Finances", "Investments", ("investment", "broker", "portfolio", "dividend", "depot", "wertpapier", "ertraege")),
    ("Finances", "Banking", ("invoice", "rechnung", "receipt", "quittung", "kontoauszug", "bank", "iban", "payment")),
    ("Insurance", "Health", ("insurance", "versicherung", "krankenversicherung", "policy", "premium", "claim", "tk", "aok")),
    ("Insurance", "Liability", ("liability", "haftpflicht")),
    ("Insurance", "Vehicle", ("car insurance", "kfz", "vehicle insurance", "fahrzeugversicherung")),
    ("Legal & Contracts", "Employment", ("contract", "employment", "arbeitsvertrag", "agreement", "signature", "signed")),
    ("Legal & Contracts", "Rental", ("rent", "rental", "lease", "mietvertrag", "landlord")),
    ("Travel", "Bookings", ("booking", "flight", "hotel", "airbnb", "boarding pass", "reservation")),
    ("Travel", "Visas & Docs", ("visa", "passport", "residence permit", "aufenthaltstitel")),
    ("Health", "Records", ("doctor", "clinic", "medical", "lab", "diagnosis", "record", "arzt")),
    ("Health", "Prescriptions", ("prescription", "medication", "rezept", "pharmacy")),
    ("Subscriptions", "General", ("subscription", "monthly plan", "renewal", "abonnement", "membership")),
    ("Real Estate", "General", ("property", "apartment", "house purchase", "mortgage", "notary")),
    ("Vehicles", "General", ("vehicle", "car", "registration", "driving licence", "license plate")),
    ("Projects & Side Hustles", "Sufra", ("sufra",)),
    ("Projects & Side Hustles", "Other", ("project", "proposal", "client", "freelance", "side hustle")),
    ("PR", "LinkedIn Composer", ("linkedin", "social post", "post draft")),
    ("Personal Development", "Courses & Certificates", ("course", "certificate", "training", "workshop")),
    ("Personal Development", "Books & Resources", ("book", "ebook", "guide", "resource")),
    ("Household", "Utilities", ("utility", "electricity", "internet", "water bill", "gas bill")),
    ("Household", "Repairs & Services", ("repair", "service visit", "plumber", "electrician")),
    ("Household", "Appliances & Warranties", ("warranty", "appliance", "manual", "guarantee")),
)


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


def classify_attachment_locally(
    original_filename: str,
    mime_type: str,
    text_content: str,
    *,
    summary_reason: str = "",
) -> ClassificationResult:
    haystack = f"{original_filename}\n{text_content}".lower()
    best_top_level = "Misc"
    best_sub_folder = "Needs Review"
    best_score = 0

    for top_level, sub_folder, keywords in _LOCAL_CLASSIFICATION_RULES:
        score = sum(1 for keyword in keywords if keyword in haystack)
        if score > best_score:
            best_score = score
            best_top_level = top_level
            best_sub_folder = sub_folder

    if best_score == 0 and mime_type.startswith("image/"):
        best_sub_folder = "Needs Review"

    summary_prefix = "Locally classified without remote document processing"
    if summary_reason:
        summary = f"{summary_prefix} because {summary_reason}."
    elif best_score > 0:
        summary = f"{summary_prefix} using filename and extracted-text keywords."
    else:
        summary = f"{summary_prefix}; insufficient signals for a stronger category match."

    return ClassificationResult(
        top_level=best_top_level if best_top_level in TOP_LEVEL_FOLDERS else "Misc",
        sub_folder=_sanitize_sub_folder(best_sub_folder),
        filename=_sanitize_filename(original_filename, original_filename),
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
