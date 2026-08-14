"""Schémas Pydantic exposés par l'API.

La validation est la première ligne de défense : tout ce qui entre est contraint
en type, en longueur et en valeurs autorisées avant d'atteindre la base.
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, timezone
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


# --- Croisements ------------------------------------------------------------


class CrossingSettingsInput(BaseModel):
    """Interrupteur des croisements : opt-in explicite, coupable à tout moment."""

    enabled: bool


class LocationPingInput(BaseModel):
    """Position envoyée par le client.

    Elle est réduite à une cellule de grille dès réception : ni `latitude` ni
    `longitude` n'atteignent jamais la base (cf. `crossings.py`).
    """

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    speed_kmh: float | None = Field(default=None, ge=0, le=400)
    heading_deg: float | None = Field(default=None, ge=0, lt=360)
    ride_id: int | None = Field(default=None, gt=0)


# --- Balades ----------------------------------------------------------------

RIDE_ROUTE_TYPES = (
    "cols",
    "departementales",
    "autoroute",
    "off-road",
    "urbain",
    "circuit",
    "cotier",
)

RIDE_VISIBILITIES = ("public", "matchs", "sur-demande")


class RideInput(BaseModel):
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(default="", max_length=2000)
    start_city: str = Field(min_length=1, max_length=80)
    start_latitude: float = Field(ge=-90, le=90)
    start_longitude: float = Field(ge=-180, le=180)
    start_at: datetime
    distance_km: int = Field(default=0, ge=0, le=5000)
    pace: str
    route_type: str
    bike_categories: list[str] = Field(default_factory=list)
    max_participants: int = Field(default=8, ge=2, le=100)
    visibility: str = "public"

    @field_validator("title", "start_city", "description")
    @classmethod
    def _clean(cls, value: str) -> str:
        return clean_text(value)

    @field_validator("start_at")
    @classmethod
    def _must_be_future(cls, value: datetime) -> datetime:
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if moment <= datetime.now(timezone.utc):
            raise ValueError("la date de départ doit être dans le futur")
        return moment

    @field_validator("pace")
    @classmethod
    def _check_pace(cls, value: str) -> str:
        pace = value.strip().lower()
        if pace not in PACE_LEVELS:
            raise ValueError(f"rythme inconnu, valeurs possibles : {', '.join(PACE_LEVELS)}")
        return pace

    @field_validator("route_type")
    @classmethod
    def _check_route_type(cls, value: str) -> str:
        route = value.strip().lower()
        if route not in RIDE_ROUTE_TYPES:
            raise ValueError(f"type de route inconnu, valeurs possibles : {', '.join(RIDE_ROUTE_TYPES)}")
        return route

    @field_validator("visibility")
    @classmethod
    def _check_visibility(cls, value: str) -> str:
        visibility = value.strip().lower()
        if visibility not in RIDE_VISIBILITIES:
            raise ValueError(
                f"autorisation inconnue, valeurs possibles : {', '.join(RIDE_VISIBILITIES)}"
            )
        return visibility

    @field_validator("bike_categories")
    @classmethod
    def _check_categories(cls, value: list[str]) -> list[str]:
        """Familles de motos bienvenues. Vide = toutes."""
        cleaned: list[str] = []
        for category in value:
            normalised = category.strip().lower()
            if normalised not in BIKE_CATEGORIES:
                raise ValueError(
                    f"catégorie inconnue '{category}', valeurs possibles : {', '.join(BIKE_CATEGORIES)}"
                )
            if normalised not in cleaned:
                cleaned.append(normalised)
        return cleaned


class ParticipationDecisionInput(BaseModel):
    """Décision de l'organisateur sur une demande de participation."""

    decision: str

    @field_validator("decision")
    @classmethod
    def _check_decision(cls, value: str) -> str:
        decision = value.strip().lower()
        if decision not in {"accepte", "refuse"}:
            raise ValueError("decision doit valoir 'accepte' ou 'refuse'")
        return decision


class DiscoveryFilters(BaseModel):
    max_distance_km: int | None = Field(default=None, ge=1, le=2000)
    min_age: int | None = Field(default=None, ge=MIN_AGE, le=MAX_AGE)
    max_age: int | None = Field(default=None, ge=MIN_AGE, le=MAX_AGE)
    categories: list[str] = Field(default_factory=list)
    styles: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)
