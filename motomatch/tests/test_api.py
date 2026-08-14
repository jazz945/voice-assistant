"""Tests de bout en bout de l'API MotoMatch."""

from __future__ import annotations

from copy import deepcopy


def other_profile(base: dict, **overrides) -> dict:
    profile = deepcopy(base)
    profile.update(overrides)
    return profile


# --- Métadonnées et authentification ---------------------------------------


def test_meta_lists_allowed_values(client):
    body = client.get("/api/meta").json()
    assert "roadster" in body["bike_categories"]
    assert "col" in body["riding_styles"]
    assert body["pace_levels"][0] == "tranquille"


def test_register_then_login(client):
    created = client.post(
        "/api/auth/register", json={"email": "A@Moto.example.com", "password": "roadtrip2024"}
    )
    assert created.status_code == 201
    assert created.json()["has_profile"] is False

    # L'e-mail est normalisé en minuscules : la casse ne bloque pas la connexion.
    logged = client.post(
        "/api/auth/login", json={"email": "a@moto.example.com", "password": "roadtrip2024"}
    )
    assert logged.status_code == 200
    assert logged.json()["token"]


def test_register_rejects_duplicate_email(client, register):
    register("dup@moto.example.com")
    again = client.post(
        "/api/auth/register", json={"email": "dup@moto.example.com", "password": "roadtrip2024"}
    )
    assert again.status_code == 409


def test_register_rejects_short_password(client):
    response = client.post("/api/auth/register", json={"email": "x@moto.example.com", "password": "court"})
    assert response.status_code == 422


def test_login_with_wrong_password_fails(client, register):
    register("who@moto.example.com")
    response = client.post(
        "/api/auth/login", json={"email": "who@moto.example.com", "password": "mauvaispass"}
    )
    assert response.status_code == 401


