import re
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from ..db import conn_ctx
from ..scheduler import add_reminder_job, remove_reminder_job
from .auth import owner_only

DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})$")
DATE_ONLY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})$")
TIME_ONLY_RE = re.compile(r"^(\d{2}:\d{2})$")


def _parse_when(tokens: list[str]) -> tuple[datetime, int]:
    """Retorna (run_at, tokens_consumed) ou levanta ValueError."""
    if not tokens:
        raise ValueError("data/hora ausente")

    now = datetime.now()
    first = tokens[0].lower()

    # "hoje HH:MM"
    if first == "hoje" and len(tokens) >= 2 and TIME_ONLY_RE.match(tokens[1]):
        h, m = map(int, tokens[1].split(":"))
        run_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if run_at <= now:
            raise ValueError("horário de hoje já passou")
        return run_at, 2

    # "amanhã HH:MM"
    if first in {"amanhã", "amanha"} and len(tokens) >= 2 and TIME_ONLY_RE.match(tokens[1]):
        h, m = map(int, tokens[1].split(":"))
        target = (now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
        return target, 2

    # "YYYY-MM-DD HH:MM"
    if len(tokens) >= 2 and DATE_ONLY_RE.match(tokens[0]) and TIME_ONLY_RE.match(tokens[1]):
        run_at = datetime.fromisoformat(f"{tokens[0]}T{tokens[1]}:00")
        if run_at <= now:
            raise ValueError("data/hora já passou")
        return run_at, 2

    # "YYYY-MM-DDTHH:MM" ou "YYYY-MM-DDTHH:MM:SS"
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

    cron_or_at = run_at.isoformat(timespec="seconds")
    with conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (text, cron_or_at, is_recurring, active) VALUES (?, ?, 0, 1)",
            (text, cron_or_at),
        )
        reminder_id = cur.lastrowid

    try:
        add_reminder_job(reminder_id, text, cron_or_at, is_recurring=False)
    except Exception as e:
        await msg.reply_text(f"Salvo no banco mas falhou ao agendar: {e}")
        return

    await msg.reply_text(
        f"⏰ Lembrete #{reminder_id} agendado para {run_at.strftime('%d/%m/%Y %H:%M')}: {text}"
    )


@owner_only
async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT id, text, cron_or_at, is_recurring FROM reminders "
            "WHERE active = 1 ORDER BY cron_or_at"
        ).fetchall()

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

    with conn_ctx() as conn:
        cur = conn.execute(
            "UPDATE reminders SET active = 0 WHERE id = ? AND active = 1", (rid,)
        )
        ok = cur.rowcount > 0

    if ok:
        try:
            remove_reminder_job(rid)
        except Exception:
            pass
        await msg.reply_text(f"Lembrete {rid} cancelado.")
    else:
        await msg.reply_text(f"Lembrete {rid} não encontrado ou já inativo.")
