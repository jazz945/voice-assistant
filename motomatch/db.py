"""Couche de persistance SQLite pour MotoMatch.

On reste sur `sqlite3` de la bibliothèque standard : l'application tient dans un
seul fichier de base, ce qui la rend triviale à lancer et à tester. Le passage à
PostgreSQL ne toucherait que `db.py` et `repository.py`.

Toutes les requêtes de l'application sont paramétrées (jamais de concaténation
de chaînes), et les clés étrangères sont activées à chaque connexion pour que
la suppression d'un compte fasse réellement disparaître ses données liées.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    email               TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    password_changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    age_attested_at     TEXT,
    deleted_at          TEXT
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

-- Une ligne par couple de jetons. `family_id` relie les rotations successives
-- d'une même connexion : c'est l'unité révoquée si un rejeu est détecté.
CREATE TABLE IF NOT EXISTS sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id          TEXT    NOT NULL,
    access_token_hash  TEXT    NOT NULL UNIQUE,
    refresh_token_hash TEXT    NOT NULL UNIQUE,
    device_label       TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at       TEXT,
    rotated_at         TEXT,
    revoked_at         TEXT,
    access_expires_at  TEXT    NOT NULL,
    refresh_expires_at TEXT    NOT NULL
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

-- Blocage : exigé par l'App Store (règle 1.2) pour toute application à contenu
-- entre utilisateurs. L'effet est bidirectionnel côté visibilité.
CREATE TABLE IF NOT EXISTS blocks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    blocker_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (blocker_id, blocked_id)
);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reported_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reason      TEXT    NOT NULL,
    details     TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'reviewed', 'dismissed')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Journal des évènements de sécurité. Aucune donnée directement identifiante :
-- les adresses IP y sont stockées hachées.
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    event      TEXT    NOT NULL,
    ip_hash    TEXT    NOT NULL DEFAULT '',
    detail     TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rate_limit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket     TEXT NOT NULL,
    key        TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_swipes_from ON swipes(from_user_id);
CREATE INDEX IF NOT EXISTS idx_messages_match ON messages(match_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_sessions_family ON sessions(family_id);
CREATE INDEX IF NOT EXISTS idx_blocks_blocker ON blocks(blocker_id);
CREATE INDEX IF NOT EXISTS idx_blocks_blocked ON blocks(blocked_id);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rate_limit ON rate_limit_events(bucket, key, created_at);
"""


def database_path() -> Path:
    return Path(get_settings().db)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(database_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Sans ce PRAGMA, SQLite ignore silencieusement les ON DELETE CASCADE.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
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
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript(SCHEMA)
    # La base contient des données personnelles : lisible par son seul
    # propriétaire, jamais par les autres comptes de la machine.
    try:
        path.chmod(0o600)
    except OSError:
        pass


def reset_db() -> None:
    """Supprime puis recrée la base — utilisé par les tests et le seed."""
    path = database_path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    init_db()
