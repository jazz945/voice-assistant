"""Limitation de débit et verrouillage de compte.

Le compteur vit en base plutôt qu'en mémoire : il survit à un redémarrage, et
un attaquant ne peut pas remettre les compteurs à zéro en faisant tomber le
processus. Pour un déploiement multi-processus, remplacer `_count_recent` par
un compteur Redis est le seul changement nécessaire.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int = 0


def record_attempt(conn: sqlite3.Connection, bucket: str, key: str) -> None:
    """Journalise une tentative dans un seau (`login`, `register`, `message`…)."""
    conn.execute(
        "INSERT INTO rate_limit_events (bucket, key) VALUES (?, ?)",
        (bucket, key.lower()[:200]),
    )


def _count_recent(conn: sqlite3.Connection, bucket: str, key: str, window_seconds: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM rate_limit_events "
        "WHERE bucket = ? AND key = ? AND created_at > datetime('now', ?)",
        (bucket, key.lower()[:200], f"-{window_seconds} seconds"),
    ).fetchone()
    return int(row["n"])


def check(
    conn: sqlite3.Connection, bucket: str, key: str, *, limit: int, window_seconds: int
) -> RateLimitResult:
    """Fenêtre glissante : au plus `limit` évènements sur `window_seconds`."""
    if _count_recent(conn, bucket, key, window_seconds) < limit:
        return RateLimitResult(allowed=True)

    oldest = conn.execute(
        "SELECT MIN(created_at) AS t FROM rate_limit_events "
        "WHERE bucket = ? AND key = ? AND created_at > datetime('now', ?)",
        (bucket, key.lower()[:200], f"-{window_seconds} seconds"),
    ).fetchone()
    retry_after = window_seconds
    if oldest is not None and oldest["t"] is not None:
        elapsed = conn.execute(
            "SELECT CAST((julianday('now') - julianday(?)) * 86400 AS INTEGER) AS s",
            (oldest["t"],),
        ).fetchone()["s"]
        retry_after = max(1, window_seconds - int(elapsed or 0))
    return RateLimitResult(allowed=False, retry_after_seconds=retry_after)


def consume(
    conn: sqlite3.Connection, bucket: str, key: str, *, limit: int, window_seconds: int
) -> RateLimitResult:
    """Vérifie puis enregistre la tentative si elle est autorisée."""
    result = check(conn, bucket, key, limit=limit, window_seconds=window_seconds)
    if result.allowed:
        record_attempt(conn, bucket, key)
    return result


# --- Verrouillage progressif après échecs de connexion -----------------------


def lockout_state(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    max_attempts: int,
    window_seconds: int,
    base_seconds: int,
    max_seconds: int,
) -> RateLimitResult:
    """Temporisation exponentielle au-delà de `max_attempts` échecs.

    Le délai double à chaque échec supplémentaire (1, 2, 4, 8… × `base_seconds`)
    et est plafonné à `max_seconds`, ce qui rend la force brute non rentable
    sans jamais verrouiller définitivement un compte — un attaquant pourrait
    sinon bloquer volontairement le compte de sa cible.
    """
    failures = _count_recent(conn, "login_failure", identifier, window_seconds)
    if failures < max_attempts:
        return RateLimitResult(allowed=True)

    penalty = min(max_seconds, base_seconds * (2 ** (failures - max_attempts)))
    last = conn.execute(
        "SELECT MAX(created_at) AS t FROM rate_limit_events "
        "WHERE bucket = 'login_failure' AND key = ?",
        (identifier.lower()[:200],),
    ).fetchone()
    if last is None or last["t"] is None:
        return RateLimitResult(allowed=True)

    elapsed = int(
        conn.execute(
            "SELECT CAST((julianday('now') - julianday(?)) * 86400 AS INTEGER) AS s",
            (last["t"],),
        ).fetchone()["s"]
        or 0
    )
    if elapsed >= penalty:
        return RateLimitResult(allowed=True)
    return RateLimitResult(allowed=False, retry_after_seconds=penalty - elapsed)


def clear_failures(conn: sqlite3.Connection, identifier: str) -> None:
    """Appelée après une connexion réussie."""
    conn.execute(
        "DELETE FROM rate_limit_events WHERE bucket = 'login_failure' AND key = ?",
        (identifier.lower()[:200],),
    )


def purge_old_events(conn: sqlite3.Connection, older_than_days: int = 2) -> int:
    cursor = conn.execute(
        "DELETE FROM rate_limit_events WHERE created_at < datetime('now', ?)",
        (f"-{older_than_days} days",),
    )
    return cursor.rowcount
