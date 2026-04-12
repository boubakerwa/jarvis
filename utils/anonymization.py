from __future__ import annotations

import json
import hashlib
import logging
import re
from dataclasses import dataclass
from time import monotonic
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from config import settings
from core.opslog import record_activity, record_issue

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+\d[\d\s().-]{6,}\d|\(?\d{3,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,})(?!\w)")
_LONG_DIGITS_RE = re.compile(r"\b\d{7,}\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_SECRET_TOKEN_RE = re.compile(r"\b(?:sk|ghp|gho|ghu|pat)_[A-Za-z0-9_]{8,}\b")
_CODE_FENCE_RE = re.compile(r"^```(?:text|markdown)?\s*|\s*```$", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\[(?P<kind>[A-Z_]+)_(?P<index>\d+)\]")


class AnonymizationUnavailableError(RuntimeError):
    pass


@dataclass
class AnonymizationResult:
    sanitized_text: str
    changed: bool
    replacement_counts: dict[str, int]
    backend: str
    model: str
    content_sha256: str
    truncated: bool = False


def content_sha256(data: bytes | str) -> str:
    payload = data if isinstance(data, bytes) else str(data or "").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def prepare_text_for_remote_processing(
    text: str,
    *,
    filename: str,
    mime_type: str,
    raw_data: bytes | None = None,
) -> tuple[str, Optional[AnonymizationResult], Optional[str]]:
    source_text = str(text or "")
    if not settings.JARVIS_ANONYMIZATION_ENABLED:
        return source_text, None, None
    if not source_text.strip():
        return "", None, "text extraction did not produce anonymization-safe text"
    try:
        result = anonymize_text(
            source_text,
            filename=filename,
            mime_type=mime_type,
            raw_data=raw_data,
        )
        return result.sanitized_text, result, None
    except AnonymizationUnavailableError as exc:
        return "", None, str(exc)


def anonymize_text(
    text: str,
    *,
    filename: str = "",
    mime_type: str = "text/plain",
    raw_data: bytes | None = None,
) -> AnonymizationResult:
    source_text = str(text or "")
    digest = content_sha256(raw_data if raw_data is not None else source_text)
    if not settings.JARVIS_ANONYMIZATION_ENABLED or not source_text.strip():
        return AnonymizationResult(
            sanitized_text=source_text,
            changed=False,
            replacement_counts={},
            backend="disabled",
            model="",
            content_sha256=digest,
            truncated=False,
        )

    truncated = len(source_text) > settings.JARVIS_ANONYMIZATION_MAX_CHARS
    working_text = source_text[: settings.JARVIS_ANONYMIZATION_MAX_CHARS]
    deterministic_text, deterministic_counts = _apply_deterministic_masks(working_text)

    model_name = settings.OLLAMA_MODEL_ANONYMIZER
    if not model_name:
        if settings.JARVIS_ANONYMIZATION_FAIL_CLOSED:
            record_issue(
                level="WARNING",
                event="document_anonymization_unconfigured",
                component="privacy",
                status="warning",
                summary="Local anonymization is enabled but no Ollama anonymizer model is configured",
            )
            raise AnonymizationUnavailableError(
                "local anonymization is enabled but OLLAMA_MODEL_ANONYMIZER is not configured"
            )
        return AnonymizationResult(
            sanitized_text=deterministic_text,
            changed=deterministic_text != source_text[: settings.JARVIS_ANONYMIZATION_MAX_CHARS],
            replacement_counts=deterministic_counts,
            backend="deterministic",
            model="",
            content_sha256=digest,
            truncated=truncated,
        )

    started = monotonic()
    try:
        refined_text = _ollama_refine_anonymization(
            deterministic_text,
            filename=filename,
            mime_type=mime_type,
            model_name=model_name,
        )
    except AnonymizationUnavailableError:
        raise
    except Exception as exc:
        logger.warning("Local anonymization failed for %s: %s", filename, exc)
        if settings.JARVIS_ANONYMIZATION_FAIL_CLOSED:
            record_issue(
                level="WARNING",
                event="document_anonymization_failed",
                component="privacy",
                status="warning",
                summary="Local anonymization failed before remote processing",
                metadata={"filename": filename[:120], "mime_type": mime_type, "error": str(exc)},
            )
            raise AnonymizationUnavailableError(str(exc)) from exc
        refined_text = deterministic_text

    replacement_counts = _count_placeholders(refined_text) or deterministic_counts
    changed = refined_text != working_text
    record_activity(
        event="document_anonymized",
        component="privacy",
        summary="Prepared anonymized document text for remote processing",
        duration_ms=(monotonic() - started) * 1000,
        metadata={
            "backend": "ollama",
            "model": model_name,
            "filename": filename[:120],
            "mime_type": mime_type,
            "truncated": truncated,
        },
    )
    return AnonymizationResult(
        sanitized_text=refined_text,
        changed=changed,
        replacement_counts=replacement_counts,
        backend="ollama",
        model=model_name,
        content_sha256=digest,
        truncated=truncated,
    )


def _apply_deterministic_masks(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    masked = text
    for pattern, kind in (
        (_EMAIL_RE, "EMAIL"),
        (_IBAN_RE, "ACCOUNT"),
        (_SECRET_TOKEN_RE, "SECRET"),
        (_PHONE_RE, "PHONE"),
        (_LONG_DIGITS_RE, "ID"),
    ):
        masked = _mask_pattern(masked, pattern, kind, counts)
    return masked, counts


def _mask_pattern(text: str, pattern: re.Pattern[str], kind: str, counts: dict[str, int]) -> str:
    def _replace(match: re.Match[str]) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return f"[{kind}_{counts[kind]}]"

    return pattern.sub(_replace, text)


def _ollama_refine_anonymization(
    text: str,
    *,
    filename: str,
    mime_type: str,
    model_name: str,
) -> str:
    prompt = (
        "You anonymize document text before it is sent to a remote LLM.\n"
        "Return only the anonymized document text, with no explanation.\n"
        "Preserve language, line breaks, amounts, dates, and document structure when they are not sensitive.\n"
        "Preserve any existing placeholders like [EMAIL_1] or [ID_1].\n"
        "Replace remaining person names with [PERSON_n], street addresses with [ADDRESS_n], "
        "and obvious confidential secrets with [SECRET_n].\n"
        "Do not invent facts. Do not summarize.\n\n"
        f"Filename: {filename}\n"
        f"MIME type: {mime_type}\n\n"
        "Document text:\n"
        f"{text}"
    )
    payload = json.dumps(
        {
            "model": model_name,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        }
    ).encode("utf-8")
    endpoint = settings.OLLAMA_BASE_URL.rstrip("/") + "/api/generate"
    req = urllib_request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=settings.OLLAMA_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except urllib_error.URLError as exc:
        record_issue(
            level="WARNING",
            event="document_anonymization_unavailable",
            component="privacy",
            status="warning",
            summary="Local anonymization backend unavailable",
            metadata={"backend": "ollama", "error": str(exc)},
        )
        raise AnonymizationUnavailableError(f"Ollama request failed: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AnonymizationUnavailableError("Ollama returned non-JSON output") from exc

    refined = _strip_code_fences(str(data.get("response", "")).strip())
    if not refined:
        raise AnonymizationUnavailableError("Ollama returned an empty anonymized response")
    return refined


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return _CODE_FENCE_RE.sub("", stripped).strip()
    return stripped


def _count_placeholders(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in _PLACEHOLDER_RE.finditer(text or ""):
        kind = match.group("kind")
        counts[kind] = counts.get(kind, 0) + 1
    return counts
