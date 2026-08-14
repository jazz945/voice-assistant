"""Hachage des mots de passe et jetons de session.

PBKDF2-HMAC-SHA256 de la stdlib : pas de dépendance supplémentaire, et le format
stocké embarque ses paramètres pour permettre une montée d'itérations plus tard.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ITERATIONS = 240_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
