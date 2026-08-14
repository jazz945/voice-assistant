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

# Ces requêtes joignent souvent le profil à d'autres tables (matchs, croisements).
# La sérialisation doit donc filtrer sur ce jeu de champs et lui seul : sans quoi
# des colonnes techniques — dont `cell_id`, la cellule de géolocalisation —
# ressortiraient dans la réponse.
PROFILE_FIELDS = frozenset(column.strip() for column in PROFILE_COLUMNS.split(","))

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


# --- Croisements ------------------------------------------------------------


def set_crossings_enabled(conn: sqlite3.Connection, user_id: int, enabled: bool) -> None:
    conn.execute("UPDATE users SET crossings_enabled = ? WHERE id = ?", (int(enabled), user_id))


def record_ping(
    conn: sqlite3.Connection,
    user_id: int,
    cell: str,
    bucket: str,
    speed_kmh: float | None,
    heading_deg: float | None,
    ride_id: int | None,
) -> None:
    conn.execute(
        "INSERT INTO location_pings (user_id, cell_id, time_bucket, speed_kmh, heading_deg, ride_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, cell, bucket, speed_kmh, heading_deg, ride_id),
    )


def find_nearby_pings(
    conn: sqlite3.Connection, user_id: int, cells: list[str], window_seconds: int
) -> list[sqlite3.Row]:
    """Dernier ping de chaque autre motard présent dans les cellules voisines.

    Ne remonte que les comptes ayant activé les croisements et non bloqués : la
    réciprocité est une règle de la fonction, pas une option d'affichage.
    """
    placeholders = ",".join("?" for _ in cells)
    return conn.execute(
        f"""
        SELECT p.user_id, p.cell_id, p.speed_kmh, p.heading_deg, p.ride_id,
               MAX(p.created_at) AS seen_at
        FROM location_pings p
        JOIN users u ON u.id = p.user_id
        WHERE p.cell_id IN ({placeholders})
          AND p.user_id != ?
          AND p.created_at > datetime('now', ?)
          AND u.deleted_at IS NULL
          AND u.crossings_enabled = 1
          AND NOT EXISTS (
              SELECT 1 FROM blocks b
              WHERE (b.blocker_id = ? AND b.blocked_id = p.user_id)
                 OR (b.blocker_id = p.user_id AND b.blocked_id = ?)
          )
        GROUP BY p.user_id
        """,
        (*cells, user_id, f"-{window_seconds} seconds", user_id, user_id),
    ).fetchall()


def record_crossing(
    conn: sqlite3.Connection,
    user_id: int,
    other_id: int,
    cell: str,
    bucket: str,
    context: str,
    direction: str,
    ride_id: int | None,
) -> bool:
    """Enregistre un croisement. Retourne False s'il était déjà connu."""
    a, b = sorted((user_id, other_id))
    cursor = conn.execute(
        "INSERT OR IGNORE INTO crossings "
        "(user_a_id, user_b_id, cell_id, time_bucket, context, direction, ride_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (a, b, cell, bucket, context, direction, ride_id),
    )
    return cursor.rowcount > 0


def list_crossings(conn: sqlite3.Connection, user_id: int, limit: int) -> list[sqlite3.Row]:
    """Croisements groupés par personne, du plus récent au plus ancien.

    Les personnes déjà évaluées dans la découverte restent affichées ici : un
    croisement réel est une information différente d'un profil proposé.
    """
    return conn.execute(
        f"""
        SELECT c.other_id AS user_id, c.times, c.last_seen_at, c.cell_id,
               c.context, c.direction, c.ride_id, c.crossing_id,
               c.my_salut, c.their_salut, {PROFILE_COLUMNS}
        FROM (
            SELECT CASE WHEN user_a_id = :uid THEN user_b_id ELSE user_a_id END AS other_id,
                   COUNT(*) AS times,
                   MAX(created_at) AS last_seen_at,
                   MAX(id) AS crossing_id,
                   MAX(CASE WHEN user_a_id = :uid THEN salut_a ELSE salut_b END) AS my_salut,
                   MAX(CASE WHEN user_a_id = :uid THEN salut_b ELSE salut_a END) AS their_salut,
                   cell_id, context, direction, ride_id
            FROM crossings
            WHERE user_a_id = :uid OR user_b_id = :uid
            GROUP BY other_id
        ) c
        JOIN profiles p ON p.user_id = c.other_id
        JOIN users u ON u.id = c.other_id AND u.deleted_at IS NULL
        WHERE {_NOT_BLOCKED}
        ORDER BY c.last_seen_at DESC
        LIMIT :limit
        """,
        {"uid": user_id, "limit": limit},
    ).fetchall()


