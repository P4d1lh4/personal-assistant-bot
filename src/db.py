import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,
    content     TEXT NOT NULL,
    source      TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    text         TEXT NOT NULL,
    cron_or_at   TEXT NOT NULL,
    is_recurring INTEGER DEFAULT 0,
    active       INTEGER DEFAULT 1,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_conversations_created_at ON conversations(created_at);

CREATE TABLE IF NOT EXISTS workouts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workout_exercises (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id    INTEGER NOT NULL,
    name          TEXT NOT NULL,
    sets          INTEGER,
    reps          TEXT,
    target_weight REAL,
    notes         TEXT,
    order_index   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (workout_id) REFERENCES workouts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workout_exercises_workout
    ON workout_exercises(workout_id);

-- weekday: 0=segunda..6=domingo (Python convention)
-- workout_id NULL = dia de descanso explícito
CREATE TABLE IF NOT EXISTS workout_schedule (
    weekday    INTEGER PRIMARY KEY,
    workout_id INTEGER,
    FOREIGN KEY (workout_id) REFERENCES workouts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS workout_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id   INTEGER,
    started_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    notes        TEXT,
    FOREIGN KEY (workout_id) REFERENCES workouts(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_workout_sessions_active
    ON workout_sessions(completed_at);

CREATE TABLE IF NOT EXISTS session_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL,
    exercise_name TEXT NOT NULL COLLATE NOCASE,
    set_number    INTEGER,
    reps_done     INTEGER,
    weight_used   REAL,
    notes         TEXT,
    logged_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES workout_sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_session_logs_session ON session_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_session_logs_exercise ON session_logs(exercise_name);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def conn_ctx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with conn_ctx() as conn:
        conn.executescript(SCHEMA)
