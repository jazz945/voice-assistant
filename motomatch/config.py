"""Configuration de l'application, pilotée par variables d'environnement.

Le principe : les valeurs par défaut sont sûres pour le développement local, et
l'application **refuse de démarrer** en production si un réglage sensible est
resté sur sa valeur de développement (voir `Settings.validate_for_production`).
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated

PACKAGE_DIR = Path(__file__).resolve().parent

# Marqueur explicite : si cette valeur survit en production, on refuse de booter.
DEV_SECRET_SENTINEL = "dev-only-insecure-secret-do-not-use-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOTOMATCH_", env_file=".env", extra="ignore")

    env: Literal["development", "production"] = "development"

    # --- Secrets et base ---
    secret_key: str = DEV_SECRET_SENTINEL
    db: Path = PACKAGE_DIR / "motomatch.db"

    # --- Durées de vie des jetons ---
    access_token_ttl_seconds: int = Field(default=15 * 60, ge=60, le=24 * 3600)
    refresh_token_ttl_seconds: int = Field(default=30 * 24 * 3600, ge=3600)

    # --- Anti-force brute ---
    login_max_attempts: int = Field(default=5, ge=3, le=50)
    login_window_seconds: int = Field(default=15 * 60, ge=60)
    lockout_base_seconds: int = Field(default=60, ge=10)
    lockout_max_seconds: int = Field(default=3600, ge=60)

    # Limites de débit : (nombre d'appels, fenêtre en secondes).
    rate_limit_register: tuple[int, int] = (5, 3600)
    rate_limit_refresh: tuple[int, int] = (60, 3600)
    rate_limit_message: tuple[int, int] = (60, 3600)
    rate_limit_swipe: tuple[int, int] = (300, 3600)
    rate_limit_report: tuple[int, int] = (20, 86400)

    # --- Confidentialité géographique ---
    # Les coordonnées d'autrui ne sortent jamais de l'API ; les distances sont
    # calculées sur une grille et renvoyées par paliers pour empêcher la
    # trilatération (cf. privacy.py).
    geo_grid_meters: int = Field(default=1000, ge=100, le=20_000)
    distance_bucket_km: int = Field(default=5, ge=1, le=50)

    # --- Exposition HTTP ---
    # Origines autorisées en CORS. Une application mobile native n'envoie pas
    # d'Origin : cette liste ne concerne que le client web.
    # `NoDecode` désactive le décodage JSON de pydantic-settings pour laisser
    # `_split_csv` accepter la forme « a,b,c », plus commode en variable d'env.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:8000"]
    )
    trusted_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])
    max_request_bytes: int = Field(default=256 * 1024, ge=1024)
    serve_web_client: bool = True
    docs_enabled: bool = True

    # --- Mots de passe ---
    password_min_length: int = Field(default=12, ge=8, le=128)

    @field_validator("cors_origins", "trusted_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accepte `MOTOMATCH_CORS_ORIGINS="https://a.tld,https://b.tld"`."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def validate_for_production(self) -> None:
        """Garde-fou de démarrage : aucune valeur de développement en production."""
        if not self.is_production:
            return

        problems: list[str] = []
        if self.secret_key == DEV_SECRET_SENTINEL or len(self.secret_key) < 32:
            problems.append(
                "MOTOMATCH_SECRET_KEY doit être défini et faire au moins 32 caractères "
                f"(ex. : {secrets.token_urlsafe(32)})"
            )
        if "*" in self.trusted_hosts:
            problems.append("MOTOMATCH_TRUSTED_HOSTS ne peut pas valoir '*' en production")
        if any(origin == "*" for origin in self.cors_origins):
            problems.append("MOTOMATCH_CORS_ORIGINS ne peut pas valoir '*' en production")
        if any(origin.startswith("http://") for origin in self.cors_origins):
            problems.append("MOTOMATCH_CORS_ORIGINS doit n'utiliser que des origines HTTPS")
        if self.docs_enabled:
            problems.append("MOTOMATCH_DOCS_ENABLED doit être false en production")

        if problems:
            raise RuntimeError(
                "Configuration de production invalide :\n  - " + "\n  - ".join(problems)
            )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_for_production()
    return settings


def reload_settings() -> Settings:
    """Vide le cache — utilisé par les tests qui modifient l'environnement."""
    get_settings.cache_clear()
    return get_settings()