def get_crossing_for_user(
    conn: sqlite3.Connection, crossing_id: int, user_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM crossings WHERE id = ? AND (user_a_id = ? OR user_b_id = ?)",
        (crossing_id, user_id, user_id),
    ).fetchone()


def send_salut(conn: sqlite3.Connection, crossing_id: int, user_id: int) -> bool:
    """Enregistre le salut. Retourne True si l'autre avait déjà salué (salut rendu)."""
    crossing = conn.execute("SELECT * FROM crossings WHERE id = ?", (crossing_id,)).fetchone()
    column = "salut_a" if int(crossing["user_a_id"]) == user_id else "salut_b"
    other_column = "salut_b" if column == "salut_a" else "salut_a"
    conn.execute(f"UPDATE crossings SET {column} = 1 WHERE id = ?", (crossing_id,))
    return bool(crossing[other_column])


def purge_crossing_data(conn: sqlite3.Connection, user_id: int) -> int:
    """Efface positions et croisements d'un utilisateur, à sa demande."""
    pings = conn.execute("DELETE FROM location_pings WHERE user_id = ?", (user_id,)).rowcount
    crossings = conn.execute(
        "DELETE FROM crossings WHERE user_a_id = ? OR user_b_id = ?", (user_id, user_id)
    ).rowcount
    return pings + crossings


def purge_old_pings(conn: sqlite3.Connection, retention_hours: int) -> int:
    """Rétention courte : les positions ne survivent pas à la journée."""
    return conn.execute(
        "DELETE FROM location_pings WHERE created_at < datetime('now', ?)",
        (f"-{retention_hours} hours",),
    ).rowcount


# --- Balades ----------------------------------------------------------------

RIDE_COLUMNS = (
    "r.id, r.organiser_id, r.title, r.description, r.start_city, r.start_latitude, "
    "r.start_longitude, r.start_at, r.distance_km, r.pace, r.route_type, "
    "r.bike_categories, r.max_participants, r.visibility, r.status, r.created_at"
)


def create_ride(conn: sqlite3.Connection, organiser_id: int, ride: dict[str, Any]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO rides (
            organiser_id, title, description, start_city, start_latitude, start_longitude,
            start_at, distance_km, pace, route_type, bike_categories, max_participants, visibility
        ) VALUES (
            :organiser_id, :title, :description, :start_city, :start_latitude, :start_longitude,
            :start_at, :distance_km, :pace, :route_type, :bike_categories, :max_participants,
            :visibility
        )
        """,
        {"organiser_id": organiser_id, **ride},
    )
    ride_id = int(cursor.lastrowid)
    # L'organisateur est participant d'office.
    conn.execute(
        "INSERT INTO ride_participants (ride_id, user_id, status) VALUES (?, ?, 'accepte')",
        (ride_id, organiser_id),
    )
    return ride_id


def get_ride(conn: sqlite3.Connection, ride_id: int) -> sqlite3.Row | None:
    return conn.execute(f"SELECT {RIDE_COLUMNS} FROM rides r WHERE r.id = ?", (ride_id,)).fetchone()


def list_visible_rides(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """Balades à venir que cet utilisateur a le droit de voir.

    L'autorisation `matchs` restreint la visibilité aux personnes déjà matchées
    avec l'organisateur ; `public` et `sur-demande` sont visibles de tous. Dans
    tous les cas, un blocage masque la balade des deux côtés.
    """
    return conn.execute(
        f"""
        SELECT {RIDE_COLUMNS},
               (SELECT COUNT(*) FROM ride_participants rp
                 WHERE rp.ride_id = r.id AND rp.status = 'accepte') AS accepted_count,
               (SELECT status FROM ride_participants rp
                 WHERE rp.ride_id = r.id AND rp.user_id = :uid) AS my_status,
               po.display_name AS organiser_name
        FROM rides r
        JOIN users u ON u.id = r.organiser_id AND u.deleted_at IS NULL
        LEFT JOIN profiles po ON po.user_id = r.organiser_id
        WHERE r.status = 'ouverte'
          AND r.start_at > datetime('now')
          AND NOT EXISTS (
              SELECT 1 FROM blocks b
              WHERE (b.blocker_id = :uid AND b.blocked_id = r.organiser_id)
                 OR (b.blocker_id = r.organiser_id AND b.blocked_id = :uid)
          )
          AND (
              r.visibility IN ('public', 'sur-demande')
              OR r.organiser_id = :uid
              OR EXISTS (
                  SELECT 1 FROM matches m
                  WHERE (m.user_a_id = :uid AND m.user_b_id = r.organiser_id)
                     OR (m.user_b_id = :uid AND m.user_a_id = r.organiser_id)
              )
          )
        ORDER BY r.start_at
        """,
        {"uid": user_id},
    ).fetchall()


def list_ride_participants(conn: sqlite3.Connection, ride_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT rp.user_id, rp.status, rp.created_at, p.display_name, p.bike_brand,
               p.bike_model, p.bike_category, p.pace, p.city
        FROM ride_participants rp
        LEFT JOIN profiles p ON p.user_id = rp.user_id
        JOIN users u ON u.id = rp.user_id AND u.deleted_at IS NULL
        WHERE rp.ride_id = ?
        ORDER BY rp.created_at
        """,
        (ride_id,),
    ).fetchall()


