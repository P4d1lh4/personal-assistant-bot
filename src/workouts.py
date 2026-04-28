from datetime import date, datetime, timedelta
from typing import Optional

from .db import conn_ctx

WEEKDAY_NAMES = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
WEEKDAY_SHORT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
_WEEKDAY_ALIASES = {
    "segunda": 0, "seg": 0, "segunda-feira": 0, "monday": 0,
    "terça": 1, "terca": 1, "ter": 1, "terça-feira": 1, "terca-feira": 1, "tuesday": 1,
    "quarta": 2, "qua": 2, "quarta-feira": 2, "wednesday": 2,
    "quinta": 3, "qui": 3, "quinta-feira": 3, "thursday": 3,
    "sexta": 4, "sex": 4, "sexta-feira": 4, "friday": 4,
    "sábado": 5, "sabado": 5, "sab": 5, "sáb": 5, "saturday": 5,
    "domingo": 6, "dom": 6, "sunday": 6,
}


class ActiveSessionExists(Exception):
    def __init__(self, session: dict):
        super().__init__(f"sessão {session['id']} ainda ativa")
        self.session = session


class NoActiveSession(Exception):
    pass


def parse_weekday(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int) and 0 <= value <= 6:
        return value
    if isinstance(value, str):
        return _WEEKDAY_ALIASES.get(value.strip().lower())
    return None


# ---------- Workouts (planos) ----------

def create_workout(
    name: str,
    description: Optional[str],
    exercises: list[dict],
) -> int:
    name = name.strip()
    if not name:
        raise ValueError("nome do treino vazio")

    with conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO workouts (name, description) VALUES (?, ?)",
            (name, (description or "").strip() or None),
        )
        workout_id = cur.lastrowid
        _insert_exercises(conn, workout_id, exercises)
    return workout_id


def replace_workout_exercises(workout_id: int, exercises: list[dict]) -> None:
    with conn_ctx() as conn:
        conn.execute(
            "DELETE FROM workout_exercises WHERE workout_id = ?", (workout_id,)
        )
        _insert_exercises(conn, workout_id, exercises)
        conn.execute(
            "UPDATE workouts SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (workout_id,),
        )


def _insert_exercises(conn, workout_id: int, exercises: list[dict]) -> None:
    for idx, ex in enumerate(exercises):
        name = (ex.get("name") or "").strip()
        if not name:
            continue
        conn.execute(
            "INSERT INTO workout_exercises "
            "(workout_id, name, sets, reps, target_weight, notes, order_index) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                workout_id,
                name,
                ex.get("sets"),
                str(ex["reps"]) if ex.get("reps") is not None else None,
                ex.get("target_weight") or ex.get("weight"),
                (ex.get("notes") or None),
                idx,
            ),
        )


def delete_workout(workout_id: int) -> bool:
    with conn_ctx() as conn:
        cur = conn.execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
        return cur.rowcount > 0


def get_workout(workout_id: int) -> Optional[dict]:
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id, name, description, created_at, updated_at "
            "FROM workouts WHERE id = ?",
            (workout_id,),
        ).fetchone()
        if not row:
            return None
        ex_rows = conn.execute(
            "SELECT id, name, sets, reps, target_weight, notes, order_index "
            "FROM workout_exercises WHERE workout_id = ? "
            "ORDER BY order_index, id",
            (workout_id,),
        ).fetchall()
    out = dict(row)
    out["exercises"] = [dict(r) for r in ex_rows]
    return out


def get_workout_by_name(name: str) -> Optional[dict]:
    name = (name or "").strip()
    if not name:
        return None
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id FROM workouts WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
    return get_workout(row["id"]) if row else None


def list_workouts() -> list[dict]:
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT id, name, description FROM workouts ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Schedule semanal ----------

def set_schedule_for_weekday(weekday: int, workout_id: Optional[int]) -> None:
    if not 0 <= weekday <= 6:
        raise ValueError(f"weekday inválido: {weekday}")
    with conn_ctx() as conn:
        conn.execute(
            "INSERT INTO workout_schedule (weekday, workout_id) VALUES (?, ?) "
            "ON CONFLICT(weekday) DO UPDATE SET workout_id = excluded.workout_id",
            (weekday, workout_id),
        )


