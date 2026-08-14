"""Fixtures partagées : base isolée par test et client HTTP prêt à l'emploi."""

from __future__ import annotations

import importlib
import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """Client de test branché sur une base SQLite temporaire."""
    monkeypatch.setenv("MOTOMATCH_DB", str(tmp_path / "test.db"))

    # Les modules lisent MOTOMATCH_DB à chaque connexion, mais on recharge par
    # sécurité pour repartir d'un état propre si un test précédent a importé.
    from motomatch import db as db_module

    importlib.reload(db_module)
    db_module.init_db()

    from motomatch import main as main_module

    importlib.reload(main_module)

    with TestClient(main_module.app) as test_client:
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
    """Crée un compte (+ profil optionnel) et renvoie ses en-têtes d'auth."""

    def _register(email: str, password: str = "roadtrip2024", profile: dict | None = None):
        response = client.post("/api/auth/register", json={"email": email, "password": password})
        assert response.status_code == 201, response.text
        session = response.json()
        headers = {"Authorization": f"Bearer {session['token']}"}
        if profile is not None:
            saved = client.put("/api/me/profile", json=profile, headers=headers)
            assert saved.status_code == 200, saved.text
        return {"headers": headers, "user_id": session["user_id"], "token": session["token"]}

    return _register
