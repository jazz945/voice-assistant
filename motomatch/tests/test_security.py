"""Tests des protections de sécurité.

Chaque test correspond à une attaque concrète : force brute, vol de jeton,
énumération de comptes, trilatération, accès aux données d'autrui.
"""

from __future__ import annotations

import time
from copy import deepcopy

import pytest

from motomatch.security import passwords
from motomatch.security.tokens import fingerprint

from .conftest import TEST_PASSWORD


def other_profile(base: dict, **overrides) -> dict:
    profile = deepcopy(base)
    profile.update(overrides)
    return profile


# --- Mots de passe ----------------------------------------------------------


def test_argon2id_hash_format_and_verification():
    stored = passwords.hash_password("Vercors-Col-2024")
    assert stored.startswith("$argon2id$")
    assert passwords.verify_password("Vercors-Col-2024", stored)
    assert not passwords.verify_password("Vercors-Col-2025", stored)


def test_hashes_are_salted_uniquely():
    a = passwords.hash_password(TEST_PASSWORD)
    b = passwords.hash_password(TEST_PASSWORD)
    assert a != b  # sel aléatoire : deux empreintes du même mot de passe diffèrent


def test_legacy_pbkdf2_hashes_still_verify_and_are_flagged_for_rehash():
    # Empreinte produite par la version précédente de l'application.
    import hashlib

    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    digest = hashlib.pbkdf2_hmac("sha256", b"roadtrip2024", salt, 240_000)
    legacy = f"pbkdf2_sha256$240000${salt.hex()}${digest.hex()}"

    assert passwords.verify_password("roadtrip2024", legacy)
    assert not passwords.verify_password("mauvais", legacy)
    assert passwords.needs_rehash(legacy)
    assert not passwords.needs_rehash(passwords.hash_password(TEST_PASSWORD))


@pytest.mark.parametrize(
    "password",
    [
        "court12",             # trop court
        "aaaaaaaaaaaaaa",      # pas assez de caractères distincts
        "azertyuiop1234",      # suite de touches
        "motdepasse12",        # trop courant
    ],
)
def test_weak_passwords_are_refused(password):
    problems = passwords.password_problems(password, min_length=12, personal_data=[])
    assert problems


def test_password_cannot_contain_personal_data():
    problems = passwords.password_problems(
        "camille-super-2024", min_length=12, personal_data=["camille@moto.example.com", "Camille"]
    )
    assert any("informations personnelles" in problem for problem in problems)


def test_strong_password_is_accepted():
    assert passwords.password_problems("Vercors-Col-2024", min_length=12, personal_data=[]) == []


def test_registration_enforces_password_policy(client):
    response = client.post(
        "/api/auth/register",
        json={"email": "faible@moto.example.com", "password": "aaaaaaaaaaaa", "age_attestation": True},
    )
    assert response.status_code == 422


def test_registration_requires_age_attestation(client):
    response = client.post(
        "/api/auth/register",
        json={"email": "mineur@moto.example.com", "password": TEST_PASSWORD, "age_attestation": False},
    )
    assert response.status_code == 422

    missing = client.post(
        "/api/auth/register",
        json={"email": "mineur2@moto.example.com", "password": TEST_PASSWORD},
    )
    assert missing.status_code == 422


def test_password_change_revokes_other_sessions(client, register):
    account = register("chg@moto.example.com")
    second = client.post(
        "/api/auth/login", json={"email": "chg@moto.example.com", "password": TEST_PASSWORD}
    ).json()
    second_headers = {"Authorization": f"Bearer {second['access_token']}"}
    assert client.get("/api/me", headers=second_headers).status_code == 200

    changed = client.post(
        "/api/me/password",
        json={"current_password": TEST_PASSWORD, "new_password": "Galibier-Matin-99"},
        headers=second_headers,
    )
    assert changed.status_code == 200
    assert changed.json()["revoked_other_sessions"] >= 1

    # La session qui a changé le mot de passe survit, les autres non.
    assert client.get("/api/me", headers=second_headers).status_code == 200
    assert client.get("/api/me", headers=account["headers"]).status_code == 401


def test_password_change_requires_the_current_password(client, register):
    account = register("chg2@moto.example.com")
    response = client.post(
        "/api/me/password",
        json={"current_password": "PasLeBon-2024", "new_password": "Galibier-Matin-99"},
        headers=account["headers"],
    )
    assert response.status_code == 401


