import logging
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from ..config import OWNER_CHAT_ID

log = logging.getLogger(__name__)


def owner_only(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat is None or chat.id != OWNER_CHAT_ID:
            log.warning(
                "Mensagem ignorada de chat_id=%s (esperado %s)",
                getattr(chat, "id", None),
                OWNER_CHAT_ID,
            )
            return
        return await handler(update, context)

    return wrapper