def clear_schedule_for_weekday(weekday: int) -> None:
    with conn_ctx() as conn:
        conn.execute("DELETE FROM workout_schedule WHERE weekday = ?", (weekday,))


def get_schedule() -> dict[int, Optional[dict]]:
    """Retorna dict {0..6: workout_dict_or_None}. Dias sem entrada não aparecem."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT s.weekday, w.id, w.name, w.description "
            "FROM workout_schedule s "
            "LEFT JOIN workouts w ON w.id = s.workout_id "
            "ORDER BY s.weekday"
        ).fetchall()
    out: dict[int, Optional[dict]] = {}
    for r in rows:
        if r["id"] is None:
            out[r["weekday"]] = None  # descanso explícito
        else:
            out[r["weekday"]] = {
                "id": r["id"],
                "name": r["name"],
                "description": r["description"],
            }
    return out


def get_today_workout() -> Optional[dict]:
    """Retorna o workout (com exercises) agendado para hoje, se houver."""
    today_idx = datetime.now().weekday()
    schedule = get_schedule()
    if today_idx not in schedule:
        return None
    info = schedule[today_idx]
    if info is None:
        return None
    return get_workout(info["id"])


# ---------- Sessões (execução) ----------

def get_active_session() -> Optional[dict]:
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT s.id, s.workout_id, s.started_at, s.notes, w.name AS workout_name "
            "FROM workout_sessions s "
            "LEFT JOIN workouts w ON w.id = s.workout_id "
            "WHERE s.completed_at IS NULL "
            "ORDER BY s.started_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def start_session(workout_id: Optional[int]) -> int:
    active = get_active_session()
    if active:
        raise ActiveSessionExists(active)
    with conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO workout_sessions (workout_id) VALUES (?)",
            (workout_id,),
        )
        return cur.lastrowid


def cancel_session(session_id: int) -> bool:
    with conn_ctx() as conn:
        cur = conn.execute(
            "DELETE FROM workout_sessions WHERE id = ? AND completed_at IS NULL",
            (session_id,),
        )
        return cur.rowcount > 0


def finish_session(session_id: int, notes: Optional[str] = None) -> Optional[dict]:
    with conn_ctx() as conn:
        cur = conn.execute(
            "UPDATE workout_sessions "
            "SET completed_at = CURRENT_TIMESTAMP, notes = COALESCE(?, notes) "
            "WHERE id = ? AND completed_at IS NULL",
            (notes, session_id),
        )
        if cur.rowcount == 0:
            return None
    return get_session_summary(session_id)


def log_set(
    exercise_name: str,
    reps_done: Optional[int],
    weight_used: Optional[float],
    notes: Optional[str] = None,
) -> dict:
    """Registra uma série na sessão ativa. Retorna dict com {log_id, set_number, is_pr, prev_max_weight}."""
    session = get_active_session()
    if not session:
        raise NoActiveSession()

    exercise_name = (exercise_name or "").strip()
    if not exercise_name:
        raise ValueError("nome do exercício vazio")

    session_id = session["id"]

    with conn_ctx() as conn:
        # Próximo número de série pra esse exercício nessa sessão
        row = conn.execute(
            "SELECT COALESCE(MAX(set_number), 0) AS max_set "
            "FROM session_logs WHERE session_id = ? AND exercise_name = ? COLLATE NOCASE",
            (session_id, exercise_name),
        ).fetchone()
        set_number = (row["max_set"] or 0) + 1

        # PR check: comparar com histórico fora dessa sessão
        prev_row = conn.execute(
            "SELECT MAX(weight_used) AS prev_max FROM session_logs "
            "WHERE exercise_name = ? COLLATE NOCASE AND session_id != ?",
            (exercise_name, session_id),
        ).fetchone()
        prev_max = prev_row["prev_max"]

        cur = conn.execute(
            "INSERT INTO session_logs "
            "(session_id, exercise_name, set_number, reps_done, weight_used, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, exercise_name, set_number, reps_done, weight_used, notes),
        )
        log_id = cur.lastrowid

    is_pr = bool(
        weight_used is not None
        and weight_used > 0
        and (prev_max is None or weight_used > prev_max)
    )
    return {
        "log_id": log_id,
        "set_number": set_number,
        "is_pr": is_pr,
        "prev_max_weight": prev_max,
        "session_id": session_id,
    }


def get_session_summary(session_id: int) -> Optional[dict]:
    with conn_ctx() as conn:
        sess = conn.execute(
            "SELECT s.id, s.started_at, s.completed_at, s.notes, "
            "w.name AS workout_name "
            "FROM workout_sessions s LEFT JOIN workouts w ON w.id = s.workout_id "
            "WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if not sess:
            return None
        log_rows = conn.execute(
            "SELECT exercise_name, set_number, reps_done, weight_used, notes "
            "FROM session_logs WHERE session_id = ? "
            "ORDER BY logged_at",
            (session_id,),
        ).fetchall()
    out = dict(sess)
    out["logs"] = [dict(r) for r in log_rows]
    return out


# ---------- Histórico ----------

def get_workout_streak() -> int:
    """Dias consecutivos com pelo menos 1 sessão encerrada, terminando hoje ou ontem.

    Se a última sessão foi anteontem ou antes, considera streak quebrado (retorna 0).
    """
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT DISTINCT DATE(started_at) AS day FROM workout_sessions "
            "WHERE completed_at IS NOT NULL "
            "ORDER BY DATE(started_at) DESC"
        ).fetchall()
    if not rows:
        return 0

    days = [date.fromisoformat(r["day"]) for r in rows]
    today = datetime.now().date()
    if days[0] not in (today, today - timedelta(days=1)):
        return 0

    streak = 1
    for i in range(1, len(days)):
        if days[i] == days[i - 1] - timedelta(days=1):
            streak += 1
        else:
            break
    return streak


def get_workout_stats(days: int = 30) -> dict:
    """Agrega total, breakdown por treino, frequência semanal e streak."""
    if days < 1:
        days = 1
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)

    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT s.id, s.started_at, s.completed_at, "
            "COALESCE(w.name, '(sem treino vinculado)') AS workout_name "
            "FROM workout_sessions s "
            "LEFT JOIN workouts w ON w.id = s.workout_id "
            "WHERE s.completed_at IS NOT NULL "
            "AND DATE(s.started_at) >= ? AND DATE(s.started_at) <= ? "
            "ORDER BY s.started_at DESC",
            (start.isoformat(), today.isoformat()),
        ).fetchall()

    by_workout: dict[str, int] = {}
    for r in rows:
        name = r["workout_name"]
        by_workout[name] = by_workout.get(name, 0) + 1

    weeks = days / 7
    per_week_avg = round(len(rows) / weeks, 1) if weeks >= 1 else None

    return {
        "total": len(rows),
        "days": days,
        "by_workout": by_workout,
        "last_session_at": rows[0]["started_at"] if rows else None,
        "per_week_avg": per_week_avg,
        "streak": get_workout_streak(),
        "start_date": start.isoformat(),
        "end_date": today.isoformat(),
    }


def get_exercise_history(exercise_name: str, limit_sessions: int = 6) -> list[dict]:
    """Retorna histórico recente do exercício agrupado por sessão."""
    pattern = f"%{exercise_name.strip()}%"
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT l.exercise_name, l.set_number, l.reps_done, l.weight_used, "
            "l.session_id, s.started_at, s.completed_at "
            "FROM session_logs l "
            "JOIN workout_sessions s ON s.id = l.session_id "
            "WHERE l.exercise_name LIKE ? COLLATE NOCASE "
            "AND s.completed_at IS NOT NULL "
            "ORDER BY s.started_at DESC, l.set_number",
            (pattern,),
        ).fetchall()

    sessions: dict[int, dict] = {}
    for r in rows:
        sid = r["session_id"]
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "started_at": r["started_at"],
                "completed_at": r["completed_at"],
                "exercise_name": r["exercise_name"],
                "sets": [],
            }
        sessions[sid]["sets"].append({
            "set_number": r["set_number"],
            "reps_done": r["reps_done"],
            "weight_used": r["weight_used"],
        })
    ordered = sorted(sessions.values(), key=lambda s: s["started_at"], reverse=True)
    return ordered[:limit_sessions]
