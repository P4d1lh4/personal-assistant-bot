import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from .. import activities as activities_service
from ..config import OWNER_CHAT_ID

log = logging.getLogger(__name__)


def _parse_callback_date(raw: str) -> str:
    """Aceita YYYYMMDD compacto e devolve ISO YYYY-MM-DD."""
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    # Fallback: hoje
    return datetime.now().date().isoformat()


async def handle_activity_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return

    user = query.from_user
    if user is None or user.id != OWNER_CHAT_ID:
        await query.answer("Sem permissão.", show_alert=True)
        return

    await query.answer()

    data = query.data
    if not data.startswith(("act_done:", "act_skip:")):
        return

    parts = data.split(":")
    if len(parts) < 2:
        log.warning("callback_data inválido: %r", data)
        return

    action = parts[0]
    try:
        activity_id = int(parts[1])
    except ValueError:
        log.warning("callback_data com id inválido: %r", data)
        return

    log_date = _parse_callback_date(parts[2]) if len(parts) >= 3 else None

    activity = activities_service.get_by_id(activity_id)
    if activity is None:
        await query.edit_message_text("⚠️ Atividade não encontrada.")
        return

    name = activity["name"]
    status = "done" if action == "act_done" else "skipped"

    try:
        activities_service.track(activity_id, status, log_date=log_date)
    except Exception:
        log.exception("Falha ao registrar atividade via callback")
        await query.edit_message_text("Falhou ao registrar.")
        return

    icon = "✅" if status == "done" else "❌"
    label = "feito" if status == "done" else "não feito"
    suffix = f" (data: {log_date})" if log_date else ""
    try:
        await query.edit_message_text(f"{icon} {name}: {label}{suffix}")
    except Exception:
        log.exception("Falha ao editar mensagem de atividade")
