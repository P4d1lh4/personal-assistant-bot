"""Configuração comum dos tests.

Define env vars necessárias antes de importar `src.config` (que valida obrigatórios)
e dá um DB SQLite em arquivo temporário recriado a cada teste.
"""

import os
import pathlib
import tempfile

# Definir antes de qualquer import de src.* — config.py valida obrigatórios.
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("OWNER_CHAT_ID", "1")

_TEMP_DIR = pathlib.Path(tempfile.mkdtemp(prefix="jarvis-test-"))
TEMP_DB = _TEMP_DIR / "test.db"
os.environ["DB_PATH"] = str(TEMP_DB)

import pytest

from src.db import init_db


@pytest.fixture(autouse=True)
def reset_db():
    """Recria DB do zero a cada teste."""
    if TEMP_DB.exists():
        TEMP_DB.unlink()
    init_db()
    yield
