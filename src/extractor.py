import asyncio
import logging

from .gemini_client import extract_facts
from .memory import add_memory

log = logging.getLogger(__name__)


async def extract_and_save(user_message: str) -> int:
    facts = await extract_facts(user_message)
    saved = 0
    for f in facts:
        try:
            add_memory(f["category"], f["content"], source="auto")
            saved += 1
        except Exception:
            log.exception("Falha ao salvar fato extraído: %r", f)
    if saved:
        log.info("Extração automática salvou %d fato(s).", saved)
    return saved


def schedule_extraction(user_message: str) -> asyncio.Task:
    """Dispara a extração em background sem bloquear a resposta principal."""
    return asyncio.create_task(extract_and_save(user_message))
