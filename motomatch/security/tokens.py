"""Jetons d'accès et de rafraîchissement.

Choix de conception : des jetons **opaques** aléatoires plutôt que des JWT.
Un JWT auto-porteur ne peut pas être révoqué avant son expiration ; ici chaque
jeton correspond à une ligne en base, donc « déconnecter cet appareil » ou
« déconnecter partout » prend effet immédiatement — ce qu'une application de
rencontre doit pouvoir garantir (téléphone volé, compte compromis).

Deux protections structurent le modèle :

1. **Aucun jeton n'est stocké en clair.** La base ne contient que le SHA-256 du
   jeton. Une fuite de la base ne permet donc pas d'usurper une session.
2. **Rotation avec détection de rejeu.** Chaque rafraîchissement invalide le
   jeton précédent. Si un jeton déjà consommé est représenté, c'est qu'il a été
   volé : toute la famille de sessions est révoquée.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass

TOKEN_BYTES = 32


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_in: int
    refresh_expires_in: int
    session_id: int


class RefreshTokenReuse(Exception):
    """Un jeton de rafraîchissement déjà consommé a été représenté."""


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def fingerprint(token: str) -> str:
    """Empreinte stockée en base.

    SHA-256 nu suffit ici, contrairement aux mots de passe : un jeton fait 256
    bits d'entropie aléatoire, il n'est pas attaquable par dictionnaire.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def issue_pair(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    access_ttl: int,
    refresh_ttl: int,
    device_label: str = "",
    family_id: str | None = None,
) -> TokenPair:
    """Crée une session et son couple de jetons.

    `family_id` relie les rotations successives d'une même connexion initiale :
    c'est l'unité que l'on révoque en bloc si un rejeu est détecté.
    """
    access_token, refresh_token = new_token(), new_token()
    cursor = conn.execute(
        """
        INSERT INTO sessions (
            user_id, family_id, access_token_hash, refresh_token_hash,
            device_label, access_expires_at, refresh_expires_at
        )
        VALUES (
            :user_id, :family_id, :access_hash, :refresh_hash,
            :device, datetime('now', :access_ttl), datetime('now', :refresh_ttl)
        )
        """,
        {
            "user_id": user_id,
            "family_id": family_id or secrets.token_urlsafe(16),
            "access_hash": fingerprint(access_token),
            "refresh_hash": fingerprint(refresh_token),
            "device": device_label[:200],
            "access_ttl": f"+{access_ttl} seconds",
            "refresh_ttl": f"+{refresh_ttl} seconds",
        },
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_in=access_ttl,
        refresh_expires_in=refresh_ttl,
        session_id=int(cursor.lastrowid),
    )


def resolve_access_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """Retourne l'utilisateur porteur d'un jeton d'accès valide, sinon None."""
    row = conn.execute(
        """
        SELECT u.*, s.id AS session_id
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.access_token_hash = ?
          AND s.revoked_at IS NULL
          AND s.access_expires_at > datetime('now')
          AND u.deleted_at IS NULL
        """,
        (fingerprint(token),),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE sessions SET last_used_at = datetime('now') WHERE id = ?",
            (row["session_id"],),
        )
    return row


def rotate_refresh_token(
    conn: sqlite3.Connection,
    token: str,
    *,
    access_ttl: int,
    refresh_ttl: int,
    device_label: str = "",
) -> TokenPair:
    """Échange un jeton de rafraîchissement contre un couple neuf.

    Lève `RefreshTokenReuse` si le jeton a déjà servi — signe d'un vol — après
    avoir révoqué toutes les sessions de la même famille.
    """
    token_hash = fingerprint(token)
    session = conn.execute(
        "SELECT * FROM sessions WHERE refresh_token_hash = ?", (token_hash,)
    ).fetchone()

    if session is None:
        raise LookupError("jeton de rafraîchissement inconnu")

    already_rotated = session["rotated_at"] is not None
    if already_rotated or session["revoked_at"] is not None:
        # Rejeu : on coupe toute la famille, y compris la session légitime.
        conn.execute(
            "UPDATE sessions SET revoked_at = datetime('now') "
            "WHERE family_id = ? AND revoked_at IS NULL",
            (session["family_id"],),
        )
        raise RefreshTokenReuse(str(session["family_id"]))

    expired = conn.execute(
        "SELECT 1 FROM sessions WHERE id = ? AND refresh_expires_at > datetime('now')",
        (session["id"],),
    ).fetchone()
    if expired is None:
        raise LookupError("jeton de rafraîchissement expiré")

    conn.execute(
        "UPDATE sessions SET rotated_at = datetime('now'), revoked_at = datetime('now') "
        "WHERE id = ?",
        (session["id"],),
    )
    return issue_pair(
        conn,
        int(session["user_id"]),
        access_ttl=access_ttl,
        refresh_ttl=refresh_ttl,
        device_label=device_label or str(session["device_label"] or ""),
        family_id=str(session["family_id"]),
    )


def revoke_session(conn: sqlite3.Connection, session_id: int, user_id: int) -> bool:
    cursor = conn.execute(
        "UPDATE sessions SET revoked_at = datetime('now') "
        "WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (session_id, user_id),
    )
    return cursor.rowcount > 0


def revoke_all_sessions(conn: sqlite3.Connection, user_id: int, *, keep_id: int | None = None) -> int:
    cursor = conn.execute(
        "UPDATE sessions SET revoked_at = datetime('now') "
        "WHERE user_id = ? AND revoked_at IS NULL AND id IS NOT ?",
        (user_id, keep_id),
    )
    return cursor.rowcount


def list_sessions(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """Sessions actives — alimente un écran « appareils connectés »."""
    return conn.execute(
        """
        SELECT id, device_label, created_at, last_used_at, access_expires_at
        FROM sessions
        WHERE user_id = ? AND revoked_at IS NULL AND refresh_expires_at > datetime('now')
        ORDER BY COALESCE(last_used_at, created_at) DESC
        """,
        (user_id,),
    ).fetchall()


def purge_expired(conn: sqlite3.Connection) -> int:
    """Supprime les sessions dont même le jeton de rafraîchissement a expiré."""
    cursor = conn.execute(
        "DELETE FROM sessions WHERE refresh_expires_at < datetime('now', '-30 days')"
    )
    return cursor.rowcount