def get_participation(
    conn: sqlite3.Connection, ride_id: int, user_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ride_participants WHERE ride_id = ? AND user_id = ?", (ride_id, user_id)
    ).fetchone()


def count_accepted_participants(conn: sqlite3.Connection, ride_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ride_participants WHERE ride_id = ? AND status = 'accepte'",
        (ride_id,),
    ).fetchone()
    return int(row["n"])


def join_ride(conn: sqlite3.Connection, ride_id: int, user_id: int, status: str) -> None:
    conn.execute(
        "INSERT INTO ride_participants (ride_id, user_id, status) VALUES (?, ?, ?) "
        "ON CONFLICT(ride_id, user_id) DO UPDATE SET status = excluded.status",
        (ride_id, user_id, status),
    )


def leave_ride(conn: sqlite3.Connection, ride_id: int, user_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM ride_participants WHERE ride_id = ? AND user_id = ?", (ride_id, user_id)
    )
    return cursor.rowcount > 0


def set_participation_status(
    conn: sqlite3.Connection, ride_id: int, user_id: int, status: str
) -> bool:
    cursor = conn.execute(
        "UPDATE ride_participants SET status = ? WHERE ride_id = ? AND user_id = ?",
        (status, ride_id, user_id),
    )
    return cursor.rowcount > 0