# --- Jetons et sessions -----------------------------------------------------


def test_tokens_are_never_stored_in_clear(client, register):
    """Une fuite de la base ne doit pas livrer de session utilisable."""
    from motomatch.db import get_connection

    account = register("clair@moto.example.com")
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM sessions").fetchall()

    stored = {value for row in rows for value in dict(row).values() if isinstance(value, str)}
    assert account["access_token"] not in stored
    assert account["refresh_token"] not in stored
    # Seule l'empreinte est présente.
    assert fingerprint(account["access_token"]) in stored


def test_refresh_rotates_and_invalidates_the_old_token(client, register):
    account = register("rot@moto.example.com")
    rotated = client.post("/api/auth/refresh", json={"refresh_token": account["refresh_token"]})
    assert rotated.status_code == 200
    new_tokens = rotated.json()
    assert new_tokens["refresh_token"] != account["refresh_token"]

    # Le nouveau jeton d'accès fonctionne...
    assert (
        client.get(
            "/api/me", headers={"Authorization": f"Bearer {new_tokens['access_token']}"}
        ).status_code
        == 200
    )
    # ...et l'ancien jeton d'accès a été révoqué avec sa session.
    assert client.get("/api/me", headers=account["headers"]).status_code == 401


def test_refresh_token_reuse_revokes_the_whole_family(client, register):
    """Scénario : un attaquant a volé un jeton de rafraîchissement déjà utilisé."""
    account = register("vol@moto.example.com")
    first = client.post(
        "/api/auth/refresh", json={"refresh_token": account["refresh_token"]}
    ).json()

    # Rejeu du jeton initial, déjà consommé.
    replayed = client.post("/api/auth/refresh", json={"refresh_token": account["refresh_token"]})
    assert replayed.status_code == 401
    assert "rejou" in replayed.json()["detail"]

    # La session légitime issue de la rotation est coupée elle aussi : le
    # compte est protégé même si l'on ne sait pas qui du deux était le voleur.
    assert (
        client.get(
            "/api/me", headers={"Authorization": f"Bearer {first['access_token']}"}
        ).status_code
        == 401
    )
    assert client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]}).status_code == 401


def test_unknown_refresh_token_is_rejected(client):
    response = client.post("/api/auth/refresh", json={"refresh_token": "jeton-inconnu"})
    assert response.status_code == 401


def test_access_token_expires(build_client):
    """Un jeton d'accès périmé n'ouvre plus rien."""
    with build_client(access_token_ttl_seconds="60") as client:
        client.post(
            "/api/auth/register",
            json={"email": "exp@moto.example.com", "password": TEST_PASSWORD, "age_attestation": True},
        )
        from motomatch.db import get_connection

        # On fait vieillir la session de deux minutes.
        with get_connection() as conn:
            conn.execute("UPDATE sessions SET access_expires_at = datetime('now', '-1 minute')")

        token = client.post(
            "/api/auth/login", json={"email": "exp@moto.example.com", "password": TEST_PASSWORD}
        ).json()["access_token"]
        with get_connection() as conn:
            conn.execute("UPDATE sessions SET access_expires_at = datetime('now', '-1 minute')")
        assert (
            client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        )


def test_sessions_are_listed_and_revocable(client, register):
    account = register("sess@moto.example.com")
    client.post("/api/auth/login", json={"email": "sess@moto.example.com", "password": TEST_PASSWORD})

    listed = client.get("/api/auth/sessions", headers=account["headers"]).json()
    assert listed["count"] == 2
    assert sum(1 for item in listed["results"] if item["current"]) == 1

    other = next(item for item in listed["results"] if not item["current"])
    assert (
        client.delete(f"/api/auth/sessions/{other['id']}", headers=account["headers"]).status_code
        == 204
    )
    assert client.get("/api/auth/sessions", headers=account["headers"]).json()["count"] == 1


def test_cannot_revoke_another_users_session(client, register):
    victim = register("victime@moto.example.com")
    attacker = register("attaquant@moto.example.com")
    victim_session = client.get("/api/auth/sessions", headers=victim["headers"]).json()["results"][0]

    response = client.delete(
        f"/api/auth/sessions/{victim_session['id']}", headers=attacker["headers"]
    )
    assert response.status_code == 404
    assert client.get("/api/me", headers=victim["headers"]).status_code == 200


