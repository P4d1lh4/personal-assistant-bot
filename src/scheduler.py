import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from telegram.ext import Application

from .config import OWNER_CHAT_ID
from .db import conn_ctx

log = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_app: Optional[Application] = None


def init_scheduler(app: Application) -> AsyncIOScheduler:
    global _scheduler, _app
    _app = app
    _scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")
    _scheduler.start()
    return _scheduler


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
