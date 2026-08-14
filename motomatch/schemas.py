"""Schémas Pydantic exposés par l'API."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, EmailStr, Field, field_validator

from .matching import BIKE_CATEGORIES, PACE_LEVELS, RIDING_STYLES

MIN_AGE = 18
MAX_AGE = 99


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


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
    riding_styles: list[str] = Field(default_factory=list)
    pace: str
    experience_years: int = Field(default=0, ge=0, le=80)
    annual_km: int = Field(default=0, ge=0, le=200_000)
    max_travel_km: int = Field(default=100, ge=5, le=2000)
    has_passenger_seat: bool = True
    photo_url: str = Field(default="", max_length=500)

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


class SwipeInput(BaseModel):
    target_user_id: int
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


class DiscoveryFilters(BaseModel):
    max_distance_km: int | None = Field(default=None, ge=1, le=2000)
    min_age: int | None = Field(default=None, ge=MIN_AGE, le=MAX_AGE)
    max_age: int | None = Field(default=None, ge=MIN_AGE, le=MAX_AGE)
    categories: list[str] = Field(default_factory=list)
    styles: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)