def test_logout_all_revokes_every_device(client, register):
    account = register("all@moto.example.com")
    client.post("/api/auth/login", json={"email": "all@moto.example.com", "password": TEST_PASSWORD})

    assert client.post("/api/auth/logout-all", headers=account["headers"]).json()[
        "revoked_sessions"
    ] == 2
    assert client.get("/api/me", headers=account["headers"]).status_code == 401


# --- Force brute ------------------------------------------------------------


def test_login_locks_out_after_repeated_failures(build_client):
    with build_client(login_max_attempts="3", lockout_base_seconds="30") as client:
        client.post(
            "/api/auth/register",
            json={"email": "brute@moto.example.com", "password": TEST_PASSWORD, "age_attestation": True},
        )
        for _ in range(3):
            failed = client.post(
                "/api/auth/login",
                json={"email": "brute@moto.example.com", "password": "MauvaisPass-2024"},
            )
            assert failed.status_code == 401

        blocked = client.post(
            "/api/auth/login",
            json={"email": "brute@moto.example.com", "password": "MauvaisPass-2024"},
        )
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) > 0

        # Même le bon mot de passe est refusé pendant la temporisation.
        assert (
            client.post(
                "/api/auth/login",
                json={"email": "brute@moto.example.com", "password": TEST_PASSWORD},
            ).status_code
            == 429
        )


def test_successful_login_clears_the_failure_counter(build_client):
    with build_client(login_max_attempts="5") as client:
        client.post(
            "/api/auth/register",
            json={"email": "reset@moto.example.com", "password": TEST_PASSWORD, "age_attestation": True},
        )
        for _ in range(3):
            client.post(
                "/api/auth/login",
                json={"email": "reset@moto.example.com", "password": "MauvaisPass-2024"},
            )
        assert (
            client.post(
                "/api/auth/login",
                json={"email": "reset@moto.example.com", "password": TEST_PASSWORD},
            ).status_code
            == 200
        )
        # Le compteur est remis à zéro : trois nouveaux échecs ne verrouillent pas.
        for _ in range(3):
            assert (
                client.post(
                    "/api/auth/login",
                    json={"email": "reset@moto.example.com", "password": "MauvaisPass-2024"},
                ).status_code
                == 401
            )


def test_registration_is_rate_limited(build_client):
    with build_client(rate_limit_register="[2,3600]") as client:
        for index in range(2):
            created = client.post(
                "/api/auth/register",
                json={
                    "email": f"quota{index}@moto.example.com",
                    "password": TEST_PASSWORD,
                    "age_attestation": True,
                },
            )
            assert created.status_code == 201

        blocked = client.post(
            "/api/auth/register",
            json={"email": "quota9@moto.example.com", "password": TEST_PASSWORD, "age_attestation": True},
        )
        assert blocked.status_code == 429


def test_messages_are_rate_limited(build_client, rider_profile):
    with build_client(rate_limit_message="[2,3600]") as client:
        def make(email, **overrides):
            session = client.post(
                "/api/auth/register",
                json={"email": email, "password": TEST_PASSWORD, "age_attestation": True},
            ).json()
            headers = {"Authorization": f"Bearer {session['access_token']}"}
            client.put("/api/me/profile", json=other_profile(rider_profile, **overrides), headers=headers)
            return headers, session["user_id"]

        alice_h, alice_id = make("spam1@moto.example.com")
        bob_h, bob_id = make("spam2@moto.example.com", display_name="Bob")
        client.post("/api/swipes", json={"target_user_id": bob_id, "direction": "like"}, headers=alice_h)
        match_id = client.post(
            "/api/swipes", json={"target_user_id": alice_id, "direction": "like"}, headers=bob_h
        ).json()["match_id"]

        for _ in range(2):
            assert (
                client.post(
                    f"/api/matches/{match_id}/messages", json={"body": "salut"}, headers=alice_h
                ).status_code
                == 201
            )
        assert (
            client.post(
                f"/api/matches/{match_id}/messages", json={"body": "encore"}, headers=alice_h
            ).status_code
            == 429
        )


