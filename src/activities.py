from datetime import date, datetime, timedelta
from typing import Optional

from .db import conn_ctx


def _serialize_days(days: Optional[list[int]]) -> Optional[str]:
    if not days:
        return None
    cleaned = sorted({int(d) for d in days if 0 <= int(d) <= 6})
    if not cleaned:
        return None
    return ",".join(str(d) for d in cleaned)


def _parse_days(raw: Optional[str]) -> Optional[list[int]]:
    if not raw:
        return None
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            out.append(int(part))
    return out or None


def _is_due_on(weekday: int, days: Optional[list[int]]) -> bool:
    if days is None:
        return True  # todo dia
    return weekday in days


def create(
    name: str,
    category: str,
    days_of_week: Optional[list[int]] = None,
) -> int:
    name = (name or "").strip()
    category = (category or "").strip()
    if not name:
        raise ValueError("nome da atividade vazio")
    if not category:
        raise ValueError("categoria da atividade vazia")
    with conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO activities (name, category, days_of_week, active) "
            "VALUES (?, ?, ?, 1)",
            (name, category, _serialize_days(days_of_week)),
        )
        return cur.lastrowid


def deactivate(activity_id: int) -> bool:
    with conn_ctx() as conn:
        cur = conn.execute(
            "UPDATE activities SET active = 0 WHERE id = ? AND active = 1",
            (activity_id,),
        )
        return cur.rowcount > 0


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["days_of_week"] = _parse_days(d.get("days_of_week"))
    return d


def get_by_id(activity_id: int) -> Optional[dict]:
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id, name, category, days_of_week, active, created_at "
            "FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_by_name(name: str) -> Optional[dict]:
    name = (name or "").strip()
    if not name:
        return None
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id, name, category, days_of_week, active, created_at "
            "FROM activities WHERE name = ? COLLATE NOCASE AND active = 1",
            (name,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_active(category: Optional[str] = None) -> list[dict]:
    sql = (
        "SELECT id, name, category, days_of_week FROM activities "
        "WHERE active = 1"
    )
    params: tuple = ()
    if category:
        sql += " AND LOWER(category) = LOWER(?)"
        params = (category,)
    sql += " ORDER BY category, name"
    with conn_ctx() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_due_today() -> list[dict]:
    """Atividades ativas previstas para hoje (filtrando por days_of_week)."""
    today_idx = datetime.now().weekday()
    return [a for a in list_active() if _is_due_on(today_idx, a["days_of_week"])]


def find_solo_active() -> Optional[dict]:
    rows = list_active()
    return rows[0] if len(rows) == 1 else None


def track(
    activity_id: int,
    status: str,
    log_date: Optional[str] = None,
) -> int:
    """UPSERT em (activity_id, log_date). Status 'done' ou 'skipped'."""
    if status not in {"done", "skipped"}:
        raise ValueError(f"status inválido: {status!r}")
    if log_date is None:
        log_date = datetime.now().date().isoformat()
    with conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO activity_logs (activity_id, log_date, status) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(activity_id, log_date) DO UPDATE SET "
            "status = excluded.status, logged_at = CURRENT_TIMESTAMP",
            (activity_id, log_date, status),
        )
        # Em UPDATE, lastrowid pode ser 0 — buscar o id real
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM activity_logs WHERE activity_id = ? AND log_date = ?",
            (activity_id, log_date),
        ).fetchone()
        return row["id"] if row else 0


def untrack(activity_id: int, log_date: Optional[str] = None) -> bool:
    if log_date is None:
        log_date = datetime.now().date().isoformat()
    with conn_ctx() as conn:
        cur = conn.execute(
            "DELETE FROM activity_logs WHERE activity_id = ? AND log_date = ?",
            (activity_id, log_date),
        )
        return cur.rowcount > 0


def get_log(activity_id: int, log_date: Optional[str] = None) -> Optional[dict]:
    if log_date is None:
        log_date = datetime.now().date().isoformat()
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id, activity_id, log_date, status, logged_at "
            "FROM activity_logs WHERE activity_id = ? AND log_date = ?",
            (activity_id, log_date),
        ).fetchone()
    return dict(row) if row else None


def compliance(activity_id: int, days: int = 30) -> dict:
    """Calcula adesão: done / dias previstos na janela.

    `expected` considera `days_of_week` (não conta dia de descanso como devido).
    """
    if days < 1:
        days = 1
    activity = get_by_id(activity_id)
    if activity is None:
        return {
            "done": 0, "skipped": 0, "expected": 0, "percent": 0.0,
            "start_date": None, "end_date": None, "dates_done": [],
        }

    today = datetime.now().date()
    start = today - timedelta(days=days - 1)

    # Conta dias previstos na janela
    expected = 0
    cursor = start
    while cursor <= today:
        if _is_due_on(cursor.weekday(), activity["days_of_week"]):
            expected += 1
        cursor += timedelta(days=1)

    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT log_date, status FROM activity_logs "
            "WHERE activity_id = ? AND log_date >= ? AND log_date <= ? "
            "ORDER BY log_date",
            (activity_id, start.isoformat(), today.isoformat()),
        ).fetchall()

    done_dates = [r["log_date"] for r in rows if r["status"] == "done"]
    skipped_count = sum(1 for r in rows if r["status"] == "skipped")

    percent = round(100 * len(done_dates) / expected, 1) if expected > 0 else 0.0
    return {
        "done": len(done_dates),
        "skipped": skipped_count,
        "expected": expected,
        "percent": percent,
        "start_date": start.isoformat(),
        "end_date": today.isoformat(),
        "dates_done": done_dates,
    }


def category_compliance(category: str, days: int = 30) -> dict:
    """Adesão agregada por categoria. Soma `done` / soma `expected` entre as atividades."""
    actives = list_active(category=category)
    total_done = 0
    total_expected = 0
    per_activity = []
    for a in actives:
        c = compliance(a["id"], days=days)
        total_done += c["done"]
        total_expected += c["expected"]
        per_activity.append({
            "name": a["name"],
            "done": c["done"],
            "expected": c["expected"],
            "percent": c["percent"],
        })
    percent = round(100 * total_done / total_expected, 1) if total_expected > 0 else 0.0
    return {
        "category": category,
        "done": total_done,
        "expected": total_expected,
        "percent": percent,
        "days": days,
        "per_activity": per_activity,
    }
