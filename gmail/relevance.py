"""
Relevance filter for incoming emails (PRD §6.4).

Claude evaluates each email and decides whether it contains a document worth filing.
Transactional emails (newsletters, OTPs, notifications, marketing) are skipped.
Contracts, invoices, insurance documents, travel bookings, and official correspondence
are considered worth filing.
"""
import json
import logging
import re

import anthropic

from config import settings

logger = logging.getLogger(__name__)

_RELEVANCE_PROMPT = """\
You are an email triage assistant. Decide whether this email contains a document \
worth filing in a personal document library.

FILE if the email contains or is:
- Contracts, agreements, or legal documents
- Invoices, receipts, or payment confirmations
- Insurance documents or policies
- Travel bookings, tickets, or itineraries
- Official correspondence (government, tax, bank, employer)
- Health records, prescriptions, or medical documents
- Certificates, credentials, or licences

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
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            raise ValueError(f"No JSON in relevance response: {raw!r}")

        data = json.loads(json_match.group())
        should_file = bool(data.get("should_file", True))
        reason = data.get("reason", "")
        return should_file, reason

    except Exception as e:
        logger.warning("Relevance check failed (%s) — defaulting to file", e)
        return True, "relevance check failed, filing by default"