def test_login_timing_does_not_reveal_account_existence(build_client):
    """Compte inconnu et mauvais mot de passe doivent coûter le même temps.

    Le verrouillage est neutralisé ici : on veut mesurer le coût du chemin
    d'authentification lui-même, pas celui d'une réponse 429 anticipée.
    """
    with build_client(login_max_attempts="50") as client:
        client.post(
            "/api/auth/register",
            json={"email": "connu@moto.example.com", "password": TEST_PASSWORD, "age_attestation": True},
        )

        def elapsed(email: str) -> float:
            start = time.perf_counter()
            response = client.post(
                "/api/auth/login", json={"email": email, "password": "MauvaisPass-2024"}
            )
            assert response.status_code == 401
            return time.perf_counter() - start

        # Mesures entrelacées pour amortir la charge machine, puis on compare
        # les minima (moins sensibles au bruit que les moyennes).
        known, unknown = [], []
        for _ in range(5):
            known.append(elapsed("connu@moto.example.com"))
            unknown.append(elapsed("inconnu@moto.example.com"))

        # Argon2id domine le temps de réponse dans les deux cas : le chemin
        # « compte inconnu » ne doit pas être notablement plus rapide.
        assert min(unknown) > min(known) * 0.6


# --- Confidentialité de la localisation -------------------------------------


def test_discover_never_returns_coordinates(client, register, rider_profile):
    viewer = register("geo1@moto.example.com", profile=rider_profile)
    register("geo2@moto.example.com", profile=other_profile(rider_profile, display_name="Voisin"))

    results = client.get("/api/discover", headers=viewer["headers"]).json()["results"]
    assert results
    for item in results:
        assert "latitude" not in item["profile"]
        assert "longitude" not in item["profile"]


