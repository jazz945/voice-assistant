"""MotoMatch — API de l'application de rencontre pour motards."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import repository as repo
from .db import get_connection, init_db
from .matching import (
    BIKE_CATEGORIES,
    PACE_LEVELS,
    RIDING_STYLES,
    age_from_birth_year,
    compatibility,
    haversine_km,
    parse_styles,
)
from .schemas import (
    Credentials,
    DiscoveryFilters,
    MessageInput,
    ProfileInput,
    SwipeInput,
)
from .security import hash_password, new_session_token, verify_password

STATIC_DIR = Path(__file__).resolve().parent / "static"

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="MotoMatch",
    description="Application de rencontre pour motards : profils, matchs et messagerie.",
    version="1.0.0",
    lifespan=lifespan,
)


# --- Dépendances ------------------------------------------------------------


def db() -> Iterator[sqlite3.Connection]:
    with get_connection() as conn:
        yield conn


def current_user(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    authorization: Annotated[str | None, Header()] = None,
) -> sqlite3.Row:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "jeton d'authentification manquant")
    user = repo.get_session_user(conn, authorization.split(" ", 1)[1].strip())
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session invalide ou expirée")
    return user


def require_profile(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> sqlite3.Row:
    profile = repo.get_profile(conn, user["id"])
    if profile is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "complétez votre profil moto avant d'utiliser cette fonctionnalité",
        )
    return profile


# --- Sérialisation ----------------------------------------------------------


def profile_payload(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["riding_styles"] = parse_styles(data.get("riding_styles"))
    data["has_passenger_seat"] = bool(data.get("has_passenger_seat"))
    data["age"] = age_from_birth_year(int(data["birth_year"]))
    return data


# --- Métadonnées ------------------------------------------------------------


@app.get("/api/meta", tags=["meta"])
def meta() -> dict[str, list[str]]:
    """Valeurs autorisées pour les champs à choix — consommées par le formulaire."""
    return {
        "bike_categories": list(BIKE_CATEGORIES),
        "riding_styles": list(RIDING_STYLES),
        "pace_levels": list(PACE_LEVELS),
    }


# --- Authentification -------------------------------------------------------


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED, tags=["auth"])
def register(
    credentials: Credentials, conn: Annotated[sqlite3.Connection, Depends(db)]
) -> dict[str, Any]:
    if repo.get_user_by_email(conn, credentials.email) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "cette adresse e-mail est déjà utilisée")
    user_id = repo.create_user(conn, credentials.email, hash_password(credentials.password))
    token = new_session_token()
    repo.create_session(conn, token, user_id)
    return {"token": token, "user_id": user_id, "has_profile": False}


@app.post("/api/auth/login", tags=["auth"])
def login(
    credentials: Credentials, conn: Annotated[sqlite3.Connection, Depends(db)]
) -> dict[str, Any]:
    user = repo.get_user_by_email(conn, credentials.email)
    if user is None or not verify_password(credentials.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "identifiants invalides")
    token = new_session_token()
    repo.create_session(conn, token, user["id"])
    return {
        "token": token,
        "user_id": user["id"],
        "has_profile": repo.get_profile(conn, user["id"]) is not None,
    }


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
def logout(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if authorization and authorization.lower().startswith("bearer "):
        repo.delete_session(conn, authorization.split(" ", 1)[1].strip())


# --- Profil -----------------------------------------------------------------


@app.get("/api/me", tags=["profil"])
def read_me(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    profile = repo.get_profile(conn, user["id"])
    return {
        "user_id": user["id"],
        "email": user["email"],
        "profile": profile_payload(profile) if profile else None,
    }


@app.put("/api/me/profile", tags=["profil"])
def save_profile(
    profile: ProfileInput,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    repo.upsert_profile(conn, user["id"], profile)
    return profile_payload(repo.get_profile(conn, user["id"]))


# --- Découverte -------------------------------------------------------------


@app.get("/api/discover", tags=["découverte"])
def discover(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    viewer: Annotated[sqlite3.Row, Depends(require_profile)],
    max_distance_km: int | None = Query(default=None, ge=1, le=2000),
    min_age: int | None = Query(default=None, ge=18, le=99),
    max_age: int | None = Query(default=None, ge=18, le=99),
    categories: Annotated[list[str], Query()] = [],
    styles: Annotated[list[str], Query()] = [],
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Profils candidats, triés par score de compatibilité décroissant."""
    filters = DiscoveryFilters(
        max_distance_km=max_distance_km,
        min_age=min_age,
        max_age=max_age,
        categories=[c.lower() for c in categories],
        styles=[s.lower() for s in styles],
        limit=limit,
    )
    if filters.min_age and filters.max_age and filters.min_age > filters.max_age:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "min_age ne peut pas dépasser max_age")

    today = date.today()
    results: list[dict[str, Any]] = []
    for candidate in repo.list_candidate_profiles(conn, viewer["user_id"]):
        distance = haversine_km(
            viewer["latitude"], viewer["longitude"], candidate["latitude"], candidate["longitude"]
        )
        if filters.max_distance_km is not None and distance > filters.max_distance_km:
            continue

        age = age_from_birth_year(candidate["birth_year"], today)
        if filters.min_age is not None and age < filters.min_age:
            continue
        if filters.max_age is not None and age > filters.max_age:
            continue
        if filters.categories and candidate["bike_category"] not in filters.categories:
            continue
        candidate_styles = parse_styles(candidate["riding_styles"])
        if filters.styles and not set(filters.styles) & set(candidate_styles):
            continue

        score = compatibility(viewer, candidate, distance_km=distance)
        results.append({"profile": profile_payload(candidate), **score})

    results.sort(key=lambda item: item["score"], reverse=True)
    return {"count": len(results), "results": results[: filters.limit]}


