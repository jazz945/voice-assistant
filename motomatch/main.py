"""MotoMatch — API de l'application de rencontre pour motards.

Posture de sécurité, en résumé (détail dans `SECURITY.md`) :

- mots de passe Argon2id, politique inspirée du NIST SP 800-63B ;
- jetons opaques révocables : accès court + rafraîchissement rotatif avec
  détection de rejeu, aucun jeton stocké en clair ;
- limitation de débit et verrouillage progressif sur tous les points sensibles ;
- coordonnées GPS d'autrui jamais exposées, distances calculées sur une grille
  pour empêcher la trilatération ;
- blocage, signalement, suppression de compte et export des données.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import audit, crossings
from . import repository as repo
from .config import Settings, get_settings
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
from .middleware import RequestSizeLimitMiddleware, SecurityHeadersMiddleware
from .privacy import bucket_distance, public_profile, snap_to_grid
from .schemas import (
    REPORT_REASONS,
    RIDE_ROUTE_TYPES,
    RIDE_VISIBILITIES,
    AccountDeletionInput,
    BlockInput,
    Credentials,
    CrossingSettingsInput,
    DiscoveryFilters,
    LocationPingInput,
    MessageInput,
    ParticipationDecisionInput,
    PasswordChangeInput,
    ProfileInput,
    RefreshInput,
    RegistrationInput,
    ReportInput,
    RideInput,
    SwipeInput,
)
from .security import passwords, ratelimit, tokens

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    with get_connection() as conn:
        # Ménage au démarrage : sessions périmées et compteurs de débit anciens.
        tokens.purge_expired(conn)
        ratelimit.purge_old_events(conn)
        repo.purge_old_pings(conn, get_settings().ping_retention_hours)
    yield


settings = get_settings()

app = FastAPI(
    title="MotoMatch",
    description="Application de rencontre pour motards : profils, matchs et messagerie.",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

app.add_middleware(SecurityHeadersMiddleware, settings=settings)
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,  # l'authentification passe par un en-tête, pas un cookie
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


# --- Gestion des erreurs ----------------------------------------------------


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Erreurs de validation réduites à l'essentiel.

    On ne renvoie que `loc`, `msg` et `type` : le `ctx` de Pydantic embarque
    l'exception d'origine et la valeur reçue, qu'on ne veut ni sérialiser ni
    renvoyer à l'appelant (le corps peut contenir un mot de passe).
    """
    details = [
        {"loc": [str(part) for part in error.get("loc", [])],
         "msg": str(error.get("msg", "")),
         "type": str(error.get("type", ""))}
        for error in exc.errors()
    ]
    return JSONResponse({"detail": details}, status_code=422)


@app.exception_handler(Exception)
async def _unhandled_error(_: Request, __: Exception) -> JSONResponse:
    """Aucune trace d'exécution ne sort de l'API : elle renseignerait un attaquant."""
    return JSONResponse({"detail": "erreur interne"}, status_code=500)


# --- Dépendances ------------------------------------------------------------


def db() -> Iterator[sqlite3.Connection]:
    with get_connection() as conn:
        yield conn


def config() -> Settings:
    return get_settings()


def client_ip(request: Request) -> str:
    """Adresse de l'appelant.

    On n'accorde aucune confiance à `X-Forwarded-For` : l'en-tête est trivial à
    falsifier et servirait à contourner la limitation de débit. Derrière un
    reverse proxy, utiliser `--proxy-headers` d'Uvicorn avec `--forwarded-allow-ips`
    restreint aux adresses du proxy, ce qui renseigne `request.client` de façon fiable.
    """
    return request.client.host if request.client else "inconnu"


def device_label(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:200]


def current_user(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    authorization: Annotated[str | None, Header()] = None,
) -> sqlite3.Row:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "jeton d'authentification manquant",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = tokens.resolve_access_token(conn, authorization.split(" ", 1)[1].strip())
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "session invalide ou expirée",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
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


def enforce_rate_limit(
    conn: sqlite3.Connection,
    bucket: str,
    key: str,
    quota: tuple[int, int],
    *,
    user_id: int | None = None,
    ip: str | None = None,
) -> None:
    limit, window = quota
    result = ratelimit.consume(conn, bucket, key, limit=limit, window_seconds=window)
    if not result.allowed:
        audit.record(conn, audit.RATE_LIMITED, user_id=user_id, ip=ip, detail=bucket)
        conn.commit()  # le journal doit survivre à la remontée de l'exception
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "trop de requêtes, réessayez plus tard",
            headers={"Retry-After": str(result.retry_after_seconds)},
        )


