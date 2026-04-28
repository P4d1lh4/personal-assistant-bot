import logging
import re
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from ..reminders import cancel, create_one_shot, list_active
from .auth import owner_only

log = logging.getLogger(__name__)

DATE_ONLY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})$")
TIME_ONLY_RE = re.compile(r"^(\d{2}:\d{2})$")


def _parse_when(tokens: list[str]) -> tuple[datetime, int]:
    """Retorna (run_at, tokens_consumed) ou levanta ValueError."""
    if not tokens:
        raise ValueError("data/hora ausente")

    now = datetime.now()
    first = tokens[0].lower()

    if first == "hoje" and len(tokens) >= 2 and TIME_ONLY_RE.match(tokens[1]):
        h, m = map(int, tokens[1].split(":"))
        run_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if run_at <= now:
            raise ValueError("horário de hoje já passou")
        return run_at, 2

    if first in {"amanhã", "amanha"} and len(tokens) >= 2 and TIME_ONLY_RE.match(tokens[1]):
        h, m = map(int, tokens[1].split(":"))
        target = (now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
        return target, 2

    if len(tokens) >= 2 and DATE_ONLY_RE.match(tokens[0]) and TIME_ONLY_RE.match(tokens[1]):
        run_at = datetime.fromisoformat(f"{tokens[0]}T{tokens[1]}:00")
        if run_at <= now:
            raise ValueError("data/hora já passou")
        return run_at, 2

    try:
        run_at = datetime.fromisoformat(tokens[0])
        if run_at <= now:
            raise ValueError("data/hora já passou")
        return run_at, 1
    except ValueError:
        pass

    raise ValueError(
        "formato inválido. Use 'YYYY-MM-DD HH:MM', 'amanhã HH:MM' ou 'hoje HH:MM'"
    )


@owner_only
async def cmd_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    args = list(context.args or [])
    if not args:
        await msg.reply_text(
            "Uso: /lembrete <quando> <texto>\n"
            "Ex: /lembrete amanhã 09:00 Tomar remédio\n"
            "Ex: /lembrete 2026-04-28 14:30 Reunião com cliente"
        )
        return

    try:
        run_at, consumed = _parse_when(args)
    except ValueError as e:
        await msg.reply_text(f"Erro: {e}")
        return

    text = " ".join(args[consumed:]).strip()
    if not text:
        await msg.reply_text("Texto do lembrete está vazio.")
        return

    try:
        reminder_id, _ = create_one_shot(text, run_at)
    except Exception:
        log.exception("Falha ao agendar lembrete via comando")
        await msg.reply_text("Falhou ao agendar o lembrete.")
        return

    await msg.reply_text(
        f"⏰ Lembrete #{reminder_id} agendado para {run_at.strftime('%d/%m/%Y %H:%M')}: {text}"
    )


@owner_only
async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    rows = list_active()

    if not rows:
        await msg.reply_text("Nenhum lembrete ativo.")
        return

    lines = []
    for r in rows:
        kind = "(recorrente)" if r["is_recurring"] else ""
        lines.append(f"#{r['id']} [{r['cron_or_at']}] {r['text']} {kind}".strip())
    await msg.reply_text("\n".join(lines))


@owner_only
async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    args = context.args or []
    if not args:
        await msg.reply_text("Uso: /cancelar <id>")
        return
    try:
        rid = int(args[0])
    except ValueError:
        await msg.reply_text("ID deve ser um número.")
        return

    if cancel(rid):
        await msg.reply_text(f"Lembrete {rid} cancelado.")
    else:
        await msg.reply_text(f"Lembrete {rid} não encontrado ou já inativo.")
