"""Couche de persistance SQLite pour MotoMatch.

On reste volontairement sur `sqlite3` de la bibliothèque standard : l'application
tient dans un seul fichier de base, ce qui la rend triviale à lancer et à tester.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "motomatch.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profiles (
    user_id            INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    display_name       TEXT    NOT NULL,
    birth_year         INTEGER NOT NULL,
    gender             TEXT    NOT NULL,
    seeking            TEXT    NOT NULL,
    city               TEXT    NOT NULL,
    latitude           REAL    NOT NULL,
    longitude          REAL    NOT NULL,
    bio                TEXT    NOT NULL DEFAULT '',
    bike_brand         TEXT    NOT NULL,
    bike_model         TEXT    NOT NULL,
    bike_year          INTEGER,
    engine_cc          INTEGER NOT NULL,
    bike_category      TEXT    NOT NULL,
    riding_styles      TEXT    NOT NULL DEFAULT '',
    pace               TEXT    NOT NULL,
    experience_years   INTEGER NOT NULL DEFAULT 0,
    annual_km          INTEGER NOT NULL DEFAULT 0,
    max_travel_km      INTEGER NOT NULL DEFAULT 100,
    has_passenger_seat INTEGER NOT NULL DEFAULT 1,
    photo_url          TEXT    NOT NULL DEFAULT '',
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS swipes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    to_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    direction    TEXT    NOT NULL CHECK (direction IN ('like', 'pass')),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (from_user_id, to_user_id)
);

-- user_a_id < user_b_id est garanti à l'insertion : une paire = une seule ligne.
CREATE TABLE IF NOT EXISTS matches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_b_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_a_id, user_b_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id   INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    sender_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_swipes_from ON swipes(from_user_id);
CREATE INDEX IF NOT EXISTS idx_messages_match ON messages(match_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


def database_path() -> Path:
    """Chemin du fichier SQLite, surchargeable via `MOTOMATCH_DB`."""
    return Path(os.environ.get("MOTOMATCH_DB", DEFAULT_DB_PATH))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(database_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Connexion transactionnelle : commit si tout va bien, rollback sinon."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)


def reset_db() -> None:
    """Supprime puis recrée la base — utilisé par les tests et le seed."""
    path = database_path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    init_db()
