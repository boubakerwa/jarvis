"""
Relevance filter for incoming emails (PRD §6.4).

The configured LLM evaluates each email and decides whether it contains a document worth filing.
Transactional emails (newsletters, OTPs, notifications, marketing) are skipped.
Contracts, invoices, insurance documents, travel bookings, and official correspondence
are considered worth filing.
"""
import logging

from core.structured_output import StructuredOutputError, generate_validated_json

logger = logging.getLogger(__name__)

_RELEVANCE_PROMPT = """\
You are an email triage assistant. Decide whether this email contains a document \
worth filing in a personal document library.

The email may be in English or German. Evaluate it correctly regardless of language.

FILE if the email contains or is:
- Contracts, agreements, or legal documents (Vertrag, Vereinbarung)
- Invoices, receipts, or payment confirmations (Rechnung, Quittung, Zahlungsbestätigung)
- Insurance documents or policies (Versicherung, Police, Krankenversicherung)
- Travel bookings, tickets, or itineraries
- Official correspondence (government, tax, bank, employer) (Steuerbescheid, Kontoauszug, Bescheid)
- Health records, prescriptions, or medical documents
- Certificates, credentials, or licences (Bescheinigung, Zertifikat)
- Dunning notices or formal reminders (Mahnung)

SKIP if the email is:
- A newsletter or marketing email
- An OTP, verification code, or security alert
- A social notification (likes, follows, comments)
- An automated system notification or status update
- A promotional or transactional email with no document value

Respond ONLY with a JSON object:
{{
  "should_file": true or false,
  "reason": "one sentence explanation"
}}

Email details:
- From: {sender}
- Subject: {subject}
- Body preview (first 500 chars): {body_preview}
- Attachments: {attachment_names}
"""


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "file", "1"}:
            return True
        if lowered in {"false", "no", "skip", "0"}:
            return False
    raise ValueError(f"Invalid should_file value: {value!r}")


def _validate_relevance_payload(data: dict) -> tuple[bool, str]:
    should_file = _coerce_bool(data.get("should_file", True))
    reason = str(data.get("reason", "")).strip() or "model classified the email"
    return should_file, reason[:240]


def is_worth_filing(email) -> tuple[bool, str]:
    """
    Returns (should_file, reason).
    On any error, defaults to True (file it rather than lose it).
    """
    attachment_names = ", ".join(a.filename for a in email.attachments) or "none"
    prompt = _RELEVANCE_PROMPT.format(
        sender=email.sender,
        subject=email.subject,
        body_preview=email.body[:500],
        attachment_names=attachment_names,
    )

    try:
        should_file, reason = generate_validated_json(
            task="relevance",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
            validator=_validate_relevance_payload,
            allow_fallback=False,
        )
        return should_file, reason

    except (StructuredOutputError, ValueError) as e:
        logger.warning("Relevance check failed (%s) — defaulting to file", e)
        return True, "relevance check failed, filing by default"