# --- Sérialisation ----------------------------------------------------------


def own_profile_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Profil de l'utilisateur connecté : il voit ses propres coordonnées.

    On filtre sur les colonnes de profil : ces lignes proviennent souvent d'une
    jointure (matchs, croisements) qui charrie des colonnes techniques n'ayant
    rien à faire dans une réponse.
    """
    data = {key: value for key, value in dict(row).items() if key in repo.PROFILE_FIELDS}
    data["riding_styles"] = parse_styles(data.get("riding_styles"))
    data["has_passenger_seat"] = bool(data.get("has_passenger_seat"))
    data["age"] = age_from_birth_year(int(data["birth_year"]))
    return data


def other_profile_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Profil d'un tiers : sans latitude ni longitude (cf. privacy.py)."""
    return public_profile(own_profile_payload(row))


def token_payload(pair: tokens.TokenPair, *, has_profile: bool, user_id: int) -> dict[str, Any]:
    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "token_type": "Bearer",
        "expires_in": pair.access_expires_in,
        "refresh_expires_in": pair.refresh_expires_in,
        "user_id": user_id,
        "has_profile": has_profile,
    }


# --- Métadonnées ------------------------------------------------------------


@app.get("/api/meta", tags=["meta"])
def meta(cfg: Annotated[Settings, Depends(config)]) -> dict[str, Any]:
    """Valeurs autorisées pour les champs à choix — consommées par le client."""
    return {
        "bike_categories": list(BIKE_CATEGORIES),
        "riding_styles": list(RIDING_STYLES),
        "pace_levels": list(PACE_LEVELS),
        "report_reasons": list(REPORT_REASONS),
        "ride_route_types": list(RIDE_ROUTE_TYPES),
        "ride_visibilities": list(RIDE_VISIBILITIES),
        "password_min_length": cfg.password_min_length,
    }


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# --- Authentification -------------------------------------------------------


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED, tags=["auth"])
def register(
    payload: RegistrationInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    cfg: Annotated[Settings, Depends(config)],
) -> dict[str, Any]:
    ip = client_ip(request)
    enforce_rate_limit(conn, "register", ip, cfg.rate_limit_register, ip=ip)

    problems = passwords.password_problems(
        payload.password,
        min_length=cfg.password_min_length,
        personal_data=[payload.email, payload.email.split("@")[0]],
    )
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problems)

    if repo.get_user_by_email(conn, payload.email) is not None:
        # Compromis assumé : cette réponse révèle qu'un compte existe. La
        # corriger proprement suppose la vérification par e-mail (cf. SECURITY.md,
        # « Limites connues ») ; en attendant, l'inscription est très fortement
        # limitée en débit par IP pour rendre l'énumération impraticable.
        raise HTTPException(status.HTTP_409_CONFLICT, "cette adresse e-mail est déjà utilisée")

    user_id = repo.create_user(conn, payload.email, passwords.hash_password(payload.password))
    pair = tokens.issue_pair(
        conn,
        user_id,
        access_ttl=cfg.access_token_ttl_seconds,
        refresh_ttl=cfg.refresh_token_ttl_seconds,
        device_label=device_label(request),
    )
    audit.record(conn, audit.REGISTER, user_id=user_id, ip=ip)
    return token_payload(pair, has_profile=False, user_id=user_id)


