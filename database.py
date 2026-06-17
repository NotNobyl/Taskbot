"""
database.py — SQLite task and reminder storage
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "tasks.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                title       TEXT    NOT NULL,
                category    TEXT    DEFAULT 'general',  -- 'it' or 'personal'
                priority    INTEGER DEFAULT 2,           -- 1=high, 2=medium, 3=low
                due_at      TEXT,                        -- ISO datetime, optional
                done        INTEGER DEFAULT 0,
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                user_id     INTEGER NOT NULL,
                remind_at   TEXT    NOT NULL,            -- ISO datetime
                fired       INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS kb_articles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT    NOT NULL,
                content     TEXT    NOT NULL DEFAULT '',  -- markdown
                tags        TEXT    NOT NULL DEFAULT '',  -- comma-separated
                created_at  TEXT    DEFAULT (datetime('now')),
                updated_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS kb_images (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id  INTEGER NOT NULL REFERENCES kb_articles(id) ON DELETE CASCADE,
                filename    TEXT    NOT NULL,
                data        BLOB    NOT NULL,             -- base64 data URL
                created_at  TEXT    DEFAULT (datetime('now'))
            );
        """)


# ── Tasks ──────────────────────────────────────────────────────────────────

def add_task(user_id: int, title: str, category: str = "general",
             priority: int = 2, due_at: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (user_id, title, category, priority, due_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, title, category, priority, due_at)
        )
        return cur.lastrowid


def list_tasks(user_id: int, include_done: bool = False) -> list[sqlite3.Row]:
    with get_conn() as conn:
        done_filter = "" if include_done else "AND done = 0"
        return conn.execute(
            f"SELECT * FROM tasks WHERE user_id = ? {done_filter} "
            "ORDER BY priority ASC, due_at ASC NULLS LAST",
            (user_id,)
        ).fetchall()


def mark_done(user_id: int, task_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET done = 1 WHERE id = ? AND user_id = ?",
            (task_id, user_id)
        )
        return cur.rowcount > 0


def delete_task(user_id: int, task_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id)
        )
        return cur.rowcount > 0


def get_next_task(user_id: int) -> sqlite3.Row | None:
    """Return the highest-priority pending task, favouring overdue ones."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        # Overdue first, then by priority, then by due_at
        return conn.execute(
            """
            SELECT * FROM tasks
            WHERE user_id = ? AND done = 0
            ORDER BY
                CASE WHEN due_at IS NOT NULL AND due_at < ? THEN 0 ELSE 1 END ASC,
                priority ASC,
                due_at ASC NULLS LAST,
                created_at ASC
            LIMIT 1
            """,
            (user_id, now)
        ).fetchone()


def set_priority(user_id: int, task_id: int, priority: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET priority = ? WHERE id = ? AND user_id = ?",
            (priority, task_id, user_id)
        )
        return cur.rowcount > 0


# ── Reminders ──────────────────────────────────────────────────────────────

def add_reminder(user_id: int, task_id: int, remind_at: datetime) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (task_id, user_id, remind_at) VALUES (?, ?, ?)",
            (task_id, user_id, remind_at.isoformat())
        )
        return cur.lastrowid


def get_due_reminders() -> list[sqlite3.Row]:
    """Fetch all unfired reminders whose time has passed."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT r.*, t.title, t.done FROM reminders r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.fired = 0 AND r.remind_at <= ?
            """,
            (now,)
        ).fetchall()


def mark_reminder_fired(reminder_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,))


# ── Knowledge Base ─────────────────────────────────────────────────────────

def kb_list() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, title, tags, updated_at FROM kb_articles ORDER BY updated_at DESC"
        ).fetchall()


def kb_search(query: str) -> list[sqlite3.Row]:
    q = f"%{query}%"
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, title, tags, updated_at FROM kb_articles "
            "WHERE title LIKE ? OR content LIKE ? OR tags LIKE ? "
            "ORDER BY updated_at DESC",
            (q, q, q)
        ).fetchall()


def kb_get(article_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM kb_articles WHERE id = ?", (article_id,)
        ).fetchone()


def kb_create(title: str, content: str, tags: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO kb_articles (title, content, tags) VALUES (?, ?, ?)",
            (title, content, tags)
        )
        return cur.lastrowid


def kb_update(article_id: int, title: str, content: str, tags: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE kb_articles SET title=?, content=?, tags=?, updated_at=datetime('now') "
            "WHERE id=?",
            (title, content, tags, article_id)
        )
        return cur.rowcount > 0


def kb_delete(article_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM kb_articles WHERE id=?", (article_id,))
        return cur.rowcount > 0


def kb_add_image(article_id: int, filename: str, data: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO kb_images (article_id, filename, data) VALUES (?, ?, ?)",
            (article_id, filename, data)
        )
        return cur.lastrowid


def kb_get_images(article_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, filename, data FROM kb_images WHERE article_id=?",
            (article_id,)
        ).fetchall()


def kb_delete_image(image_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM kb_images WHERE id=?", (image_id,))
        return cur.rowcount > 0


# ── Reminders ──────────────────────────────────────────────────────────────

def list_reminders(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT r.id, r.remind_at, t.title, t.id as task_id
            FROM reminders r JOIN tasks t ON t.id = r.task_id
            WHERE r.user_id = ? AND r.fired = 0 AND t.done = 0
            ORDER BY r.remind_at ASC
            """,
            (user_id,)
        ).fetchall()
