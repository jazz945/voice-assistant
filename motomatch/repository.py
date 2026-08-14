"""Accès aux données : requêtes SQL isolées du reste de l'application.

Toutes les requêtes sont paramétrées. Les jointures de découverte et de
messagerie excluent systématiquement les comptes supprimés et les utilisateurs
bloqués — le filtrage de sécurité vit ici, pas dans la couche HTTP, pour qu'un
nouvel appel ne puisse pas l'oublier.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .schemas import ProfileInput

PROFILE_COLUMNS = (
    "user_id, display_name, birth_year, gender, seeking, city, latitude, longitude, bio, "
    "bike_brand, bike_model, bike_year, engine_cc, bike_category, riding_styles, pace, "
    "experience_years, annual_km, max_travel_km, has_passenger_seat, photo_url, updated_at"
)

# Condition réutilisée : ni l'un ni l'autre n'a bloqué son vis-à-vis.
_NOT_BLOCKED = """
    NOT EXISTS (
        SELECT 1 FROM blocks b
        WHERE (b.blocker_id = :uid AND b.blocked_id = p.user_id)
           OR (b.blocker_id = p.user_id AND b.blocked_id = :uid)
    )
"""


# --- Utilisateurs -----------------------------------------------------------


def create_user(conn: sqlite3.Connection, email: str, password_hash: str) -> int:
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, age_attested_at) "
        "VALUES (?, ?, datetime('now'))",
        (email.lower(), password_hash),
    )
    return int(cursor.lastrowid)


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE email = ? AND deleted_at IS NULL", (email.lower(),)
    ).fetchone()


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,)
    ).fetchone()


def update_password_hash(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    conn.execute(
        "UPDATE users SET password_hash = ?, password_changed_at = datetime('now') WHERE id = ?",
        (password_hash, user_id),
    )


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Suppression définitive du compte et de toutes ses données liées.

    Exigée par l'App Store (règle 5.1.1(v)) et par le RGPD (droit à l'effacement).
    Les `ON DELETE CASCADE` du schéma emportent profil, sessions, swipes, matchs,
    messages, blocages et signalements ; le journal d'audit conserve des lignes
    anonymisées (`user_id` passe à NULL) pour la traçabilité de sécurité.
    """
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


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
    """Profils encore jamais évalués, hors comptes supprimés et blocages."""
    return conn.execute(
        f"""
        SELECT {PROFILE_COLUMNS} FROM profiles p
        JOIN users u ON u.id = p.user_id AND u.deleted_at IS NULL
        WHERE p.user_id != :uid
          AND p.user_id NOT IN (SELECT to_user_id FROM swipes WHERE from_user_id = :uid)
          AND {_NOT_BLOCKED}
        """,
        {"uid": viewer_id},
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
    conn.execute("INSERT OR IGNORE INTO matches (user_a_id, user_b_id) VALUES (?, ?)", (a, b))
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
        JOIN users u ON u.id = p.user_id AND u.deleted_at IS NULL
        WHERE (m.user_a_id = :uid OR m.user_b_id = :uid)
          AND {_NOT_BLOCKED}
        ORDER BY COALESCE(last_message_at, m.created_at) DESC
        """,
        {"uid": user_id},
    ).fetchall()


def get_match_for_user(conn: sqlite3.Connection, match_id: int, user_id: int) -> sqlite3.Row | None:
    """Match accessible : l'utilisateur en fait partie et aucun blocage ne s'applique."""
    return conn.execute(
        """
        SELECT m.* FROM matches m
        WHERE m.id = :match_id
          AND (m.user_a_id = :uid OR m.user_b_id = :uid)
          AND NOT EXISTS (
              SELECT 1 FROM blocks b
              WHERE (b.blocker_id = m.user_a_id AND b.blocked_id = m.user_b_id)
                 OR (b.blocker_id = m.user_b_id AND b.blocked_id = m.user_a_id)
          )
          AND NOT EXISTS (
              SELECT 1 FROM users u
              WHERE u.id IN (m.user_a_id, m.user_b_id) AND u.deleted_at IS NOT NULL
          )
        """,
        {"match_id": match_id, "uid": user_id},
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


# --- Sécurité des personnes : blocage et signalement ------------------------


def block_user(conn: sqlite3.Connection, blocker_id: int, blocked_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)",
        (blocker_id, blocked_id),
    )


def unblock_user(conn: sqlite3.Connection, blocker_id: int, blocked_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM blocks WHERE blocker_id = ? AND blocked_id = ?", (blocker_id, blocked_id)
    )
    return cursor.rowcount > 0


def list_blocks(conn: sqlite3.Connection, blocker_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT b.blocked_id AS user_id, b.created_at, p.display_name
        FROM blocks b
        LEFT JOIN profiles p ON p.user_id = b.blocked_id
        WHERE b.blocker_id = ?
        ORDER BY b.created_at DESC
        """,
        (blocker_id,),
    ).fetchall()


def is_blocked_either_way(conn: sqlite3.Connection, user_id: int, other_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM blocks WHERE (blocker_id = ? AND blocked_id = ?) "
        "OR (blocker_id = ? AND blocked_id = ?)",
        (user_id, other_id, other_id, user_id),
    ).fetchone()
    return row is not None


def create_report(
    conn: sqlite3.Connection, reporter_id: int, reported_id: int, reason: str, details: str
) -> int:
    cursor = conn.execute(
        "INSERT INTO reports (reporter_id, reported_id, reason, details) VALUES (?, ?, ?, ?)",
        (reporter_id, reported_id, reason, details),
    )
    return int(cursor.lastrowid)


def count_pending_reports(conn: sqlite3.Connection, reported_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM reports WHERE reported_id = ? AND status = 'pending'",
        (reported_id,),
    ).fetchone()
    return int(row["n"])


# --- Export des données (RGPD article 20) -----------------------------------


def export_user_data(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """Toutes les données personnelles du compte, dans un format réutilisable."""
    user = conn.execute(
        "SELECT id, email, created_at, password_changed_at, age_attested_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    profile = get_profile(conn, user_id)
    return {
        "compte": dict(user) if user else None,
        "profil": dict(profile) if profile else None,
        "swipes": rows_to_dicts(
            conn.execute(
                "SELECT to_user_id, direction, created_at FROM swipes WHERE from_user_id = ?",
                (user_id,),
            ).fetchall()
        ),
        "matchs": rows_to_dicts(
            conn.execute(
                "SELECT id, user_a_id, user_b_id, created_at FROM matches "
                "WHERE user_a_id = ? OR user_b_id = ?",
                (user_id, user_id),
            ).fetchall()
        ),
        "messages_envoyes": rows_to_dicts(
            conn.execute(
                "SELECT match_id, body, created_at FROM messages WHERE sender_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        ),
        "blocages": rows_to_dicts(list_blocks(conn, user_id)),
        "signalements_emis": rows_to_dicts(
            conn.execute(
                "SELECT reported_id, reason, details, status, created_at FROM reports "
                "WHERE reporter_id = ?",
                (user_id,),
            ).fetchall()
        ),
        "sessions": rows_to_dicts(
            conn.execute(
                "SELECT device_label, created_at, last_used_at, revoked_at FROM sessions "
                "WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        ),
        "journal_securite": rows_to_dicts(
            conn.execute(
                "SELECT event, detail, created_at FROM audit_log WHERE user_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        ),
    }


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
