import logging

from telegram import Update
from telegram.ext import ContextTypes

from .. import reminders as reminders_service
from ..config import OWNER_CHAT_ID

log = logging.getLogger(__name__)


async def handle_reminder_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Trata cliques nos botões `rem_cancel:<id>`."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    user = query.from_user
    if user is None or user.id != OWNER_CHAT_ID:
        await query.answer("Sem permissão.", show_alert=True)
        return

    await query.answer()

    if not query.data.startswith("rem_cancel:"):
        return

    try:
        rid = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        log.warning("callback_data inválido: %r", query.data)
        return

    try:
        ok = reminders_service.cancel(rid)
    except Exception:
        log.exception("Falha ao cancelar lembrete via callback")
        await query.edit_message_text("Falhou ao cancelar.")
        return

    original = query.message.text or ""
    if ok:
        new_text = f"{original}\n\n❌ Lembrete #{rid} cancelado."
    else:
        new_text = f"{original}\n\n⚠️ Lembrete #{rid} já não estava ativo."
    # Remove o teclado mas preserva o texto original + nota de cancelamento.
    try:
        await query.edit_message_text(new_text, reply_markup=None)
    except Exception:
        log.exception("Falha ao editar mensagem após cancelamento")
