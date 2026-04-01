import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


# Required
ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_USER_ID: int = int(_require("TELEGRAM_ALLOWED_USER_ID"))
GOOGLE_CREDENTIALS_PATH: str = _require("GOOGLE_CREDENTIALS_PATH")
GOOGLE_TOKEN_PATH: str = _require("GOOGLE_TOKEN_PATH")

# Optional with defaults
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4096"))
GMAIL_POLL_INTERVAL: int = int(os.getenv("GMAIL_POLL_INTERVAL", "300"))
JARVIS_DB_PATH: str = os.getenv("JARVIS_DB_PATH", "./data/jarvis_memory.db")
JARVIS_CHROMA_PATH: str = os.getenv("JARVIS_CHROMA_PATH", "./data/jarvis_chroma")

# Google API scopes
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
]

# Conversation history cap
MAX_CONVERSATION_TURNS: int = 20
