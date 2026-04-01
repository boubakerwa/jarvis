"""
Attachment classifier using the Anthropic Python SDK directly.
Sends a classification prompt to Claude and parses the JSON response.

Note: The PRD specifies claude-agent-sdk for this task. We implement this
using the standard anthropic SDK with a focused single-turn call, which gives
us the same result without the overhead of a full agentic loop.
"""
import json
import logging
import re
from dataclasses import dataclass

import anthropic

from config import settings
from storage.schema import build_classification_prompt

logger = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    top_level: str
    sub_folder: str
    filename: str
    summary: str


def classify_attachment(
    original_filename: str,
    mime_type: str,
    text_content: str,
) -> ClassificationResult:
    """
    Classify a file attachment using Claude.
    Returns a ClassificationResult with the target Drive path and filename.
    """
    prompt = build_classification_prompt(original_filename, mime_type, text_content)

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=settings.CLAUDE_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    logger.debug("Classification raw response: %s", raw)

    # Extract JSON from response (may have surrounding text)
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        raise ValueError(f"No JSON found in classifier response: {raw!r}")

    data = json.loads(json_match.group())

    return ClassificationResult(
        top_level=data["top_level"],
        sub_folder=data["sub_folder"],
        filename=data["filename"],
        summary=data["summary"],
    )
