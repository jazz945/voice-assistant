"""Accès aux données : requêtes SQL isolées du reste de l'application."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .schemas import ProfileInput

PROFILE_COLUMNS = (
    "user_id, display_name, birth_year, gender, seeking, city, latitude, longitude, bio, "
    "bike_brand, bike_model, bike_year, engine_cc, bike_category, riding_styles, pace, "
    "experience_years, annual_km, max_travel_km, has_passenger_seat, photo_url, updated_at"
)


# --- Utilisateurs -----------------------------------------------------------


def create_user(conn: sqlite3.Connection, email: str, password_hash: str) -> int:
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        (email.lower(), password_hash),
    )
    return int(cursor.lastrowid)


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


# --- Sessions ---------------------------------------------------------------


def create_session(conn: sqlite3.Connection, token: str, user_id: int) -> None:
    conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))


def get_session_user(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
        (token,),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# --- Profils ----------------------------------------------------------------


def upsert_profile(conn: sqlite3.Connection, user_id: int, profile: ProfileInput) -> None:
    values = {
        "user_id": user_id,
        **profile.model_dump(),
        "riding_styles": ",".join(profile.riding_styles),
        "has_passenger_seat": int(profile.has_passenger_seat),
    }
    columns = list(values)
    placeholders = ", ".join(f":{column}" for column in columns)
    assignments = ", ".join(f"{column} = :{column}" for column in columns if column != "user_id")
    conn.execute(
        f"INSERT INTO profiles ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(user_id) DO UPDATE SET {assignments}, updated_at = datetime('now')",
        values,
    )


def get_profile(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {PROFILE_COLUMNS} FROM profiles WHERE user_id = ?", (user_id,)
    ).fetchone()


def list_candidate_profiles(conn: sqlite3.Connection, viewer_id: int) -> list[sqlite3.Row]:
    """Profils encore jamais évalués par `viewer_id`."""
    return conn.execute(
        f"""
        SELECT {PROFILE_COLUMNS} FROM profiles
        WHERE user_id != ?
          AND user_id NOT IN (SELECT to_user_id FROM swipes WHERE from_user_id = ?)
        """,
        (viewer_id, viewer_id),
    ).fetchall()


# --- Swipes et matchs -------------------------------------------------------


def record_swipe(
    conn: sqlite3.Connection, from_user_id: int, to_user_id: int, direction: str
) -> None:
    conn.execute(
        "INSERT INTO swipes (from_user_id, to_user_id, direction) VALUES (?, ?, ?) "
        "ON CONFLICT(from_user_id, to_user_id) DO UPDATE SET direction = excluded.direction",
        (from_user_id, to_user_id, direction),
    )


def has_liked(conn: sqlite3.Connection, from_user_id: int, to_user_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM swipes WHERE from_user_id = ? AND to_user_id = ? AND direction = 'like'",
        (from_user_id, to_user_id),
    ).fetchone()
    return row is not None


def create_match(conn: sqlite3.Connection, user_id: int, other_id: int) -> int:
    a, b = sorted((user_id, other_id))
    conn.execute(
        "INSERT OR IGNORE INTO matches (user_a_id, user_b_id) VALUES (?, ?)",
        (a, b),
    )
    row = conn.execute(
        "SELECT id FROM matches WHERE user_a_id = ? AND user_b_id = ?", (a, b)
    ).fetchone()
    return int(row["id"])


def list_matches(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT m.id AS match_id, m.created_at AS matched_at, {PROFILE_COLUMNS},
               (SELECT body FROM messages WHERE match_id = m.id ORDER BY id DESC LIMIT 1)
                   AS last_message,
               (SELECT created_at FROM messages WHERE match_id = m.id ORDER BY id DESC LIMIT 1)
                   AS last_message_at
        FROM matches m
        JOIN profiles p
          ON p.user_id = CASE WHEN m.user_a_id = :uid THEN m.user_b_id ELSE m.user_a_id END
        WHERE m.user_a_id = :uid OR m.user_b_id = :uid
        ORDER BY COALESCE(last_message_at, m.created_at) DESC
        """,
        {"uid": user_id},
    ).fetchall()


def get_match_for_user(conn: sqlite3.Connection, match_id: int, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM matches WHERE id = ? AND (user_a_id = ? OR user_b_id = ?)",
        (match_id, user_id, user_id),
    ).fetchone()


# --- Messages ---------------------------------------------------------------


def add_message(conn: sqlite3.Connection, match_id: int, sender_id: int, body: str) -> int:
    cursor = conn.execute(
        "INSERT INTO messages (match_id, sender_id, body) VALUES (?, ?, ?)",
        (match_id, sender_id, body),
    )
    return int(cursor.lastrowid)


def list_messages(conn: sqlite3.Connection, match_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, match_id, sender_id, body, created_at FROM messages "
        "WHERE match_id = ? ORDER BY id",
        (match_id,),
    ).fetchall()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
