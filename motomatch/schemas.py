"""Schémas Pydantic exposés par l'API.

La validation est la première ligne de défense : tout ce qui entre est contraint
en type, en longueur et en valeurs autorisées avant d'atteindre la base.
"""

from __future__ import annotations

import unicodedata
from datetime import date
from urllib.parse import urlparse

from pydantic import BaseModel, EmailStr, Field, field_validator

from .matching import BIKE_CATEGORIES, PACE_LEVELS, RIDING_STYLES

MIN_AGE = 18
MAX_AGE = 99

REPORT_REASONS = (
    "harcelement",
    "faux-profil",
    "contenu-inapproprie",
    "spam",
    "mineur",
    "arnaque",
    "autre",
)


def clean_text(value: str) -> str:
    """Supprime les caractères de contrôle et normalise l'Unicode.

    Bloque notamment les marques de direction bidirectionnelle, qui permettent
    d'afficher un texte différent de ce qui est stocké.
    """
    normalised = unicodedata.normalize("NFKC", value)
    return "".join(
        char
        for char in normalised
        if unicodedata.category(char) not in {"Cc", "Cf", "Co", "Cs"} or char in "\n\t"
    ).strip()


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RegistrationInput(Credentials):
    # Attestation de majorité : l'App Store et le Play Store l'exigent pour une
    # application de rencontre. L'âge réel est revérifié à la saisie du profil.
    age_attestation: bool = Field(
        description="l'utilisateur déclare avoir 18 ans ou plus",
    )

    @field_validator("age_attestation")
    @classmethod
    def _must_attest(cls, value: bool) -> bool:
        if not value:
            raise ValueError("l'inscription est réservée aux personnes majeures")
        return value


class PasswordChangeInput(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class RefreshInput(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class ProfileInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)
    birth_year: int
    gender: str = Field(min_length=1, max_length=30)
    seeking: str = Field(min_length=1, max_length=60)
    city: str = Field(min_length=1, max_length=80)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    bio: str = Field(default="", max_length=1000)
    bike_brand: str = Field(min_length=1, max_length=40)
    bike_model: str = Field(min_length=1, max_length=60)
    bike_year: int | None = Field(default=None, ge=1900, le=2100)
    engine_cc: int = Field(ge=50, le=3000)
    bike_category: str
    riding_styles: list[str] = Field(default_factory=list, max_length=len(RIDING_STYLES))
    pace: str
    experience_years: int = Field(default=0, ge=0, le=80)
    annual_km: int = Field(default=0, ge=0, le=200_000)
    max_travel_km: int = Field(default=100, ge=5, le=2000)
    has_passenger_seat: bool = True
    photo_url: str = Field(default="", max_length=500)

    @field_validator("display_name", "gender", "seeking", "city", "bike_brand", "bike_model", "bio")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = clean_text(value)
        if not cleaned and value.strip():
            raise ValueError("valeur invalide")
        return cleaned

    @field_validator("birth_year")
    @classmethod
    def _check_age(cls, value: int) -> int:
        age = date.today().year - value
        if not MIN_AGE <= age <= MAX_AGE:
            raise ValueError(f"l'âge doit être compris entre {MIN_AGE} et {MAX_AGE} ans")
        return value

    @field_validator("bike_category")
    @classmethod
    def _check_category(cls, value: str) -> str:
        category = value.strip().lower()
        if category not in BIKE_CATEGORIES:
            raise ValueError(f"catégorie inconnue, valeurs possibles : {', '.join(BIKE_CATEGORIES)}")
        return category

    @field_validator("pace")
    @classmethod
    def _check_pace(cls, value: str) -> str:
        pace = value.strip().lower()
        if pace not in PACE_LEVELS:
            raise ValueError(f"rythme inconnu, valeurs possibles : {', '.join(PACE_LEVELS)}")
        return pace

    @field_validator("riding_styles")
    @classmethod
    def _check_styles(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for style in value:
            normalized = style.strip().lower()
            if normalized not in RIDING_STYLES:
                raise ValueError(
                    f"pratique inconnue '{style}', valeurs possibles : {', '.join(RIDING_STYLES)}"
                )
            if normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned

    @field_validator("photo_url")
    @classmethod
    def _check_photo_url(cls, value: str) -> str:
        """N'accepte que du HTTPS : bloque `javascript:`, `data:` et `file:`."""
        if not value:
            return ""
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("l'URL de la photo doit être en https://")
        return value


class SwipeInput(BaseModel):
    target_user_id: int = Field(gt=0)
    direction: str

    @field_validator("direction")
    @classmethod
    def _check_direction(cls, value: str) -> str:
        direction = value.strip().lower()
        if direction not in {"like", "pass"}:
            raise ValueError("direction doit valoir 'like' ou 'pass'")
        return direction


class MessageInput(BaseModel):
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("body")
    @classmethod
    def _clean_body(cls, value: str) -> str:
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("le message ne peut pas être vide")
        return cleaned


class BlockInput(BaseModel):
    target_user_id: int = Field(gt=0)


class ReportInput(BaseModel):
    target_user_id: int = Field(gt=0)
    reason: str
    details: str = Field(default="", max_length=1000)

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, value: str) -> str:
        reason = value.strip().lower()
        if reason not in REPORT_REASONS:
            raise ValueError(f"motif inconnu, valeurs possibles : {', '.join(REPORT_REASONS)}")
        return reason

    @field_validator("details")
    @classmethod
    def _clean_details(cls, value: str) -> str:
        return clean_text(value)


class AccountDeletionInput(BaseModel):
    """La suppression de compte exige le mot de passe : elle est irréversible."""

    password: str = Field(min_length=1, max_length=128)
    confirmation: str

    @field_validator("confirmation")
    @classmethod
    def _check_confirmation(cls, value: str) -> str:
        if value.strip().upper() != "SUPPRIMER":
            raise ValueError("tapez SUPPRIMER pour confirmer la suppression définitive")
        return value


class DiscoveryFilters(BaseModel):
    max_distance_km: int | None = Field(default=None, ge=1, le=2000)
    min_age: int | None = Field(default=None, ge=MIN_AGE, le=MAX_AGE)
    max_age: int | None = Field(default=None, ge=MIN_AGE, le=MAX_AGE)
    categories: list[str] = Field(default_factory=list)
    styles: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)
