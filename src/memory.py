from typing import Optional

from .db import conn_ctx

VALID_CATEGORIES = {"fact", "preference", "routine", "goal", "habit"}


def add_memory(category: str, content: str, source: str = "manual") -> int:
    category = category if category in VALID_CATEGORIES else "fact"
    with conn_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO memories (category, content, source) VALUES (?, ?, ?)",
            (category, content.strip(), source),
        )
        return cur.lastrowid


def list_memories(category: Optional[str] = None, limit: int = 100) -> list[dict]:
    sql = "SELECT id, category, content, source, created_at FROM memories"
    params: tuple = ()
    if category:
        sql += " WHERE category = ?"
        params = (category,)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params = (*params, limit)

    with conn_ctx() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def delete_memory(memory_id: int) -> bool:
    with conn_ctx() as conn:
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cur.rowcount > 0


def update_memory(memory_id: int, content: str) -> bool:
    with conn_ctx() as conn:
        cur = conn.execute(
            "UPDATE memories SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (content.strip(), memory_id),
        )
        return cur.rowcount > 0


def get_memory_summary(limit_per_category: int = 50) -> str:
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT category, content FROM memories ORDER BY category, created_at DESC"
        ).fetchall()

    if not rows:
        return "(nenhuma memória registrada ainda)"

    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["category"], []).append(r["content"])

    parts = []
    for cat, items in grouped.items():
        items = items[:limit_per_category]
        bullets = "\n".join(f"- {it}" for it in items)
        parts.append(f"[{cat}]\n{bullets}")
    return "\n\n".join(parts)


def append_message(role: str, content: str) -> None:
    with conn_ctx() as conn:
        conn.execute(
            "INSERT INTO conversations (role, content) VALUES (?, ?)",
            (role, content),
        )


def get_recent_history(limit: int = 20) -> list[dict]:
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    return history