@app.post("/api/auth/login", tags=["auth"])
def login(
    credentials: Credentials,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    cfg: Annotated[Settings, Depends(config)],
) -> dict[str, Any]:
    ip = client_ip(request)
    email = credentials.email.lower()

    # Deux verrous distincts : par compte (protège un compte visé) et par IP
    # (protège l'ensemble des comptes d'un même attaquant).
    for identifier in (email, f"ip:{ip}"):
        state = ratelimit.lockout_state(
            conn,
            identifier,
            max_attempts=cfg.login_max_attempts,
            window_seconds=cfg.login_window_seconds,
            base_seconds=cfg.lockout_base_seconds,
            max_seconds=cfg.lockout_max_seconds,
        )
        if not state.allowed:
            audit.record(conn, audit.LOGIN_LOCKED, ip=ip, detail=identifier)
            conn.commit()
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "trop de tentatives, réessayez plus tard",
                headers={"Retry-After": str(state.retry_after_seconds)},
            )

    user = repo.get_user_by_email(conn, email)
    if user is None:
        # Même coût CPU que pour un compte existant : le temps de réponse ne
        # doit pas révéler si l'adresse est connue.
        passwords.waste_time_like_a_verification()
        _record_login_failure(conn, email, ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "identifiants invalides")

    if not passwords.verify_password(credentials.password, user["password_hash"]):
        _record_login_failure(conn, email, ip, user_id=int(user["id"]))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "identifiants invalides")

    # Migration transparente des anciennes empreintes PBKDF2 vers Argon2id.
    if passwords.needs_rehash(user["password_hash"]):
        repo.update_password_hash(
            conn, int(user["id"]), passwords.hash_password(credentials.password)
        )

    ratelimit.clear_failures(conn, email)
    ratelimit.clear_failures(conn, f"ip:{ip}")
    pair = tokens.issue_pair(
        conn,
        int(user["id"]),
        access_ttl=cfg.access_token_ttl_seconds,
        refresh_ttl=cfg.refresh_token_ttl_seconds,
        device_label=device_label(request),
    )
    audit.record(conn, audit.LOGIN_SUCCESS, user_id=int(user["id"]), ip=ip)
    return token_payload(
        pair,
        has_profile=repo.get_profile(conn, int(user["id"])) is not None,
        user_id=int(user["id"]),
    )


def _record_login_failure(
    conn: sqlite3.Connection, email: str, ip: str, user_id: int | None = None
) -> None:
    ratelimit.record_attempt(conn, "login_failure", email)
    ratelimit.record_attempt(conn, "login_failure", f"ip:{ip}")
    audit.record(conn, audit.LOGIN_FAILURE, user_id=user_id, ip=ip)
    conn.commit()


