"""SQLite database for user and knowledge base management."""

import sqlite3
import threading
from pathlib import Path

_db_lock = threading.Lock()


def get_db_path(base_dir: str = "/app/kb") -> Path:
    """Get the database file path."""
    return Path(base_dir) / "users.db"


def init_db(base_dir: str = "/app/kb") -> None:
    """Initialize the database with required tables."""
    db_path = get_db_path(base_dir)
    with _db_lock:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    display_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    domain TEXT DEFAULT '农业',
                    kb_dir_name TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    UNIQUE(user_id, name)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kb_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (kb_id) REFERENCES knowledge_bases(id),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """)
            conn.commit()
        finally:
            conn.close()


def get_connection(base_dir: str = "/app/kb") -> sqlite3.Connection:
    """Get a database connection with WAL mode and a busy timeout."""
    db_path = get_db_path(base_dir)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
