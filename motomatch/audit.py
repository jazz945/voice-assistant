"""Journal des évènements de sécurité.

Objectif : pouvoir répondre à « ce compte a-t-il été compromis ? » sans
transformer le journal lui-même en base de données de traçage. Les adresses IP
n'y sont donc écrites que hachées avec le secret serveur (HMAC) : on peut
comparer deux évènements entre eux, on ne peut pas relire l'adresse.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3

from .config import get_settings

# Évènements journalisés, nommés une fois pour éviter les fautes de frappe.
LOGIN_SUCCESS = "login.success"
LOGIN_FAILURE = "login.failure"
LOGIN_LOCKED = "login.locked"
REGISTER = "account.register"
PASSWORD_CHANGED = "account.password_changed"
ACCOUNT_DELETED = "account.deleted"
DATA_EXPORTED = "account.data_exported"
TOKEN_REFRESHED = "session.refreshed"
TOKEN_REUSE_DETECTED = "session.refresh_reuse_detected"
SESSION_REVOKED = "session.revoked"
SESSIONS_REVOKED_ALL = "session.revoked_all"
USER_BLOCKED = "safety.blocked"
USER_UNBLOCKED = "safety.unblocked"
USER_REPORTED = "safety.reported"
CROSSINGS_TOGGLED = "crossings.toggled"
CROSSINGS_PURGED = "crossings.purged"
RATE_LIMITED = "security.rate_limited"


def hash_ip(ip: str | None) -> str:
    """HMAC de l'adresse IP : comparable, non réversible."""
    if not ip:
        return ""
    secret = get_settings().secret_key.encode()
    return hmac.new(secret, ip.encode(), hashlib.sha256).hexdigest()[:32]


def record(
    conn: sqlite3.Connection,
    event: str,
    *,
    user_id: int | None = None,
    ip: str | None = None,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO audit_log (user_id, event, ip_hash, detail) VALUES (?, ?, ?, ?)",
        (user_id, event, hash_ip(ip), detail[:500]),
    )


def recent_for_user(conn: sqlite3.Connection, user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT event, detail, created_at FROM audit_log "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