def test_protected_route_requires_token(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer faux"}).status_code == 401


def test_logout_invalidates_the_token(client, register):
    account = register("bye@moto.example.com")
    assert client.post("/api/auth/logout", headers=account["headers"]).status_code == 204
    assert client.get("/api/me", headers=account["headers"]).status_code == 401


# --- Profil -----------------------------------------------------------------


def test_profile_roundtrip(client, register, rider_profile):
    account = register("camille@moto.example.com", profile=rider_profile)
    me = client.get("/api/me", headers=account["headers"]).json()
    assert me["profile"]["display_name"] == "Camille"
    assert me["profile"]["riding_styles"] == ["balade", "col"]
    assert me["profile"]["has_passenger_seat"] is True
    assert me["profile"]["age"] > 18


def test_profile_update_replaces_previous_values(client, register, rider_profile):
    account = register("maj@moto.example.com", profile=rider_profile)
    updated = client.put(
        "/api/me/profile",
        json=other_profile(rider_profile, city="Grenoble", bike_category="trail"),
        headers=account["headers"],
    )
    assert updated.status_code == 200
    assert updated.json()["city"] == "Grenoble"
    assert updated.json()["bike_category"] == "trail"


def test_profile_rejects_unknown_category(client, register, rider_profile):
    account = register("bad@moto.example.com")
    response = client.put(
        "/api/me/profile",
        json=other_profile(rider_profile, bike_category="tracteur"),
        headers=account["headers"],
    )
    assert response.status_code == 422


def test_profile_rejects_unknown_style(client, register, rider_profile):
    account = register("style@moto.example.com")
    response = client.put(
        "/api/me/profile",
        json=other_profile(rider_profile, riding_styles=["parapente"]),
        headers=account["headers"],
    )
    assert response.status_code == 422


def test_profile_rejects_minors(client, register, rider_profile):
    account = register("jeune@moto.example.com")
    response = client.put(
        "/api/me/profile",
        json=other_profile(rider_profile, birth_year=2020),
        headers=account["headers"],
    )
    assert response.status_code == 422


def test_discover_requires_a_profile(client, register):
    account = register("sansprofil@moto.example.com")
    assert client.get("/api/discover", headers=account["headers"]).status_code == 409


# --- Découverte -------------------------------------------------------------


def test_discover_ranks_by_compatibility(client, register, rider_profile):
    viewer = register("viewer@moto.example.com", profile=rider_profile)
    register(
        "jumeau@moto.example.com",
        profile=other_profile(rider_profile, display_name="Jumeau"),
    )
    register(
        "oppose@moto.example.com",
        profile=other_profile(
            rider_profile,
            display_name="Oppose",
            latitude=48.8566,
            longitude=2.3522,
            bike_category="custom",
            pace="tranquille",
            engine_cc=125,
            riding_styles=["urbain"],
            max_travel_km=30,
        ),
    )

    results = client.get("/api/discover", headers=viewer["headers"]).json()["results"]
    assert [item["profile"]["display_name"] for item in results] == ["Jumeau", "Oppose"]
    assert results[0]["score"] > results[1]["score"]
    assert results[0]["breakdown"]["styles"] == 1.0


def test_discover_excludes_self(client, register, rider_profile):
    viewer = register("solo@moto.example.com", profile=rider_profile)
    results = client.get("/api/discover", headers=viewer["headers"]).json()["results"]
    assert all(item["profile"]["user_id"] != viewer["user_id"] for item in results)


def test_discover_filters_by_distance_age_and_category(client, register, rider_profile):
    viewer = register("filtre@moto.example.com", profile=rider_profile)
    register("proche@moto.example.com", profile=other_profile(rider_profile, display_name="Proche"))
    register(
        "loin@moto.example.com",
        profile=other_profile(
            rider_profile, display_name="Loin", latitude=48.8566, longitude=2.3522
        ),
    )

    near_only = client.get(
        "/api/discover?max_distance_km=50", headers=viewer["headers"]
    ).json()["results"]
    assert [item["profile"]["display_name"] for item in near_only] == ["Proche"]

    by_category = client.get(
        "/api/discover?categories=trail", headers=viewer["headers"]
    ).json()["results"]
    assert by_category == []

    by_style = client.get("/api/discover?styles=col", headers=viewer["headers"]).json()["results"]
    assert len(by_style) == 2

    older_only = client.get("/api/discover?min_age=90", headers=viewer["headers"]).json()["results"]
    assert older_only == []


def test_discover_rejects_inverted_age_range(client, register, rider_profile):
    viewer = register("age@moto.example.com", profile=rider_profile)
    response = client.get("/api/discover?min_age=40&max_age=25", headers=viewer["headers"])
    assert response.status_code == 400


def test_discover_honours_limit(client, register, rider_profile):
    viewer = register("limite@moto.example.com", profile=rider_profile)
    for index in range(3):
        register(f"r{index}@moto.example.com", profile=other_profile(rider_profile, display_name=f"R{index}"))
    body = client.get("/api/discover?limit=2", headers=viewer["headers"]).json()
    assert body["count"] == 3
    assert len(body["results"]) == 2


# --- Swipes et matchs -------------------------------------------------------


def test_mutual_like_creates_a_match(client, register, rider_profile):
    alice = register("alice@moto.example.com", profile=rider_profile)
    bob = register("bob@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))

    first = client.post(
        "/api/swipes",
        json={"target_user_id": bob["user_id"], "direction": "like"},
        headers=alice["headers"],
    ).json()
    assert first == {"matched": False, "match_id": None}

    second = client.post(
        "/api/swipes",
        json={"target_user_id": alice["user_id"], "direction": "like"},
        headers=bob["headers"],
    ).json()
    assert second["matched"] is True
    assert second["match_id"] is not None

    for account in (alice, bob):
        matches = client.get("/api/matches", headers=account["headers"]).json()
        assert matches["count"] == 1


def test_pass_does_not_create_a_match(client, register, rider_profile):
    alice = register("a2@moto.example.com", profile=rider_profile)
    bob = register("b2@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))

    client.post(
        "/api/swipes",
        json={"target_user_id": alice["user_id"], "direction": "like"},
        headers=bob["headers"],
    )
    result = client.post(
        "/api/swipes",
        json={"target_user_id": bob["user_id"], "direction": "pass"},
        headers=alice["headers"],
    ).json()
    assert result["matched"] is False
    assert client.get("/api/matches", headers=alice["headers"]).json()["count"] == 0


def test_swiped_profiles_leave_the_deck(client, register, rider_profile):
    alice = register("a3@moto.example.com", profile=rider_profile)
    bob = register("b3@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))

    client.post(
        "/api/swipes",
        json={"target_user_id": bob["user_id"], "direction": "pass"},
        headers=alice["headers"],
    )
    assert client.get("/api/discover", headers=alice["headers"]).json()["results"] == []


def test_cannot_swipe_on_self(client, register, rider_profile):
    alice = register("a4@moto.example.com", profile=rider_profile)
    response = client.post(
        "/api/swipes",
        json={"target_user_id": alice["user_id"], "direction": "like"},
        headers=alice["headers"],
    )
    assert response.status_code == 400


def test_swipe_on_unknown_user_is_404(client, register, rider_profile):
    alice = register("a5@moto.example.com", profile=rider_profile)
    response = client.post(
        "/api/swipes",
        json={"target_user_id": 9999, "direction": "like"},
        headers=alice["headers"],
    )
    assert response.status_code == 404


def test_swipe_rejects_invalid_direction(client, register, rider_profile):
    alice = register("a6@moto.example.com", profile=rider_profile)
    bob = register("b6@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))
    response = client.post(
        "/api/swipes",
        json={"target_user_id": bob["user_id"], "direction": "peut-etre"},
        headers=alice["headers"],
    )
    assert response.status_code == 422


def test_repeated_mutual_like_is_idempotent(client, register, rider_profile):
    alice = register("a7@moto.example.com", profile=rider_profile)
    bob = register("b7@moto.example.com", profile=other_profile(rider_profile, display_name="Bob"))
    payload = {"target_user_id": bob["user_id"], "direction": "like"}

    client.post("/api/swipes", json=payload, headers=alice["headers"])
    client.post(
        "/api/swipes",
        json={"target_user_id": alice["user_id"], "direction": "like"},
        headers=bob["headers"],
    )
    again = client.post("/api/swipes", json=payload, headers=alice["headers"]).json()

    assert again["matched"] is True
    assert client.get("/api/matches", headers=alice["headers"]).json()["count"] == 1


# --- Messagerie -------------------------------------------------------------


def matched_pair(client, register, rider_profile, suffix: str):
    alice = register(f"m1{suffix}@moto.example.com", profile=rider_profile)
    bob = register(
        f"m2{suffix}@moto.example.com", profile=other_profile(rider_profile, display_name="Bob")
    )
    client.post(
        "/api/swipes",
        json={"target_user_id": bob["user_id"], "direction": "like"},
        headers=alice["headers"],
    )
    result = client.post(
        "/api/swipes",
        json={"target_user_id": alice["user_id"], "direction": "like"},
        headers=bob["headers"],
    ).json()
    return alice, bob, result["match_id"]


def test_messages_are_exchanged_within_a_match(client, register, rider_profile):
    alice, bob, match_id = matched_pair(client, register, rider_profile, "a")

    sent = client.post(
        f"/api/matches/{match_id}/messages",
        json={"body": "Sortie dans le Vercors dimanche ?"},
        headers=alice["headers"],
    )
    assert sent.status_code == 201

    client.post(
        f"/api/matches/{match_id}/messages",
        json={"body": "Partant, départ 8h."},
        headers=bob["headers"],
    )

    thread = client.get(f"/api/matches/{match_id}/messages", headers=bob["headers"]).json()
    assert [message["body"] for message in thread["results"]] == [
        "Sortie dans le Vercors dimanche ?",
        "Partant, départ 8h.",
    ]
    assert thread["results"][0]["sender_id"] == alice["user_id"]


def test_last_message_appears_in_match_list(client, register, rider_profile):
    alice, _, match_id = matched_pair(client, register, rider_profile, "b")
    client.post(
        f"/api/matches/{match_id}/messages",
        json={"body": "On se croise au col ?"},
        headers=alice["headers"],
    )
    matches = client.get("/api/matches", headers=alice["headers"]).json()["results"]
    assert matches[0]["last_message"] == "On se croise au col ?"


def test_outsider_cannot_read_or_write_a_conversation(client, register, rider_profile):
    _, _, match_id = matched_pair(client, register, rider_profile, "c")
    intruder = register("intrus@moto.example.com", profile=rider_profile)

    assert (
        client.get(f"/api/matches/{match_id}/messages", headers=intruder["headers"]).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/matches/{match_id}/messages",
            json={"body": "coucou"},
            headers=intruder["headers"],
        ).status_code
        == 404
    )


def test_blank_message_is_rejected(client, register, rider_profile):
    alice, _, match_id = matched_pair(client, register, rider_profile, "d")
    response = client.post(
        f"/api/matches/{match_id}/messages",
        json={"body": "   "},
        headers=alice["headers"],
    )
    assert response.status_code == 400
