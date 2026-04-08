from __future__ import annotations

import os
import re
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


# Required
OPENROUTER_API_KEY: str = _require("OPENROUTER_API_KEY")
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_USER_ID: int = int(_require("TELEGRAM_ALLOWED_USER_ID"))
GOOGLE_CREDENTIALS_PATH: str = _require("GOOGLE_CREDENTIALS_PATH")
GOOGLE_TOKEN_PATH: str = _require("GOOGLE_TOKEN_PATH")


def _normalize_legacy_model_name(model: str) -> str:
    model = model.strip()
    if not model:
        return "anthropic/claude-sonnet-4.6"
    if "/" in model:
        return model
    normalized = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model)
    return f"anthropic/{normalized}"


def _optional_model(key: str) -> str | None:
    value = os.getenv(key, "").strip()
    if not value:
        return None
    return _normalize_legacy_model_name(value)


def _env_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Optional with defaults
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL") or _normalize_legacy_model_name(CLAUDE_MODEL)
OPENROUTER_MODEL_RELEVANCE: str | None = _optional_model("OPENROUTER_MODEL_RELEVANCE")
OPENROUTER_MODEL_FINANCIAL: str | None = _optional_model("OPENROUTER_MODEL_FINANCIAL")
OPENROUTER_MODEL_CLASSIFICATION: str | None = _optional_model("OPENROUTER_MODEL_CLASSIFICATION")
OPENROUTER_MODEL_VISION: str | None = _optional_model("OPENROUTER_MODEL_VISION")
MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4096"))
GMAIL_POLL_INTERVAL: int = int(os.getenv("GMAIL_POLL_INTERVAL", "300"))
GMAIL_START_DATE: str = os.getenv("GMAIL_START_DATE", "").strip()
TELEGRAM_EMAIL_SUMMARY_NOTIFICATIONS: bool = _env_bool("TELEGRAM_EMAIL_SUMMARY_NOTIFICATIONS", True)
JARVIS_TIMEZONE: str = os.getenv("JARVIS_TIMEZONE", "").strip()
JARVIS_DB_PATH: str = os.getenv("JARVIS_DB_PATH", "./data/jarvis_memory.db")
JARVIS_CHROMA_PATH: str = os.getenv("JARVIS_CHROMA_PATH", "./data/jarvis_chroma")
OBSIDIAN_VAULT_PATH: str = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
OBSIDIAN_ROOT_FOLDER: str = os.getenv("OBSIDIAN_ROOT_FOLDER", "Marvis").strip()

# Prevent the Anthropic SDK from picking up a legacy first-party API key when routing via OpenRouter.
os.environ.pop("ANTHROPIC_API_KEY", None)

# Google API scopes
# NOTE: If you add a new scope here, delete token.json and re-authenticate.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

# Conversation history cap
MAX_CONVERSATION_TURNS: int = 20