def cancel_ride(conn: sqlite3.Connection, ride_id: int, organiser_id: int) -> bool:
    cursor = conn.execute(
        "UPDATE rides SET status = 'annulee' WHERE id = ? AND organiser_id = ? AND status = 'ouverte'",
        (ride_id, organiser_id),
    )
    return cursor.rowcount > 0


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
        "croisements": rows_to_dicts(
            conn.execute(
                "SELECT cell_id, time_bucket, context, direction, created_at FROM crossings "
                "WHERE user_a_id = ? OR user_b_id = ?",
                (user_id, user_id),
            ).fetchall()
        ),
        "balades_organisees": rows_to_dicts(
            conn.execute(
                "SELECT id, title, start_city, start_at, visibility, status FROM rides "
                "WHERE organiser_id = ?",
                (user_id,),
            ).fetchall()
        ),
        "participations": rows_to_dicts(
            conn.execute(
                "SELECT ride_id, status, created_at FROM ride_participants WHERE user_id = ?",
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


# --- Abonnement et boosts ---------------------------------------------------


def get_subscription(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()


def upsert_subscription(
    conn: sqlite3.Connection,
    user_id: int,
    tier: str,
    expires_at: str | None,
    provider: str,
    external_id: str | None = None,
    customer_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO subscriptions
            (user_id, tier, expires_at, provider, external_id, customer_id, cancelled_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(user_id) DO UPDATE SET
            tier = excluded.tier,
            expires_at = excluded.expires_at,
            provider = excluded.provider,
            external_id = excluded.external_id,
            -- Un renouvellement sans identifiant client ne doit pas effacer
            -- celui qu'on a mémorisé au premier paiement : c'est lui qui permet
            -- de retrouver le compte sur les évènements suivants.
            customer_id = COALESCE(excluded.customer_id, subscriptions.customer_id),
            cancelled_at = NULL
        """,
        (user_id, tier, expires_at, provider, external_id, customer_id),
    )


def find_subscription_by_customer(
    conn: sqlite3.Connection, customer_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM subscriptions WHERE customer_id = ?", (customer_id,)
    ).fetchone()


def cancel_subscription(conn: sqlite3.Connection, user_id: int) -> bool:
    """Marque l'abonnement comme résilié.

    L'accès est conservé jusqu'à l'échéance déjà payée : couper immédiatement
    reviendrait à garder l'argent d'une période non servie.
    """
    cursor = conn.execute(
        "UPDATE subscriptions SET cancelled_at = datetime('now') "
        "WHERE user_id = ? AND cancelled_at IS NULL",
        (user_id,),
    )
    return cursor.rowcount > 0


def count_likes_today(conn: sqlite3.Connection, user_id: int) -> int:
    """Likes émis depuis minuit UTC.

    Le quota se remet à zéro sur une journée calendaire et non sur une fenêtre
    glissante : « ça repart à minuit » est compréhensible, « dans 7 h 12 » ne
    l'est pas. Les `pass` ne sont pas comptés — seuls les likes sont rationnés.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM swipes "
        "WHERE from_user_id = ? AND direction = 'like' AND created_at >= date('now')",
        (user_id,),
    ).fetchone()
    return int(row["n"])


def count_boosts_this_month(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM boosts "
        "WHERE user_id = ? AND started_at >= date('now', 'start of month')",
        (user_id,),
    ).fetchone()
    return int(row["n"])


def start_boost(conn: sqlite3.Connection, user_id: int, minutes: int) -> str:
    row = conn.execute(
        "INSERT INTO boosts (user_id, expires_at) "
        "VALUES (?, datetime('now', ?)) RETURNING expires_at",
        (user_id, f"+{minutes} minutes"),
    ).fetchone()
    return str(row["expires_at"])


def active_boost(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM boosts WHERE user_id = ? AND expires_at > datetime('now') "
        "ORDER BY expires_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def boosted_user_ids(conn: sqlite3.Connection) -> set[int]:
    """Comptes actuellement boostés — une requête plutôt qu'une par candidat."""
    return {
        int(row["user_id"])
        for row in conn.execute(
            "SELECT DISTINCT user_id FROM boosts WHERE expires_at > datetime('now')"
        ).fetchall()
    }


def list_received_likes(conn: sqlite3.Connection, user_id: int, limit: int) -> list[sqlite3.Row]:
    """Profils ayant liké cet utilisateur sans réponse de sa part.

    Ceux qu'il a déjà évalués sont exclus : ils sont soit devenus des matchs,
    soit délibérément passés.
    """
    return conn.execute(
        f"""
        SELECT s.created_at AS liked_at, {PROFILE_COLUMNS}
        FROM swipes s
        JOIN profiles p ON p.user_id = s.from_user_id
        JOIN users u ON u.id = s.from_user_id AND u.deleted_at IS NULL
        WHERE s.to_user_id = :uid
          AND s.direction = 'like'
          AND NOT EXISTS (
              SELECT 1 FROM swipes mine
              WHERE mine.from_user_id = :uid AND mine.to_user_id = s.from_user_id
          )
          AND {_NOT_BLOCKED}
        ORDER BY s.created_at DESC
        LIMIT :limit
        """,
        {"uid": user_id, "limit": limit},
    ).fetchall()


def last_swipe(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM swipes WHERE from_user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
    ).fetchone()


def delete_swipe(conn: sqlite3.Connection, swipe_id: int, user_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM swipes WHERE id = ? AND from_user_id = ?", (swipe_id, user_id)
    )
    return cursor.rowcount > 0


def record_payment_event(
    conn: sqlite3.Connection, provider: str, event_id: str, kind: str, user_id: int | None
) -> bool:
    """Journalise un évènement de paiement. False s'il avait déjà été traité.

    C'est la garantie d'idempotence : les prestataires rejouent leurs webhooks
    dès qu'ils doutent d'une réception, et sans ce verrou un rejeu prolongerait
    l'abonnement une seconde fois.
    """
    cursor = conn.execute(
        "INSERT OR IGNORE INTO payment_events (provider, event_id, kind, user_id) "
        "VALUES (?, ?, ?, ?)",
        (provider, event_id, kind, user_id),
    )
    return cursor.rowcount > 0


def get_match_for_user_pair(
    conn: sqlite3.Connection, user_id: int, other_id: int
) -> sqlite3.Row | None:
    a, b = sorted((user_id, other_id))
    return conn.execute(
        "SELECT * FROM matches WHERE user_a_id = ? AND user_b_id = ?", (a, b)
    ).fetchone()
