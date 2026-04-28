from datetime import datetime, timedelta

from src import workouts
from src.db import conn_ctx


# ---------- parse_weekday ----------

def test_parse_weekday_pt_long():
    assert workouts.parse_weekday("segunda") == 0
    assert workouts.parse_weekday("terça") == 1
    assert workouts.parse_weekday("domingo") == 6


def test_parse_weekday_pt_short():
    assert workouts.parse_weekday("seg") == 0
    assert workouts.parse_weekday("sex") == 4


def test_parse_weekday_int_valid():
    assert workouts.parse_weekday(0) == 0
    assert workouts.parse_weekday(6) == 6


def test_parse_weekday_invalid():
    assert workouts.parse_weekday(None) is None
    assert workouts.parse_weekday("foo") is None
    assert workouts.parse_weekday(7) is None


# ---------- create / get / list ----------

def _make_basic_workout():
    return workouts.create_workout(
        "Treino A",
        "Peito/Tríceps",
        [
            {"name": "supino", "sets": 4, "reps": "10", "weight": 60},
            {"name": "tríceps", "sets": 3, "reps": "12", "weight": 30},
        ],
    )


def test_create_and_get_workout():
    wid = _make_basic_workout()
    assert wid > 0
    w = workouts.get_workout(wid)
    assert w["name"] == "Treino A"
    assert len(w["exercises"]) == 2
    assert w["exercises"][0]["name"] == "supino"


def test_get_workout_by_name_case_insensitive():
    _make_basic_workout()
    w = workouts.get_workout_by_name("treino a")
    assert w is not None
    assert w["name"] == "Treino A"


def test_list_workouts():
    _make_basic_workout()
    rows = workouts.list_workouts()
    assert len(rows) == 1
    assert rows[0]["name"] == "Treino A"


def test_replace_workout_exercises():
    wid = _make_basic_workout()
    workouts.replace_workout_exercises(
        wid,
        [{"name": "rosca direta", "sets": 3, "reps": "12"}],
    )
    w = workouts.get_workout(wid)
    assert len(w["exercises"]) == 1
    assert w["exercises"][0]["name"] == "rosca direta"


def test_delete_workout():
    wid = _make_basic_workout()
    assert workouts.delete_workout(wid) is True
    assert workouts.get_workout(wid) is None


# ---------- session + log_set + streak/stats ----------

def test_log_set_without_session_raises():
    import pytest
    with pytest.raises(workouts.NoActiveSession):
        workouts.log_set("supino", 10, 60)


def test_full_session_flow_with_pr():
    wid = _make_basic_workout()
    sid = workouts.start_session(wid)
    assert sid > 0
    assert workouts.get_active_session()["id"] == sid

    r1 = workouts.log_set("supino", 10, 60)
    assert r1["set_number"] == 1
    assert r1["is_pr"] is True  # primeiro registro
    assert r1["prev_max_weight"] is None

    r2 = workouts.log_set("supino", 8, 65)
    assert r2["set_number"] == 2
    assert r2["is_pr"] is True  # peso aumentou
    assert r2["prev_max_weight"] == 60

    summary = workouts.finish_session(sid)
    assert summary is not None
    assert len(summary["logs"]) == 2

    # Após encerrar, não há sessão ativa
    assert workouts.get_active_session() is None


def test_active_session_exists_blocks_second_start():
    import pytest
    wid = _make_basic_workout()
    workouts.start_session(wid)
    with pytest.raises(workouts.ActiveSessionExists):
        workouts.start_session(wid)


def test_streak_zero_when_no_sessions():
    assert workouts.get_workout_streak() == 0


def test_streak_one_when_today_only():
    wid = _make_basic_workout()
    sid = workouts.start_session(wid)
    workouts.log_set("supino", 10, 60)
    workouts.finish_session(sid)
    assert workouts.get_workout_streak() == 1


def test_streak_broken_when_old_session():
    wid = _make_basic_workout()
    sid = workouts.start_session(wid)
    workouts.log_set("supino", 10, 60)
    workouts.finish_session(sid)
    # Forçar started_at e completed_at pra 5 dias atrás
    five_days_ago = (datetime.now() - timedelta(days=5)).isoformat()
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE workout_sessions SET started_at = ?, completed_at = ? WHERE id = ?",
            (five_days_ago, five_days_ago, sid),
        )
    assert workouts.get_workout_streak() == 0


def test_stats_total_and_breakdown():
    wid = _make_basic_workout()
    for _ in range(3):
        sid = workouts.start_session(wid)
        workouts.log_set("supino", 10, 60)
        workouts.finish_session(sid)
    stats = workouts.get_workout_stats(days=30)
    assert stats["total"] == 3
    assert stats["by_workout"]["Treino A"] == 3
    assert stats["streak"] >= 1


def test_stats_zero_when_no_completed():
    stats = workouts.get_workout_stats(days=30)
    assert stats["total"] == 0
    assert stats["by_workout"] == {}
    assert stats["streak"] == 0


# ---------- schedule ----------

def test_schedule_set_and_get():
    wid = _make_basic_workout()
    workouts.set_schedule_for_weekday(0, wid)  # segunda
    workouts.set_schedule_for_weekday(2, None)  # quarta = descanso
    sched = workouts.get_schedule()
    assert sched[0]["name"] == "Treino A"
    assert sched[2] is None
    assert 1 not in sched


def test_clear_schedule():
    wid = _make_basic_workout()
    workouts.set_schedule_for_weekday(0, wid)
    workouts.clear_schedule_for_weekday(0)
    assert 0 not in workouts.get_schedule()


# ---------- exercise history ----------

def test_get_exercise_history_only_includes_completed():
    wid = _make_basic_workout()
    sid = workouts.start_session(wid)
    workouts.log_set("supino", 10, 60)
    # Não encerra: não deve aparecer no histórico
    hist = workouts.get_exercise_history("supino")
    assert hist == []
    # Encerra: agora aparece
    workouts.finish_session(sid)
    hist = workouts.get_exercise_history("supino")
    assert len(hist) == 1
