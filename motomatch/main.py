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

from . import audit
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
    AccountDeletionInput,
    BlockInput,
    Credentials,
    DiscoveryFilters,
    MessageInput,
    PasswordChangeInput,
    ProfileInput,
    RefreshInput,
    RegistrationInput,
    ReportInput,
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
    """Profil de l'utilisateur connecté : il voit ses propres coordonnées."""
    data = dict(row)
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


# --- Interface web ----------------------------------------------------------

if settings.serve_web_client and STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
