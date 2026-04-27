from datetime import datetime, timedelta
from typing import Optional

from .db import conn_ctx


def create(name: str, schedule_cron: str) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("nome do medicamento vazio")
    if not schedule_cron or not schedule_cron.strip():
        raise ValueError("horário (cron) vazio")
    with conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO medications (name, schedule_cron, active) VALUES (?, ?, 1)",
            (name, schedule_cron.strip()),
        )
        return cur.lastrowid


def deactivate(med_id: int) -> bool:
    with conn_ctx() as conn:
        cur = conn.execute(
            "UPDATE medications SET active = 0 WHERE id = ? AND active = 1", (med_id,)
        )
        return cur.rowcount > 0


def get_by_id(med_id: int) -> Optional[dict]:
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id, name, schedule_cron, active, created_at FROM medications "
            "WHERE id = ?",
            (med_id,),
        ).fetchone()
    return dict(row) if row else None


def get_by_name(name: str) -> Optional[dict]:
    name = (name or "").strip()
    if not name:
        return None
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id, name, schedule_cron, active, created_at FROM medications "
            "WHERE name = ? COLLATE NOCASE AND active = 1",
            (name,),
        ).fetchone()
    return dict(row) if row else None


def list_active() -> list[dict]:
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT id, name, schedule_cron FROM medications "
            "WHERE active = 1 ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def find_solo_active() -> Optional[dict]:
    """Retorna o único medicamento ativo se houver exatamente 1, senão None."""
    rows = list_active()
    return rows[0] if len(rows) == 1 else None


def track(med_id: int, skipped: bool = False) -> int:
    """Registra uma tomada (ou skip) — sempre cria nova entrada."""
    with conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO medication_intakes (medication_id, skipped) VALUES (?, ?)",
            (med_id, 1 if skipped else 0),
        )
        return cur.lastrowid


def has_intake_today(med_id: int) -> bool:
    today = datetime.now().date().isoformat()
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT 1 FROM medication_intakes "
            "WHERE medication_id = ? AND DATE(taken_at) = ? AND skipped = 0 "
            "LIMIT 1",
            (med_id, today),
        ).fetchone()
    return row is not None


def untrack_today(med_id: int) -> int:
    """Remove registros (não-skipped) de hoje desse medicamento."""
    today = datetime.now().date().isoformat()
    with conn_ctx() as conn:
        cur = conn.execute(
            "DELETE FROM medication_intakes "
            "WHERE medication_id = ? AND DATE(taken_at) = ? AND skipped = 0",
            (med_id, today),
        )
        return cur.rowcount


def compliance(med_id: int, days: int = 30) -> dict:
    """Retorna {taken, total, percent, dates_taken} para a janela de N dias terminando hoje."""
    if days < 1:
        days = 1
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)

    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT DATE(taken_at) AS day FROM medication_intakes "
            "WHERE medication_id = ? AND skipped = 0 "
            "AND DATE(taken_at) >= ? AND DATE(taken_at) <= ? "
            "GROUP BY DATE(taken_at) "
            "ORDER BY DATE(taken_at)",
            (med_id, start.isoformat(), today.isoformat()),
        ).fetchall()

    dates_taken = [r["day"] for r in rows]
    taken = len(dates_taken)
    percent = round(100 * taken / days, 1)
    return {
        "taken": taken,
        "total": days,
        "percent": percent,
        "dates_taken": dates_taken,
        "start_date": start.isoformat(),
        "end_date": today.isoformat(),
    }
