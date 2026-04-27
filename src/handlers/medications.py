import logging

from telegram import Update
from telegram.ext import ContextTypes

from .. import medications as medications_service
from ..config import OWNER_CHAT_ID

log = logging.getLogger(__name__)


async def handle_medication_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return

    # Restringe ao dono
    user = query.from_user
    if user is None or user.id != OWNER_CHAT_ID:
        await query.answer("Você não tem permissão.", show_alert=True)
        return

    await query.answer()

    data = query.data
    if not data.startswith(("med_taken:", "med_skip:")):
        return

    action, _, raw_id = data.partition(":")
    try:
        med_id = int(raw_id)
    except ValueError:
        log.warning("callback_data inválido: %r", data)
        return

    med = medications_service.get_by_id(med_id)
    if med is None:
        await query.edit_message_text("⚠️ Medicamento não encontrado.")
        return

    name = med["name"]
    if action == "med_taken":
        try:
            medications_service.track(med_id, skipped=False)
        except Exception:
            log.exception("Falha ao registrar tomada")
            await query.edit_message_text("Falhou ao registrar.")
            return
        await query.edit_message_text(f"💊 {name}: tomado ✅")
    elif action == "med_skip":
        try:
            medications_service.track(med_id, skipped=True)
        except Exception:
            log.exception("Falha ao registrar skip")
            await query.edit_message_text("Falhou ao registrar.")
            return
        await query.edit_message_text(f"💊 {name}: pulado ⏭️")
