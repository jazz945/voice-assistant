"""Hachage et politique de mots de passe.

Argon2id est l'algorithme de référence actuel (lauréat de la Password Hashing
Competition, recommandé par l'OWASP). Les empreintes PBKDF2 de la version
précédente restent vérifiables et sont **remigrées silencieusement vers Argon2id
à la première connexion réussie** — cf. `needs_rehash`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Paramètres conformes aux recommandations OWASP pour Argon2id
# (19 Mio de mémoire, 2 passes, parallélisme 1).
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

# Empreinte d'un mot de passe fictif : sert à faire travailler le CPU même quand
# le compte n'existe pas, pour que le temps de réponse ne trahisse pas son
# existence (énumération par mesure de latence).
_DUMMY_HASH = _hasher.hash("mot-de-passe-inexistant-pour-temps-constant")

# Mots de passe les plus courants — refusés quelle que soit leur longueur.
_COMMON_PASSWORDS = frozenset(
    {
        "123456789012", "azertyuiopqs", "motdepasse12", "password1234",
        "qwertyuiop12", "administrator", "motdepasse123", "password123!",
        "123456789abc", "iloveyou1234", "welcome12345", "abcd1234efgh",
        "letmein12345", "monkey123456", "dragon123456", "sunshine1234",
        "princess1234", "football1234", "baseball1234", "superman1234",
        "azerty123456", "qwerty123456", "motorcycle12", "harleydavidson",
    }
)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored: str) -> bool:
    """Vérifie un mot de passe contre une empreinte Argon2id ou PBKDF2 héritée."""
    if stored.startswith("pbkdf2_sha256$"):
        return _verify_legacy_pbkdf2(password, stored)
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    """Vrai si l'empreinte doit être régénérée (PBKDF2 hérité ou paramètres obsolètes)."""
    if stored.startswith("pbkdf2_sha256$"):
        return True
    try:
        return _hasher.check_needs_rehash(stored)
    except InvalidHashError:
        return True


def waste_time_like_a_verification() -> None:
    """Consomme le même temps CPU qu'une vérification réelle.

    Appelée quand l'adresse e-mail est inconnue, pour que « compte inexistant »
    et « mauvais mot de passe » soient indiscernables au chronomètre.
    """
    try:
        _hasher.verify(_DUMMY_HASH, "mot-de-passe-forcement-different")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass


def _verify_legacy_pbkdf2(password: str, stored: str) -> bool:
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


# --- Politique de robustesse -------------------------------------------------


def _normalise(value: str) -> str:
    """Minuscules sans accents, pour comparer un mot de passe à des données perso."""
    decomposed = unicodedata.normalize("NFKD", value.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def password_problems(password: str, *, min_length: int, personal_data: list[str]) -> list[str]:
    """Retourne la liste des raisons pour lesquelles un mot de passe est refusé.

    On suit l'esprit du NIST SP 800-63B : priorité à la longueur et au blocage
    des mots de passe devinables, plutôt qu'à des règles de composition rigides.
    """
    problems: list[str] = []

    if len(password) < min_length:
        problems.append(f"le mot de passe doit faire au moins {min_length} caractères")
    if len(password) > 128:
        problems.append("le mot de passe ne peut pas dépasser 128 caractères")

    normalised = _normalise(password)
    if normalised in _COMMON_PASSWORDS:
        problems.append("ce mot de passe est trop courant")

    # Une seule valeur répétée ou une suite simple : « aaaaaaaaaaaa », « 123456… ».
    if len(set(password)) < 5:
        problems.append("le mot de passe doit contenir au moins 5 caractères différents")
    if re.search(r"(0123456789|abcdefghij|azertyuiop|qwertyuiop)", normalised):
        problems.append("le mot de passe ne doit pas être une suite de touches")

    for item in personal_data:
        candidate = _normalise(item).strip()
        # On ne compare que des fragments significatifs (e-mail, prénom, ville).
        if len(candidate) >= 4 and candidate in normalised:
            problems.append("le mot de passe ne doit pas contenir vos informations personnelles")
            break

    return problems