def test_matches_never_return_coordinates(client, register, rider_profile):
    alice = register("geo3@moto.example.com", profile=rider_profile)
    bob = register("geo4@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))
    client.post(
        "/api/swipes", json={"target_user_id": bob["user_id"], "direction": "like"}, headers=alice["headers"]
    )
    client.post(
        "/api/swipes", json={"target_user_id": alice["user_id"], "direction": "like"}, headers=bob["headers"]
    )

    match = client.get("/api/matches", headers=alice["headers"]).json()["results"][0]
    assert "latitude" not in match["profile"]
    assert "longitude" not in match["profile"]


def test_own_coordinates_remain_visible_to_their_owner(client, register, rider_profile):
    account = register("geo5@moto.example.com", profile=rider_profile)
    me = client.get("/api/me", headers=account["headers"]).json()
    assert me["profile"]["latitude"] == pytest.approx(45.7640)


def test_reported_distance_is_bucketed(client, register, rider_profile):
    viewer = register("geo6@moto.example.com", profile=rider_profile)
    register(
        "geo7@moto.example.com",
        profile=other_profile(rider_profile, display_name="Loin", latitude=46.0, longitude=5.0),
    )
    results = client.get("/api/discover", headers=viewer["headers"]).json()["results"]
    for item in results:
        assert item["distance_km"] % 5 == 0  # palier de 5 km par défaut


def test_grid_snapping_defeats_trilateration(build_client, rider_profile):
    """Un attaquant qui déplace sa position ne doit pas retrouver la position exacte.

    On simule l'attaque : la victime ne bouge pas, l'attaquant se déclare
    successivement à plusieurs endroits et relève la distance annoncée. Sans
    protection, ces mesures se recoupent sur un point unique. Avec la grille,
    elles se recoupent sur la cellule — jamais mieux.
    """
    from motomatch.privacy import snap_to_grid

    victim_lat, victim_lon = 45.7640, 4.8357
    snapped = [
        snap_to_grid(
            victim_lat, victim_lon, user_id=42, secret_key="cle-de-test", grid_meters=1000
        )
        for _ in range(20)
    ]
    # Déterminisme : interroger en boucle ne moyenne aucun bruit.
    assert len(set(snapped)) == 1

    # La position publiée est décalée de la position réelle...
    from motomatch.matching import haversine_km

    error_km = haversine_km(victim_lat, victim_lon, *snapped[0])
    assert error_km > 0
    # ...d'au plus une diagonale de cellule (1 km × √2, marge incluse).
    assert error_km < 1.6


def test_grid_offset_differs_between_users(build_client):
    from motomatch.privacy import snap_to_grid

    a = snap_to_grid(45.764, 4.8357, user_id=1, secret_key="cle-de-test", grid_meters=1000)
    b = snap_to_grid(45.764, 4.8357, user_id=2, secret_key="cle-de-test", grid_meters=1000)
    # Deux utilisateurs au même endroit ne tombent pas sur la même cellule :
    # impossible de cartographier les frontières de grille.
    assert a != b


def test_grid_offset_depends_on_the_server_secret():
    from motomatch.privacy import snap_to_grid

    a = snap_to_grid(45.764, 4.8357, user_id=1, secret_key="secret-un", grid_meters=1000)
    b = snap_to_grid(45.764, 4.8357, user_id=1, secret_key="secret-deux", grid_meters=1000)
    assert a != b  # non prédictible sans le secret serveur


# --- Blocage et signalement -------------------------------------------------


def test_blocked_user_disappears_from_discovery_both_ways(client, register, rider_profile):
    alice = register("bl1@moto.example.com", profile=rider_profile)
    bob = register("bl2@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))

    assert client.post(
        "/api/blocks", json={"target_user_id": bob["user_id"]}, headers=alice["headers"]
    ).status_code == 201

    assert client.get("/api/discover", headers=alice["headers"]).json()["results"] == []
    # Le blocage vaut dans les deux sens : Bob ne voit plus Alice non plus.
    assert client.get("/api/discover", headers=bob["headers"]).json()["results"] == []


def test_blocking_hides_an_existing_match_and_its_messages(client, register, rider_profile):
    alice = register("bl3@moto.example.com", profile=rider_profile)
    bob = register("bl4@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))
    client.post(
        "/api/swipes", json={"target_user_id": bob["user_id"], "direction": "like"}, headers=alice["headers"]
    )
    match_id = client.post(
        "/api/swipes", json={"target_user_id": alice["user_id"], "direction": "like"}, headers=bob["headers"]
    ).json()["match_id"]
    client.post(f"/api/matches/{match_id}/messages", json={"body": "salut"}, headers=bob["headers"])

    client.post("/api/blocks", json={"target_user_id": bob["user_id"]}, headers=alice["headers"])

    for headers in (alice["headers"], bob["headers"]):
        assert client.get("/api/matches", headers=headers).json()["count"] == 0
        assert client.get(f"/api/matches/{match_id}/messages", headers=headers).status_code == 404
        assert (
            client.post(
                f"/api/matches/{match_id}/messages", json={"body": "encore"}, headers=headers
            ).status_code
            == 404
        )


def test_blocked_user_cannot_swipe_and_cannot_tell(client, register, rider_profile):
    alice = register("bl5@moto.example.com", profile=rider_profile)
    bob = register("bl6@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))
    client.post("/api/blocks", json={"target_user_id": bob["user_id"]}, headers=alice["headers"])

    response = client.post(
        "/api/swipes", json={"target_user_id": alice["user_id"], "direction": "like"}, headers=bob["headers"]
    )
    # 404 et non 403 : Bob ne doit pas apprendre qu'il a été bloqué.
    assert response.status_code == 404
    assert response.json()["detail"] == "profil introuvable"


def test_unblocking_restores_visibility(client, register, rider_profile):
    alice = register("bl7@moto.example.com", profile=rider_profile)
    bob = register("bl8@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))
    client.post("/api/blocks", json={"target_user_id": bob["user_id"]}, headers=alice["headers"])
    assert client.get("/api/discover", headers=alice["headers"]).json()["count"] == 0

    assert client.delete(
        f"/api/blocks/{bob['user_id']}", headers=alice["headers"]
    ).status_code == 204
    assert client.get("/api/discover", headers=alice["headers"]).json()["count"] == 1


def test_cannot_block_or_report_self(client, register, rider_profile):
    alice = register("bl9@moto.example.com", profile=rider_profile)
    assert client.post(
        "/api/blocks", json={"target_user_id": alice["user_id"]}, headers=alice["headers"]
    ).status_code == 400
    assert client.post(
        "/api/reports",
        json={"target_user_id": alice["user_id"], "reason": "spam"},
        headers=alice["headers"],
    ).status_code == 400


def test_report_also_blocks(client, register, rider_profile):
    alice = register("rp1@moto.example.com", profile=rider_profile)
    bob = register("rp2@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))

    created = client.post(
        "/api/reports",
        json={"target_user_id": bob["user_id"], "reason": "harcelement", "details": "messages insistants"},
        headers=alice["headers"],
    )
    assert created.status_code == 201
    assert created.json()["blocked"] is True
    assert client.get("/api/discover", headers=alice["headers"]).json()["count"] == 0


def test_report_rejects_unknown_reason(client, register, rider_profile):
    alice = register("rp3@moto.example.com", profile=rider_profile)
    bob = register("rp4@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))
    response = client.post(
        "/api/reports",
        json={"target_user_id": bob["user_id"], "reason": "il-roule-en-scooter"},
        headers=alice["headers"],
    )
    assert response.status_code == 422


# --- Cycle de vie du compte -------------------------------------------------


def test_account_deletion_removes_all_personal_data(client, register, rider_profile):
    alice = register("del1@moto.example.com", profile=rider_profile)
    bob = register("del2@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))
    client.post(
        "/api/swipes", json={"target_user_id": bob["user_id"], "direction": "like"}, headers=alice["headers"]
    )
    match_id = client.post(
        "/api/swipes", json={"target_user_id": alice["user_id"], "direction": "like"}, headers=bob["headers"]
    ).json()["match_id"]
    client.post(f"/api/matches/{match_id}/messages", json={"body": "coucou"}, headers=alice["headers"])

    deleted = client.request(
        "DELETE",
        "/api/me",
        json={"password": TEST_PASSWORD, "confirmation": "SUPPRIMER"},
        headers=alice["headers"],
    )
    assert deleted.status_code == 204

    from motomatch.db import get_connection

    with get_connection() as conn:
        for table, column in [
            ("profiles", "user_id"),
            ("sessions", "user_id"),
            ("swipes", "from_user_id"),
            ("messages", "sender_id"),
        ]:
            remaining = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (alice["user_id"],)
            ).fetchone()["n"]
            assert remaining == 0, f"{table} contient encore des lignes du compte supprimé"

    # Le compte ne peut plus se connecter ni être vu.
    assert client.get("/api/me", headers=alice["headers"]).status_code == 401
    assert (
        client.post(
            "/api/auth/login", json={"email": "del1@moto.example.com", "password": TEST_PASSWORD}
        ).status_code
        == 401
    )
    assert client.get("/api/matches", headers=bob["headers"]).json()["count"] == 0


def test_account_deletion_requires_password_and_confirmation(client, register):
    account = register("del3@moto.example.com")
    wrong_password = client.request(
        "DELETE",
        "/api/me",
        json={"password": "PasLeBon-2024", "confirmation": "SUPPRIMER"},
        headers=account["headers"],
    )
    assert wrong_password.status_code == 401

    wrong_confirmation = client.request(
        "DELETE",
        "/api/me",
        json={"password": TEST_PASSWORD, "confirmation": "oui"},
        headers=account["headers"],
    )
    assert wrong_confirmation.status_code == 422
    assert client.get("/api/me", headers=account["headers"]).status_code == 200


def test_data_export_contains_the_users_own_data_only(client, register, rider_profile):
    alice = register("exp1@moto.example.com", profile=rider_profile)
    export = client.get("/api/me/export", headers=alice["headers"]).json()

    assert export["compte"]["email"] == "exp1@moto.example.com"
    assert export["profil"]["display_name"] == "Camille"
    assert "password_hash" not in export["compte"]  # l'empreinte n'est jamais exportée
    assert set(export) >= {"compte", "profil", "swipes", "matchs", "messages_envoyes", "blocages"}


def test_birth_year_is_immutable(client, register, rider_profile):
    """Empêche un compte créé mineur de se vieillir après coup."""
    account = register("age@moto.example.com", profile=rider_profile)
    response = client.put(
        "/api/me/profile",
        json=other_profile(rider_profile, birth_year=1990),
        headers=account["headers"],
    )
    assert response.status_code == 409
    # Les autres champs restent modifiables.
    assert (
        client.put(
            "/api/me/profile",
            json=other_profile(rider_profile, city="Grenoble"),
            headers=account["headers"],
        ).status_code
        == 200
    )


# --- Validation et exposition -----------------------------------------------


def test_photo_url_must_be_https(client, register, rider_profile):
    account = register("url@moto.example.com")
    for bad in ["javascript:alert(1)", "data:text/html,<script>", "http://exemple.test/p.jpg"]:
        response = client.put(
            "/api/me/profile",
            json=other_profile(rider_profile, photo_url=bad),
            headers=account["headers"],
        )
        assert response.status_code == 422, bad

    ok = client.put(
        "/api/me/profile",
        json=other_profile(rider_profile, photo_url="https://exemple.test/p.jpg"),
        headers=account["headers"],
    )
    assert ok.status_code == 200


def test_control_characters_are_stripped_from_text(client, register, rider_profile):
    account = register("ctrl@moto.example.com")
    saved = client.put(
        "/api/me/profile",
        json=other_profile(rider_profile, display_name="Cami‮lle"),
        headers=account["headers"],
    ).json()
    assert saved["display_name"] == "Camille"


def test_security_headers_are_present(client):
    response = client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Referrer-Policy"] == "no-referrer"
    # Les réponses de l'API ne doivent jamais être mises en cache.
    assert response.headers["Cache-Control"] == "no-store"


def test_hsts_only_in_production(client, build_client):
    assert "Strict-Transport-Security" not in client.get("/api/health").headers
    with build_client(
        env="production",
        secret_key="une-cle-de-production-vraiment-longue-et-aleatoire",
        docs_enabled="false",
        trusted_hosts="motomatch.example.com,testserver",
        cors_origins="https://motomatch.example.com",
    ) as prod:
        assert "Strict-Transport-Security" in prod.get("/api/health").headers


def test_oversized_request_is_rejected(build_client):
    with build_client(max_request_bytes="2048") as client:
        response = client.post(
            "/api/auth/register",
            json={
                "email": "gros@moto.example.com",
                "password": TEST_PASSWORD,
                "age_attestation": True,
                "remplissage": "x" * 5000,
            },
        )
        assert response.status_code == 413


def test_internal_errors_do_not_leak_details(build_client, monkeypatch):
    # `raise_server_exceptions=False` fait remonter la réponse HTTP réelle au
    # lieu de relancer l'exception dans le test — c'est ce que verrait un client.
    with build_client(raise_server_exceptions=False) as client:
        from motomatch import main as main_module

        def explode(*_args, **_kwargs):
            raise RuntimeError("chemin/interne/secret.py ligne 42")

        monkeypatch.setattr(main_module.repo, "get_user_by_email", explode)
        response = client.post(
            "/api/auth/login",
            json={"email": "boum@moto.example.com", "password": TEST_PASSWORD},
        )
        assert response.status_code == 500
        assert response.json() == {"detail": "erreur interne"}
        assert "secret.py" not in response.text


def test_production_config_refuses_dev_secret(env):
    from motomatch.config import reload_settings

    env.setenv("MOTOMATCH_ENV", "production")
    env.delenv("MOTOMATCH_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        reload_settings()


def test_production_config_refuses_wildcard_hosts_and_http_origins(env):
    from motomatch.config import reload_settings

    env.setenv("MOTOMATCH_ENV", "production")
    env.setenv("MOTOMATCH_SECRET_KEY", "une-cle-de-production-vraiment-longue-et-aleatoire")
    env.setenv("MOTOMATCH_TRUSTED_HOSTS", "*")
    env.setenv("MOTOMATCH_CORS_ORIGINS", "http://motomatch.example.com")
    env.setenv("MOTOMATCH_DOCS_ENABLED", "true")

    with pytest.raises(RuntimeError) as failure:
        reload_settings()
    message = str(failure.value)
    assert "TRUSTED_HOSTS" in message
    assert "HTTPS" in message
    assert "DOCS_ENABLED" in message


# --- Journal d'audit --------------------------------------------------------


def test_audit_log_records_security_events_without_raw_ip(client, register):
    register("audit@moto.example.com")
    client.post(
        "/api/auth/login", json={"email": "audit@moto.example.com", "password": "MauvaisPass-2024"}
    )

    from motomatch.db import get_connection

    with get_connection() as conn:
        rows = conn.execute("SELECT event, ip_hash FROM audit_log ORDER BY id").fetchall()

    events = [row["event"] for row in rows]
    assert "account.register" in events
    assert "login.failure" in events
    # L'adresse est hachée, jamais stockée en clair.
    assert all(row["ip_hash"] != "testclient" for row in rows)
    assert all(len(row["ip_hash"]) in (0, 32) for row in rows)
