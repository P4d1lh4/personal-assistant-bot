import logging
from datetime import datetime
from typing import Optional

import google.generativeai as genai
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from telegram.ext import Application

from .config import DAILY_DIGEST_HOUR, GEMINI_MODEL, OWNER_CHAT_ID
from .db import conn_ctx

log = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_app: Optional[Application] = None


def init_scheduler(app: Application) -> AsyncIOScheduler:
    global _scheduler, _app
    _app = app
    _scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")
    _scheduler.start()
    _register_maintenance_jobs()
    return _scheduler


def _register_maintenance_jobs() -> None:
    """Jobs internos de manutenção (cleanup, resumo diário, etc.)."""
    sched = get_scheduler()
    sched.add_job(
        _cleanup_old_conversations,
        trigger=CronTrigger(hour=3, minute=0, timezone="America/Sao_Paulo"),
        id="maint_cleanup_conversations",
        replace_existing=True,
    )
    sched.add_job(
        _send_daily_digest,
        trigger=CronTrigger(
            hour=DAILY_DIGEST_HOUR, minute=0, timezone="America/Sao_Paulo"
        ),
        id="maint_daily_digest",
        replace_existing=True,
    )
    log.info(
        "Resumo diário agendado para %02d:00 (America/Sao_Paulo).", DAILY_DIGEST_HOUR
    )


def _cleanup_old_conversations(retention_days: int = 30) -> int:
    """Apaga registros de `conversations` mais velhos que `retention_days`."""
    with conn_ctx() as conn:
        cur = conn.execute(
            "DELETE FROM conversations "
            "WHERE created_at < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        deleted = cur.rowcount
    if deleted:
        log.info("Cleanup: %d conversa(s) antiga(s) removida(s).", deleted)
    return deleted


_WEEKDAY_PT = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _collect_digest_data(today: datetime) -> tuple[str, str]:
    today_str = today.date().isoformat()
    with conn_ctx() as conn:
        reminder_rows = conn.execute(
            "SELECT text, cron_or_at, is_recurring FROM reminders WHERE active = 1"
        ).fetchall()
        memory_rows = conn.execute(
            "SELECT category, content FROM memories "
            "WHERE category IN ('routine', 'goal') "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()

    todays_reminders = []
    for r in reminder_rows:
        if r["is_recurring"]:
            todays_reminders.append(
                f"- {r['text']} (recorrente: {r['cron_or_at']})"
            )
        elif r["cron_or_at"].startswith(today_str):
            todays_reminders.append(f"- {r['text']} ({r['cron_or_at']})")

    reminders_block = "\n".join(todays_reminders) or "(sem lembretes específicos hoje)"
    memories_block = "\n".join(
        f"- [{m['category']}] {m['content']}" for m in memory_rows
    ) or "(sem rotinas/metas anotadas)"
    return reminders_block, memories_block


async def _send_daily_digest() -> None:
    if _app is None:
        log.error("App não inicializado, não consegui mandar resumo diário")
        return

    today = datetime.now()
    weekday_pt = _WEEKDAY_PT[today.weekday()]
    reminders_block, memories_block = _collect_digest_data(today)

    prompt = (
        f"Você é o assistente pessoal do Guilherme. Crie uma mensagem curta "
        f"(3-5 linhas) de bom dia, em português brasileiro, mencionando os "
        f"compromissos do dia e suas rotinas/metas relevantes. Tom natural, "
        f"direto, próximo. Sem markdown, sem bullets formais.\n\n"
        f"Hoje é {today.date().isoformat()} ({weekday_pt}).\n\n"
        f"Lembretes de hoje:\n{reminders_block}\n\n"
        f"Rotinas e metas:\n{memories_block}\n\n"
        f"Mensagem:"
    )

    text: Optional[str] = None
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = await model.generate_content_async(prompt)
        text = (response.text or "").strip() or None
    except Exception:
        log.exception("Falha ao gerar resumo diário, usando fallback")

    if not text:
        text = f"Bom dia, Guilherme!\n\nLembretes de hoje:\n{reminders_block}"

    try:
        await _app.bot.send_message(chat_id=OWNER_CHAT_ID, text=text)
        log.info("Resumo diário enviado.")
    except Exception:
        log.exception("Falha ao enviar resumo diário")


def get_scheduler() -> AsyncIOScheduler:
    if _scheduler is None:
        raise RuntimeError("Scheduler não inicializado. Chame init_scheduler primeiro.")
    return _scheduler


async def _send_reminder(text: str, reminder_id: int, recurring: bool) -> None:
    if _app is None:
        log.error("App não inicializado, não consegui enviar lembrete %s", reminder_id)
        return
    try:
        await _app.bot.send_message(chat_id=OWNER_CHAT_ID, text=f"⏰ Lembrete: {text}")
    except Exception:
        log.exception("Erro ao enviar lembrete %s", reminder_id)
        return

    if not recurring:
        with conn_ctx() as conn:
            conn.execute(
                "UPDATE reminders SET active = 0 WHERE id = ?", (reminder_id,)
            )


def _build_trigger(cron_or_at: str, is_recurring: bool):
    if is_recurring:
        return CronTrigger.from_crontab(cron_or_at, timezone="America/Sao_Paulo")
    run_at = datetime.fromisoformat(cron_or_at)
    return DateTrigger(run_date=run_at, timezone="America/Sao_Paulo")


def add_reminder_job(reminder_id: int, text: str, cron_or_at: str, is_recurring: bool) -> None:
    sched = get_scheduler()
    trigger = _build_trigger(cron_or_at, is_recurring)
    sched.add_job(
        _send_reminder,
        trigger=trigger,
        args=[text, reminder_id, is_recurring],
        id=f"reminder_{reminder_id}",
        replace_existing=True,
        misfire_grace_time=300,
    )


def remove_reminder_job(reminder_id: int) -> None:
    sched = get_scheduler()
    job_id = f"reminder_{reminder_id}"
    if sched.get_job(job_id):
        sched.remove_job(job_id)


def load_reminders_from_db() -> int:
    """Carrega todos os lembretes ativos do banco e registra como jobs."""
    loaded = 0
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT id, text, cron_or_at, is_recurring FROM reminders WHERE active = 1"
        ).fetchall()

    now = datetime.now()
    for r in rows:
        is_recurring = bool(r["is_recurring"])
        # Pula lembretes one-shot que já passaram
        if not is_recurring:
            try:
                run_at = datetime.fromisoformat(r["cron_or_at"])
                if run_at < now:
                    with conn_ctx() as conn:
                        conn.execute(
                            "UPDATE reminders SET active = 0 WHERE id = ?", (r["id"],)
                        )
                    continue
            except ValueError:
                log.warning("Lembrete %s tem cron_or_at inválido: %r", r["id"], r["cron_or_at"])
                continue

        try:
            add_reminder_job(r["id"], r["text"], r["cron_or_at"], is_recurring)
            loaded += 1
        except Exception:
            log.exception("Falha ao carregar lembrete %s", r["id"])

    log.info("Carregados %d lembrete(s) do banco.", loaded)
    return loaded
