"""
Financial data extraction from document text.
Used when filing documents classified under Finances/.
"""
from datetime import datetime
import logging
from typing import Optional

from core.structured_output import StructuredOutputError, generate_validated_json

logger = logging.getLogger(__name__)

_ALLOWED_CATEGORIES = {"invoice", "receipt", "subscription", "insurance", "tax", "bank", "other"}
_CATEGORY_ALIASES = {
    "bill": "invoice",
    "billing": "invoice",
    "invoice": "invoice",
    "receipt": "receipt",
    "subscription": "subscription",
    "subscriptions": "subscription",
    "internet": "subscription",
    "telecom": "subscription",
    "telephone": "subscription",
    "phone": "subscription",
    "mobile": "subscription",
    "broadband": "subscription",
    "insurance": "insurance",
    "tax": "tax",
    "bank": "bank",
    "banking": "bank",
    "other": "other",
}

_FINANCIAL_PROMPT = """\
You are a financial document parser. Extract key financial data from the document text below.

Respond ONLY with a JSON object:
{{
  "vendor": "company or person name",
  "amount": 123.45,
  "currency": "EUR",
  "date": "YYYY-MM-DD",
  "category": "one of: invoice, receipt, subscription, insurance, tax, bank, other"
}}

If this is not a financial document or the data cannot be extracted, respond with:
{{
  "is_financial": false
}}

The document may be in English or German. Extract amounts regardless of currency symbol (€, EUR, $, etc.).

Document text:
{text}
"""


def _normalize_amount(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        raise ValueError("amount is empty")

    text = text.replace("EUR", "").replace("€", "").replace("$", "").strip()
    text = text.replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")

    return float(text)


def _normalize_date(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text if len(text) == 10 and text[4] == "-" and text[7] == "-" else ""


def _normalize_category(value) -> str:
    raw = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
    if not raw:
        return "other"
    if raw in _ALLOWED_CATEGORIES:
        return raw
    if raw in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[raw]
    for token in raw.split():
        if token in _CATEGORY_ALIASES:
            return _CATEGORY_ALIASES[token]
    return "other"


def _validate_financial_payload(data: dict) -> Optional[dict]:
    if data.get("is_financial") is False:
        return None

    vendor = str(data.get("vendor", "")).strip()
    if not vendor:
        raise ValueError("vendor is missing")

    amount = _normalize_amount(data.get("amount"))
    return {
        "vendor": vendor[:200],
        "amount": amount,
        "currency": str(data.get("currency", "EUR")).strip().upper() or "EUR",
        "date": _normalize_date(data.get("date", "")),
        "category": _normalize_category(data.get("category", "other")),
    }


def extract_financial_data(text_content: str, filename: str) -> Optional[dict]:
    """
    Extract vendor, amount, currency, date, and category from document text.
    Returns a dict with the extracted fields, or None if not a financial document.
    """
    if not text_content.strip():
        return None

    prompt = _FINANCIAL_PROMPT.format(text=text_content[:3000])

    try:
        return generate_validated_json(
            task="financial",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
            validator=_validate_financial_payload,
            allow_fallback=False,
        )
    except (StructuredOutputError, ValueError) as e:
        logger.warning("Financial extraction failed for %s: %s", filename, e)
        return None