@app.post("/api/auth/refresh", tags=["auth"])
def refresh(
    payload: RefreshInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    cfg: Annotated[Settings, Depends(config)],
) -> dict[str, Any]:
    """Échange un jeton de rafraîchissement contre un couple neuf (rotation)."""
    ip = client_ip(request)
    enforce_rate_limit(conn, "refresh", ip, cfg.rate_limit_refresh, ip=ip)

    try:
        pair = tokens.rotate_refresh_token(
            conn,
            payload.refresh_token,
            access_ttl=cfg.access_token_ttl_seconds,
            refresh_ttl=cfg.refresh_token_ttl_seconds,
            device_label=device_label(request),
        )
    except tokens.RefreshTokenReuse as reuse:
        # Un jeton déjà consommé a été rejoué : vol probable. Toute la famille
        # de sessions vient d'être révoquée, y compris celle du voleur.
        audit.record(conn, audit.TOKEN_REUSE_DETECTED, ip=ip, detail=str(reuse))
        conn.commit()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "jeton rejoué : toutes les sessions ont été révoquées, reconnectez-vous",
        ) from None
    except LookupError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "jeton de rafraîchissement invalide"
        ) from None

    session = conn.execute(
        "SELECT user_id FROM sessions WHERE id = ?", (pair.session_id,)
    ).fetchone()
    user_id = int(session["user_id"])
    audit.record(conn, audit.TOKEN_REFRESHED, user_id=user_id, ip=ip)
    return token_payload(
        pair, has_profile=repo.get_profile(conn, user_id) is not None, user_id=user_id
    )


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
def logout(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> None:
    tokens.revoke_session(conn, int(user["session_id"]), int(user["id"]))
    audit.record(conn, audit.SESSION_REVOKED, user_id=int(user["id"]), ip=client_ip(request))


@app.post("/api/auth/logout-all", tags=["auth"])
def logout_all(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, int]:
    """Déconnecte tous les appareils, y compris celui-ci — cas du téléphone volé."""
    revoked = tokens.revoke_all_sessions(conn, int(user["id"]))
    audit.record(conn, audit.SESSIONS_REVOKED_ALL, user_id=int(user["id"]), ip=client_ip(request))
    return {"revoked_sessions": revoked}


@app.get("/api/auth/sessions", tags=["auth"])
def list_sessions(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    """Appareils connectés — permet à l'utilisateur de repérer une session inconnue."""
    rows = repo.rows_to_dicts(tokens.list_sessions(conn, int(user["id"])))
    for row in rows:
        row["current"] = row["id"] == user["session_id"]
    return {"count": len(rows), "results": rows}


@app.delete("/api/auth/sessions/{session_id}", status_code=204, tags=["auth"])
def revoke_session(
    session_id: int,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> None:
    if not tokens.revoke_session(conn, session_id, int(user["id"])):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session introuvable")
    audit.record(conn, audit.SESSION_REVOKED, user_id=int(user["id"]), ip=client_ip(request))


# --- Compte -----------------------------------------------------------------


@app.get("/api/me", tags=["compte"])
def read_me(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    profile = repo.get_profile(conn, int(user["id"]))
    return {
        "user_id": user["id"],
        "email": user["email"],
        "created_at": user["created_at"],
        "crossings_enabled": bool(user["crossings_enabled"]),
        "profile": own_profile_payload(profile) if profile else None,
    }


@app.put("/api/me/profile", tags=["compte"])
def save_profile(
    profile: ProfileInput,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    existing = repo.get_profile(conn, int(user["id"]))
    if existing is not None and int(existing["birth_year"]) != profile.birth_year:
        # L'année de naissance est figée : sinon un compte créé mineur pourrait
        # se vieillir après coup, et un majeur se rajeunir pour cibler des jeunes.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "l'année de naissance ne peut pas être modifiée, contactez le support",
        )
    repo.upsert_profile(conn, int(user["id"]), profile)
    return own_profile_payload(repo.get_profile(conn, int(user["id"])))


@app.post("/api/me/password", tags=["compte"])
def change_password(
    payload: PasswordChangeInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
    cfg: Annotated[Settings, Depends(config)],
) -> dict[str, Any]:
    if not passwords.verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "mot de passe actuel incorrect")

    problems = passwords.password_problems(
        payload.new_password,
        min_length=cfg.password_min_length,
        personal_data=[user["email"], str(user["email"]).split("@")[0]],
    )
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problems)

    repo.update_password_hash(
        conn, int(user["id"]), passwords.hash_password(payload.new_password)
    )
    # Un changement de mot de passe doit expulser d'éventuels intrus : on ne
    # garde que la session courante.
    revoked = tokens.revoke_all_sessions(conn, int(user["id"]), keep_id=int(user["session_id"]))
    audit.record(conn, audit.PASSWORD_CHANGED, user_id=int(user["id"]), ip=client_ip(request))
    return {"revoked_other_sessions": revoked}


@app.get("/api/me/export", tags=["compte"])
def export_my_data(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    """Export complet des données personnelles (RGPD article 20)."""
    data = repo.export_user_data(conn, int(user["id"]))
    audit.record(conn, audit.DATA_EXPORTED, user_id=int(user["id"]), ip=client_ip(request))
    return data


@app.delete("/api/me", status_code=status.HTTP_204_NO_CONTENT, tags=["compte"])
def delete_my_account(
    payload: AccountDeletionInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> None:
    """Suppression définitive du compte — obligatoire pour l'App Store (5.1.1(v))."""
    if not passwords.verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "mot de passe incorrect")
    user_id = int(user["id"])
    # Journalisé avant la suppression : la ligne d'audit passe à user_id NULL.
    audit.record(conn, audit.ACCOUNT_DELETED, user_id=user_id, ip=client_ip(request))
    repo.delete_user(conn, user_id)


# --- Découverte -------------------------------------------------------------


@app.get("/api/discover", tags=["découverte"])
def discover(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    viewer: Annotated[sqlite3.Row, Depends(require_profile)],
    cfg: Annotated[Settings, Depends(config)],
    max_distance_km: int | None = Query(default=None, ge=1, le=2000),
    min_age: int | None = Query(default=None, ge=18, le=99),
    max_age: int | None = Query(default=None, ge=18, le=99),
    categories: Annotated[list[str], Query()] = [],
    styles: Annotated[list[str], Query()] = [],
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Profils candidats, triés par score de compatibilité décroissant.

    Les distances sont calculées depuis une position plaquée sur une grille et
    renvoyées par paliers : voir `privacy.py` pour le détail de la protection
    contre la trilatération.
    """
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
    for candidate in repo.list_candidate_profiles(conn, int(viewer["user_id"])):
        snapped_lat, snapped_lon = snap_to_grid(
            candidate["latitude"],
            candidate["longitude"],
            user_id=int(candidate["user_id"]),
            secret_key=cfg.secret_key,
            grid_meters=cfg.geo_grid_meters,
        )
        distance = haversine_km(
            viewer["latitude"], viewer["longitude"], snapped_lat, snapped_lon
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
        score["distance_km"] = bucket_distance(distance, cfg.distance_bucket_km)
        results.append({"profile": other_profile_payload(candidate), **score})

    results.sort(key=lambda item: item["score"], reverse=True)
    return {"count": len(results), "results": results[: filters.limit]}


# --- Swipes -----------------------------------------------------------------


@app.post("/api/swipes", tags=["découverte"])
def swipe(
    payload: SwipeInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    viewer: Annotated[sqlite3.Row, Depends(require_profile)],
    cfg: Annotated[Settings, Depends(config)],
) -> dict[str, Any]:
    """Enregistre un like/pass ; crée un match si le like est réciproque."""
    viewer_id = int(viewer["user_id"])
    enforce_rate_limit(
        conn, "swipe", str(viewer_id), cfg.rate_limit_swipe, user_id=viewer_id, ip=client_ip(request)
    )

    if payload.target_user_id == viewer_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "impossible de se liker soi-même")
    if repo.get_user(conn, payload.target_user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "profil introuvable")
    if repo.is_blocked_either_way(conn, viewer_id, payload.target_user_id):
        # Même réponse que « profil introuvable » : la personne bloquée ne doit
        # pas pouvoir déduire qu'elle l'a été.
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
    rows = repo.list_matches(conn, int(user["id"]))
    items = [
        {
            "match_id": row["match_id"],
            "matched_at": row["matched_at"],
            "last_message": row["last_message"],
            "last_message_at": row["last_message_at"],
            "profile": other_profile_payload(row),
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
    if repo.get_match_for_user(conn, match_id, int(user["id"])) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "match introuvable")
    return {"results": repo.rows_to_dicts(repo.list_messages(conn, match_id))}


@app.post(
    "/api/matches/{match_id}/messages", status_code=status.HTTP_201_CREATED, tags=["messagerie"]
)
def send_message(
    match_id: int,
    payload: MessageInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
    cfg: Annotated[Settings, Depends(config)],
) -> dict[str, Any]:
    user_id = int(user["id"])
    enforce_rate_limit(
        conn, "message", str(user_id), cfg.rate_limit_message, user_id=user_id, ip=client_ip(request)
    )
    if repo.get_match_for_user(conn, match_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "match introuvable")
    message_id = repo.add_message(conn, match_id, user_id, payload.body)
    return {"id": message_id, "match_id": match_id, "sender_id": user_id, "body": payload.body}


# --- Sécurité des personnes -------------------------------------------------


@app.post("/api/blocks", status_code=status.HTTP_201_CREATED, tags=["sécurité"])
def block(
    payload: BlockInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    """Bloque un utilisateur : disparition mutuelle de la découverte et des matchs."""
    user_id = int(user["id"])
    if payload.target_user_id == user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "impossible de se bloquer soi-même")
    if repo.get_user(conn, payload.target_user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "profil introuvable")

    repo.block_user(conn, user_id, payload.target_user_id)
    audit.record(
        conn,
        audit.USER_BLOCKED,
        user_id=user_id,
        ip=client_ip(request),
        detail=f"cible={payload.target_user_id}",
    )
    return {"blocked_user_id": payload.target_user_id}


@app.delete("/api/blocks/{target_user_id}", status_code=204, tags=["sécurité"])
def unblock(
    target_user_id: int,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> None:
    if not repo.unblock_user(conn, int(user["id"]), target_user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "blocage introuvable")
    audit.record(
        conn,
        audit.USER_UNBLOCKED,
        user_id=int(user["id"]),
        ip=client_ip(request),
        detail=f"cible={target_user_id}",
    )


@app.get("/api/blocks", tags=["sécurité"])
def blocks(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    rows = repo.rows_to_dicts(repo.list_blocks(conn, int(user["id"])))
    return {"count": len(rows), "results": rows}


@app.post("/api/reports", status_code=status.HTTP_201_CREATED, tags=["sécurité"])
def report(
    payload: ReportInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
    cfg: Annotated[Settings, Depends(config)],
) -> dict[str, Any]:
    """Signale un utilisateur à la modération, et le bloque dans la foulée."""
    user_id = int(user["id"])
    enforce_rate_limit(
        conn, "report", str(user_id), cfg.rate_limit_report, user_id=user_id, ip=client_ip(request)
    )
    if payload.target_user_id == user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "impossible de se signaler soi-même")
    if repo.get_user(conn, payload.target_user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "profil introuvable")

    report_id = repo.create_report(
        conn, user_id, payload.target_user_id, payload.reason, payload.details
    )
    # Signaler quelqu'un implique de ne plus vouloir le croiser.
    repo.block_user(conn, user_id, payload.target_user_id)
    audit.record(
        conn,
        audit.USER_REPORTED,
        user_id=user_id,
        ip=client_ip(request),
        detail=f"cible={payload.target_user_id} motif={payload.reason}",
    )
    return {"report_id": report_id, "blocked": True}


# --- Croisements ------------------------------------------------------------


@app.put("/api/me/crossings", tags=["croisements"])
def set_crossings(
    payload: CrossingSettingsInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    """Active ou coupe les croisements.

    Couper la fonction efface immédiatement les positions déjà enregistrées :
    désactiver doit vouloir dire « oubliez-moi », pas « mettez en pause ».
    """
    user_id = int(user["id"])
    repo.set_crossings_enabled(conn, user_id, payload.enabled)
    purged = 0
    if not payload.enabled:
        purged = conn.execute(
            "DELETE FROM location_pings WHERE user_id = ?", (user_id,)
        ).rowcount
    audit.record(
        conn,
        audit.CROSSINGS_TOGGLED,
        user_id=user_id,
        ip=client_ip(request),
        detail="active" if payload.enabled else "desactive",
    )
    return {"enabled": payload.enabled, "purged_pings": purged}


@app.post("/api/crossings/ping", tags=["croisements"])
def crossing_ping(
    payload: LocationPingInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
    cfg: Annotated[Settings, Depends(config)],
) -> dict[str, Any]:
    """Signale une position et détecte les croisements.

    La position est réduite à une cellule dès cette ligne : ni latitude ni
    longitude n'atteignent la base.
    """
    user_id = int(user["id"])
    if not user["crossings_enabled"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "activez les croisements dans vos réglages avant d'envoyer une position",
        )
    enforce_rate_limit(
        conn, "ping", str(user_id), cfg.rate_limit_ping, user_id=user_id, ip=client_ip(request)
    )

    cell = crossings.cell_id(payload.latitude, payload.longitude, cfg.crossing_cell_meters)
    bucket = crossings.time_bucket(conn, cfg.crossing_bucket_minutes)
    repo.record_ping(
        conn, user_id, cell, bucket, payload.speed_kmh, payload.heading_deg, payload.ride_id
    )

    neighbours = crossings.neighbouring_cells(
        payload.latitude, payload.longitude, cfg.crossing_cell_meters
    )
    new_crossings = 0
    for other in repo.find_nearby_pings(
        conn, user_id, neighbours, cfg.crossing_window_seconds
    ):
        context = crossings.classify_context(payload.speed_kmh, other["speed_kmh"])
        direction = crossings.classify_direction(payload.heading_deg, other["heading_deg"])
        # La balade rattachée au croisement est celle que les deux partagent.
        shared_ride = payload.ride_id if payload.ride_id == other["ride_id"] else None
        if repo.record_crossing(
            conn, user_id, int(other["user_id"]), cell, bucket, context, direction, shared_ride
        ):
            new_crossings += 1

    return {"recorded": True, "new_crossings": new_crossings}


@app.get("/api/crossings", tags=["croisements"])
def list_crossings(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
    cfg: Annotated[Settings, Depends(config)],
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    """Personnes croisées, de la plus récente à la plus ancienne.

    Le lieu remonté est le **centre de la cellule**, jamais la position réelle de
    l'autre personne — et le demandeur y était lui-même.
    """
    rows = repo.list_crossings(conn, int(user["id"]), limit)
    items = []
    for row in rows:
        latitude, longitude = crossings.cell_centre(row["cell_id"], cfg.crossing_cell_meters)
        items.append(
            {
                "crossing_id": row["crossing_id"],
                "times": row["times"],
                "last_seen_at": row["last_seen_at"],
                "context": row["context"],
                "direction": row["direction"],
                "ride_id": row["ride_id"],
                "summary": crossings.describe(row["context"], row["direction"], row["times"]),
                "area": {"latitude": latitude, "longitude": longitude},
                "salut_sent": bool(row["my_salut"]),
                "salut_received": bool(row["their_salut"]),
                "profile": other_profile_payload(row),
            }
        )
    return {"count": len(items), "results": items}


@app.post("/api/crossings/{crossing_id}/salut", tags=["croisements"])
def salut(
    crossing_id: int,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, Any]:
    """Le salut motard : un signe, sans engager la conversation.

    Si l'autre avait déjà salué, le salut est rendu et un match est créé — c'est
    l'équivalent en ligne du signe de la main qu'on se rend sur la route.
    """
    user_id = int(user["id"])
    crossing = repo.get_crossing_for_user(conn, crossing_id, user_id)
    if crossing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "croisement introuvable")

    other_id = (
        int(crossing["user_b_id"])
        if int(crossing["user_a_id"]) == user_id
        else int(crossing["user_a_id"])
    )
    if repo.is_blocked_either_way(conn, user_id, other_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "croisement introuvable")

    returned = repo.send_salut(conn, crossing_id, user_id)
    match_id = repo.create_match(conn, user_id, other_id) if returned else None
    return {"salut_sent": True, "salut_returned": returned, "match_id": match_id}


@app.delete("/api/crossings", tags=["croisements"])
def purge_crossings(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    user: Annotated[sqlite3.Row, Depends(current_user)],
) -> dict[str, int]:
    """Efface toutes les positions et tous les croisements de l'utilisateur."""
    user_id = int(user["id"])
    deleted = repo.purge_crossing_data(conn, user_id)
    audit.record(conn, audit.CROSSINGS_PURGED, user_id=user_id, ip=client_ip(request))
    return {"deleted_rows": deleted}


# --- Balades ----------------------------------------------------------------


def ride_payload(
    row: sqlite3.Row, *, viewer_id: int, is_participant: bool, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Balade sérialisée.

    Le point de rendez-vous exact n'est donné qu'aux participants acceptés et à
    l'organisateur : une balade ouverte ne doit pas publier l'adresse précise
    d'un rendez-vous à qui n'y va pas.
    """
    data = dict(row)
    data["bike_categories"] = parse_styles(data.get("bike_categories"))
    data["is_organiser"] = int(row["organiser_id"]) == viewer_id
    if not (is_participant or data["is_organiser"]):
        data.pop("start_latitude", None)
        data.pop("start_longitude", None)
    if extra:
        data.update(extra)
    return data


@app.post("/api/rides", status_code=status.HTTP_201_CREATED, tags=["balades"])
def create_ride(
    payload: RideInput,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    organiser: Annotated[sqlite3.Row, Depends(require_profile)],
    cfg: Annotated[Settings, Depends(config)],
) -> dict[str, Any]:
    """Crée une balade. L'autorisation choisie décide de qui la voit et la rejoint."""
    organiser_id = int(organiser["user_id"])
    enforce_rate_limit(
        conn, "ride", str(organiser_id), cfg.rate_limit_ride, user_id=organiser_id,
        ip=client_ip(request),
    )
    values = payload.model_dump()
    values["start_at"] = payload.start_at.isoformat()
    values["bike_categories"] = ",".join(payload.bike_categories)
    ride_id = repo.create_ride(conn, organiser_id, values)
    return ride_payload(
        repo.get_ride(conn, ride_id),
        viewer_id=organiser_id,
        is_participant=True,
        extra={"accepted_count": 1, "my_status": "accepte"},
    )


@app.get("/api/rides", tags=["balades"])
def list_rides(
    conn: Annotated[sqlite3.Connection, Depends(db)],
    viewer: Annotated[sqlite3.Row, Depends(require_profile)],
    max_distance_km: int | None = Query(default=None, ge=1, le=2000),
    pace: str | None = Query(default=None),
    route_type: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    """Balades à venir visibles par l'utilisateur, de la plus proche dans le temps."""
    viewer_id = int(viewer["user_id"])
    results: list[dict[str, Any]] = []
    for row in repo.list_visible_rides(conn, viewer_id):
        if pace and row["pace"] != pace.lower():
            continue
        if route_type and row["route_type"] != route_type.lower():
            continue
        distance = haversine_km(
            viewer["latitude"], viewer["longitude"], row["start_latitude"], row["start_longitude"]
        )
        if max_distance_km is not None and distance > max_distance_km:
            continue
        results.append(
            ride_payload(
                row,
                viewer_id=viewer_id,
                is_participant=row["my_status"] == "accepte",
                extra={
                    "accepted_count": row["accepted_count"],
                    "my_status": row["my_status"],
                    "organiser_name": row["organiser_name"],
                    # Distinct de `distance_km`, qui est la longueur du parcours.
                    "distance_from_you_km": round(distance, 1),
                    "spots_left": max(0, int(row["max_participants"]) - int(row["accepted_count"])),
                },
            )
        )
    return {"count": len(results), "results": results[:limit]}


@app.get("/api/rides/{ride_id}", tags=["balades"])
def read_ride(
    ride_id: int,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    viewer: Annotated[sqlite3.Row, Depends(require_profile)],
) -> dict[str, Any]:
    viewer_id = int(viewer["user_id"])
    ride = _visible_ride_or_404(conn, ride_id, viewer_id)
    participation = repo.get_participation(conn, ride_id, viewer_id)
    my_status = participation["status"] if participation else None
    return ride_payload(
        ride,
        viewer_id=viewer_id,
        is_participant=my_status == "accepte",
        extra={
            "my_status": my_status,
            "accepted_count": repo.count_accepted_participants(conn, ride_id),
            "participants": [
                dict(row)
                for row in repo.list_ride_participants(conn, ride_id)
                # Les demandes en attente ne regardent que l'organisateur.
                if row["status"] == "accepte" or int(ride["organiser_id"]) == viewer_id
            ],
        },
    )


def _visible_ride_or_404(
    conn: sqlite3.Connection, ride_id: int, viewer_id: int
) -> sqlite3.Row:
    """Récupère une balade que ce visiteur a le droit de voir.

    Passe par la même requête que la liste, pour qu'aucune règle d'autorisation
    ne puisse diverger entre les deux chemins.
    """
    visible = {int(row["id"]) for row in repo.list_visible_rides(conn, viewer_id)}
    ride = repo.get_ride(conn, ride_id)
    if ride is None or (ride_id not in visible and int(ride["organiser_id"]) != viewer_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "balade introuvable")
    return ride


@app.post("/api/rides/{ride_id}/join", tags=["balades"])
def join_ride(
    ride_id: int,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    viewer: Annotated[sqlite3.Row, Depends(require_profile)],
) -> dict[str, Any]:
    """Rejoint une balade, ou dépose une demande si l'organisateur doit valider."""
    viewer_id = int(viewer["user_id"])
    ride = _visible_ride_or_404(conn, ride_id, viewer_id)

    if ride["status"] != "ouverte":
        raise HTTPException(status.HTTP_409_CONFLICT, "cette balade est annulée")
    if int(ride["organiser_id"]) == viewer_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "vous organisez déjà cette balade")

    # Une balade « sur-demande » place la personne en attente de validation.
    requested = ride["visibility"] == "sur-demande"
    if not requested and repo.count_accepted_participants(conn, ride_id) >= int(
        ride["max_participants"]
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "cette balade est complète")

    new_status = "demande" if requested else "accepte"
    repo.join_ride(conn, ride_id, viewer_id, new_status)
    return {"ride_id": ride_id, "status": new_status}


@app.delete("/api/rides/{ride_id}/join", status_code=204, tags=["balades"])
def leave_ride(
    ride_id: int,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    viewer: Annotated[sqlite3.Row, Depends(require_profile)],
) -> None:
    viewer_id = int(viewer["user_id"])
    ride = repo.get_ride(conn, ride_id)
    if ride is not None and int(ride["organiser_id"]) == viewer_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "un organisateur ne peut pas quitter sa balade, il doit l'annuler",
        )
    if not repo.leave_ride(conn, ride_id, viewer_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "participation introuvable")


@app.post("/api/rides/{ride_id}/participants/{participant_id}", tags=["balades"])
def decide_participation(
    ride_id: int,
    participant_id: int,
    payload: ParticipationDecisionInput,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    viewer: Annotated[sqlite3.Row, Depends(require_profile)],
) -> dict[str, Any]:
    """Accepte ou refuse une demande — réservé à l'organisateur."""
    viewer_id = int(viewer["user_id"])
    ride = repo.get_ride(conn, ride_id)
    if ride is None or int(ride["organiser_id"]) != viewer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "balade introuvable")

    if payload.decision == "accepte" and repo.count_accepted_participants(conn, ride_id) >= int(
        ride["max_participants"]
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "cette balade est complète")
    if not repo.set_participation_status(conn, ride_id, participant_id, payload.decision):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "demande introuvable")
    return {"ride_id": ride_id, "user_id": participant_id, "status": payload.decision}


@app.delete("/api/rides/{ride_id}", status_code=204, tags=["balades"])
def cancel_ride(
    ride_id: int,
    conn: Annotated[sqlite3.Connection, Depends(db)],
    viewer: Annotated[sqlite3.Row, Depends(current_user)],
) -> None:
    """Annule une balade — réservé à l'organisateur."""
    if not repo.cancel_ride(conn, ride_id, int(viewer["id"])):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "balade introuvable")


# --- Interface web ----------------------------------------------------------

if settings.serve_web_client and STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        """Servi depuis la racine, et pas depuis /static.

        La portée d'un service worker est limitée au répertoire qui le sert :
        publié sous /static/, il ne contrôlerait pas la page d'accueil et
        l'application ne serait pas installable.
        """
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json"
        )