# --- Swipes -----------------------------------------------------------------


@app.post("/api/swipes", tags=["découverte"])
def swipe(
    payload: SwipeInput,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    viewer: Annotated[sqlite3.Row, Depends(require_profile)],
) -> dict[str, Any]:
    """Enregistre un like/pass ; crée un match si le like est réciproque."""
    viewer_id = int(viewer["user_id"])
    if payload.target_user_id == viewer_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "impossible de se liker soi-même")
    if repo.get_user(conn, payload.target_user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "profil introuvable")

    repo.record_swipe(conn, viewer_id, payload.target_user_id, payload.direction)

    matched = payload.direction == "like" and repo.has_liked(
        conn, payload.target_user_id, viewer_id
    )
    match_id = repo.create_match(conn, viewer_id, payload.target_user_id) if matched else None
    return {"matched": matched, "match_id": match_id}


# --- Matchs et messagerie ---------------------------------------------------


@app.get("/api/matches", tags=["messagerie"])
def matches(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    rows = repo.list_matches(conn, user["id"])
    items = [
        {
            "match_id": row["match_id"],
            "matched_at": row["matched_at"],
            "last_message": row["last_message"],
            "last_message_at": row["last_message_at"],
            "profile": profile_payload(row),
        }
        for row in rows
    ]
    return {"count": len(items), "results": items}


@app.get("/api/matches/{match_id}/messages", tags=["messagerie"])
def read_messages(
    match_id: int,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    if repo.get_match_for_user(conn, match_id, user["id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "match introuvable")
    return {"results": repo.rows_to_dicts(repo.list_messages(conn, match_id))}


@app.post(
    "/api/matches/{match_id}/messages",
    status_code=status.HTTP_201_CREATED,
    tags=["messagerie"],
)
def send_message(
    match_id: int,
    payload: MessageInput,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    if repo.get_match_for_user(conn, match_id, user["id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "match introuvable")
    body = payload.body.strip()
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "le message ne peut pas être vide")
    message_id = repo.add_message(conn, match_id, user["id"], body)
    return {"id": message_id, "match_id": match_id, "sender_id": user["id"], "body": body}


# --- Interface web ----------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
