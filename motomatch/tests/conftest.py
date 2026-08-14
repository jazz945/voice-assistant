"""Fixtures partagées : base isolée par test et client HTTP prêt à l'emploi."""

from __future__ import annotations

import importlib
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

# Mot de passe conforme à la politique (12 caractères minimum, non courant).
TEST_PASSWORD = "Vercors-Col-2024"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Environnement de base : chaque test a sa propre base SQLite."""
    monkeypatch.setenv("MOTOMATCH_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MOTOMATCH_ENV", "development")
    monkeypatch.setenv("MOTOMATCH_SECRET_KEY", "cle-de-test-suffisamment-longue-pour-les-tests")
    return monkeypatch


@pytest.fixture
def build_client(env):
    """Fabrique un client de test, avec surcharges de configuration optionnelles.

    Les modules lisent la configuration à l'import ; on les recharge donc après
    avoir posé les variables d'environnement.
    """

    def _build(*, raise_server_exceptions: bool = True, **settings_env: str) -> TestClient:
        for key, value in settings_env.items():
            env.setenv(f"MOTOMATCH_{key.upper()}", value)

        from motomatch import config as config_module

        config_module.reload_settings()

        for name in ("db", "audit", "repository", "main"):
            importlib.reload(importlib.import_module(f"motomatch.{name}"))

        from motomatch import main as main_module

        return TestClient(main_module.app, raise_server_exceptions=raise_server_exceptions)

    return _build


@pytest.fixture
def client(build_client) -> Iterator[TestClient]:
    with build_client() as test_client:
        yield test_client


@pytest.fixture
def rider_profile() -> dict:
    return {
        "display_name": "Camille",
        "birth_year": 1993,
        "gender": "femme",
        "seeking": "hommes",
        "city": "Lyon",
        "latitude": 45.7640,
        "longitude": 4.8357,
        "bio": "Cols et mécanique.",
        "bike_brand": "Yamaha",
        "bike_model": "MT-09",
        "bike_year": 2021,
        "engine_cc": 890,
        "bike_category": "roadster",
        "riding_styles": ["balade", "col"],
        "pace": "sportif",
        "experience_years": 9,
        "annual_km": 14000,
        "max_travel_km": 150,
        "has_passenger_seat": True,
        "photo_url": "",
    }


@pytest.fixture
def register(client):
    """Crée un compte (+ profil optionnel) et renvoie ses jetons et en-têtes."""

    def _register(email: str, password: str = TEST_PASSWORD, profile: dict | None = None):
        response = client.post(
            "/api/auth/register",
            json={"email": email, "password": password, "age_attestation": True},
        )
        assert response.status_code == 201, response.text
        session = response.json()
        headers = {"Authorization": f"Bearer {session['access_token']}"}
        if profile is not None:
            saved = client.put("/api/me/profile", json=profile, headers=headers)
            assert saved.status_code == 200, saved.text
        return {
            "headers": headers,
            "user_id": session["user_id"],
            "access_token": session["access_token"],
            "refresh_token": session["refresh_token"],
        }

    return _register
