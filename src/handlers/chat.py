import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from ..extractor import schedule_extraction
from ..gemini_client import chat as gemini_chat
from .auth import owner_only

log = logging.getLogger(__name__)


@owner_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    user_text = message.text.strip()
    if not user_text:
        return

    await context.bot.send_chat_action(
        chat_id=message.chat_id, action=ChatAction.TYPING
    )

    try:
        reply = await gemini_chat(user_text)
    except Exception as e:
        log.exception("Erro ao processar mensagem")
        reply = f"Tive um erro ao gerar resposta: {e}"

    await message.reply_text(reply)
    schedule_extraction(user_text)
