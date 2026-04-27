import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Variável de ambiente {name!r} não está definida. "
            f"Verifique o arquivo .env em {PROJECT_ROOT}."
        )
    return value


TELEGRAM_TOKEN = _required("TELEGRAM_TOKEN")
GEMINI_API_KEY = _required("GEMINI_API_KEY")
OWNER_CHAT_ID = int(_required("OWNER_CHAT_ID"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"

DB_PATH = PROJECT_ROOT / "bot.db"
