from datetime import datetime

from .db import conn_ctx
from .scheduler import add_reminder_job, remove_reminder_job


def create_one_shot(text: str, run_at: datetime) -> tuple[int, str]:
    cron_or_at = run_at.isoformat(timespec="seconds")
    with conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (text, cron_or_at, is_recurring, active) "
            "VALUES (?, ?, 0, 1)",
            (text, cron_or_at),
        )
        rid = cur.lastrowid
    add_reminder_job(rid, text, cron_or_at, is_recurring=False)
    return rid, cron_or_at


def create_recurring(text: str, cron: str) -> int:
    with conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (text, cron_or_at, is_recurring, active) "
            "VALUES (?, ?, 1, 1)",
            (text, cron),
        )
        rid = cur.lastrowid
    add_reminder_job(rid, text, cron, is_recurring=True)
    return rid


def cancel(reminder_id: int) -> bool:
    with conn_ctx() as conn:
        cur = conn.execute(
            "UPDATE reminders SET active = 0 WHERE id = ? AND active = 1",
            (reminder_id,),
        )
        ok = cur.rowcount > 0
    if ok:
        try:
            remove_reminder_job(reminder_id)
        except Exception:
            pass
    return ok


def list_active() -> list[dict]:
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT id, text, cron_or_at, is_recurring FROM reminders "
            "WHERE active = 1 ORDER BY cron_or_at"
        ).fetchall()
    return [dict(r) for r in rows]
