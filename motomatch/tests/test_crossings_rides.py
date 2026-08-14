"""Tests des croisements et des balades."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from motomatch import crossings
from motomatch.db import get_connection

from .conftest import TEST_PASSWORD

LYON = (45.7640, 4.8357)


def other_profile(base: dict, **overrides) -> dict:
    profile = deepcopy(base)
    profile.update(overrides)
    return profile


def in_days(days: int, hours: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=hours)).isoformat()


@pytest.fixture
def rider(client, register, rider_profile):
    """Crée un motard, avec croisements activés à la demande."""

    def _rider(name: str, *, crossings_on: bool = False, **profile_overrides):
        account = register(
            f"{name}@moto.example.com",
            profile=other_profile(rider_profile, display_name=name.capitalize(), **profile_overrides),
        )
        if crossings_on:
            response = client.put(
                "/api/me/crossings", json={"enabled": True}, headers=account["headers"]
            )
            assert response.status_code == 200
        return account

    return _rider


def ping(client, account, latitude, longitude, **kwargs):
    return client.post(
        "/api/crossings/ping",
        json={"latitude": latitude, "longitude": longitude, **kwargs},
        headers=account["headers"],
    )


# --- Classification (unitaire) ----------------------------------------------


def test_cell_id_is_stable_and_groups_nearby_points():
    a = crossings.cell_id(45.7640, 4.8357, 500)
    b = crossings.cell_id(45.7641, 4.8358, 500)  # ~13 m plus loin
    far = crossings.cell_id(45.8000, 4.9000, 500)
    assert a == b
    assert a != far


def test_neighbouring_cells_include_the_centre_and_eight_around():
    cells = crossings.neighbouring_cells(*LYON, 500)
    assert len(cells) == 9
    assert crossings.cell_id(*LYON, 500) in cells


def test_cell_centre_is_close_to_the_original_point():
    from motomatch.matching import haversine_km

    centre = crossings.cell_centre(crossings.cell_id(*LYON, 500), 500)
    # Le centre reste dans la cellule : au plus une demi-diagonale.
    assert haversine_km(*LYON, *centre) < 0.5


@pytest.mark.parametrize(
    "speed_a,speed_b,expected",
    [
        (90, 85, crossings.CONTEXT_ROAD),
        (0, 2, crossings.CONTEXT_STOPPED),
        (90, 0, crossings.CONTEXT_MIXED),
        (None, 90, crossings.CONTEXT_MIXED),
    ],
)
def test_context_classification(speed_a, speed_b, expected):
    assert crossings.classify_context(speed_a, speed_b) == expected


@pytest.mark.parametrize(
    "heading_a,heading_b,expected",
    [
        (0, 180, crossings.DIRECTION_OPPOSITE),
        (350, 175, crossings.DIRECTION_OPPOSITE),
        (90, 100, crossings.DIRECTION_SAME),
        (0, 90, crossings.DIRECTION_CROSSING),
        (None, 90, crossings.DIRECTION_UNKNOWN),
    ],
)
def test_direction_classification(heading_a, heading_b, expected):
    assert crossings.classify_direction(heading_a, heading_b) == expected


def test_describe_highlights_the_opposite_direction_crossing():
    text = crossings.describe(crossings.CONTEXT_ROAD, crossings.DIRECTION_OPPOSITE, 1)
    assert "sens inverse" in text
    assert "3 fois" in crossings.describe(crossings.CONTEXT_ROAD, crossings.DIRECTION_OPPOSITE, 3)


# --- Croisements : opt-in et confidentialité --------------------------------


def test_ping_refused_until_crossings_are_enabled(client, rider):
    account = rider("solo")
    assert ping(client, account, *LYON).status_code == 409

    client.put("/api/me/crossings", json={"enabled": True}, headers=account["headers"])
    assert ping(client, account, *LYON).status_code == 200


def test_no_raw_coordinates_are_ever_stored(client, rider):
    account = rider("geo", crossings_on=True)
    ping(client, account, 45.764012, 4.835734, speed_kmh=90, heading_deg=12)

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM location_pings").fetchone()
        columns = {description[0] for description in conn.execute(
            "SELECT * FROM location_pings"
        ).description}

    # Le schéma ne comporte même pas de colonne de coordonnées.
    assert "latitude" not in columns
    assert "longitude" not in columns
    assert "45.764" not in str(dict(row))


def test_crossing_is_detected_between_two_nearby_riders(client, rider):
    alice = rider("alice", crossings_on=True)
    bob = rider("bob", crossings_on=True)

    assert ping(client, alice, 45.7640, 4.8357, speed_kmh=88, heading_deg=10).json()[
        "new_crossings"
    ] == 0
    assert ping(client, bob, 45.7642, 4.8359, speed_kmh=92, heading_deg=190).json()[
        "new_crossings"
    ] == 1

    results = client.get("/api/crossings", headers=alice["headers"]).json()["results"]
    assert len(results) == 1
    assert results[0]["profile"]["display_name"] == "Bob"
    assert results[0]["direction"] == crossings.DIRECTION_OPPOSITE
    assert results[0]["context"] == crossings.CONTEXT_ROAD
    assert "sens inverse" in results[0]["summary"]


def test_crossing_requires_both_sides_to_have_opted_in(client, rider):
    watcher = rider("watcher", crossings_on=True)
    passive = rider("passive", crossings_on=False)

    ping(client, watcher, *LYON, speed_kmh=80, heading_deg=0)
    # Le passif ne peut même pas envoyer de position.
    assert ping(client, passive, *LYON).status_code == 409
    assert client.get("/api/crossings", headers=watcher["headers"]).json()["count"] == 0


def test_crossing_response_never_leaks_the_grid_cell(client, rider):
    alice = rider("alice2", crossings_on=True)
    bob = rider("bob2", crossings_on=True)
    ping(client, alice, *LYON, speed_kmh=80, heading_deg=0)
    ping(client, bob, *LYON, speed_kmh=80, heading_deg=180)

    body = client.get("/api/crossings", headers=alice["headers"]).text
    assert "cell_id" not in body
    # Seule la zone approximative est exposée.
    item = client.get("/api/crossings", headers=alice["headers"]).json()["results"][0]
    assert set(item["area"]) == {"latitude", "longitude"}
    assert "latitude" not in item["profile"]


def test_repeated_pings_in_the_same_window_create_one_crossing(client, rider):
    alice = rider("alice3", crossings_on=True)
    bob = rider("bob3", crossings_on=True)
    for _ in range(4):
        ping(client, alice, *LYON, speed_kmh=0)
        ping(client, bob, *LYON, speed_kmh=0)

    results = client.get("/api/crossings", headers=alice["headers"]).json()["results"]
    assert len(results) == 1
    assert results[0]["times"] == 1  # même cellule, même créneau


def test_blocked_riders_never_cross(client, rider):
    alice = rider("alice4", crossings_on=True)
    bob = rider("bob4", crossings_on=True)
    client.post("/api/blocks", json={"target_user_id": bob["user_id"]}, headers=alice["headers"])

    ping(client, alice, *LYON, speed_kmh=80, heading_deg=0)
    assert ping(client, bob, *LYON, speed_kmh=80, heading_deg=180).json()["new_crossings"] == 0
    assert client.get("/api/crossings", headers=alice["headers"]).json()["count"] == 0


def test_distant_riders_do_not_cross(client, rider):
    alice = rider("alice5", crossings_on=True)
    bob = rider("bob5", crossings_on=True)
    ping(client, alice, *LYON, speed_kmh=80)
    # Paris : très au-delà du voisinage de cellule.
    assert ping(client, bob, 48.8566, 2.3522, speed_kmh=80).json()["new_crossings"] == 0


# --- Le salut motard --------------------------------------------------------


def test_reciprocal_salut_creates_a_match(client, rider):
    alice = rider("alice6", crossings_on=True)
    bob = rider("bob6", crossings_on=True)
    ping(client, alice, *LYON, speed_kmh=80, heading_deg=0)
    ping(client, bob, *LYON, speed_kmh=80, heading_deg=180)

    crossing_id = client.get("/api/crossings", headers=alice["headers"]).json()["results"][0][
        "crossing_id"
    ]

    first = client.post(f"/api/crossings/{crossing_id}/salut", headers=alice["headers"]).json()
    assert first == {"salut_sent": True, "salut_returned": False, "match_id": None}
    assert client.get("/api/matches", headers=alice["headers"]).json()["count"] == 0

    second = client.post(f"/api/crossings/{crossing_id}/salut", headers=bob["headers"]).json()
    assert second["salut_returned"] is True
    assert second["match_id"] is not None
    assert client.get("/api/matches", headers=alice["headers"]).json()["count"] == 1

    # L'état du salut remonte des deux côtés.
    item = client.get("/api/crossings", headers=alice["headers"]).json()["results"][0]
    assert item["salut_sent"] and item["salut_received"]


def test_cannot_salut_a_crossing_of_someone_else(client, rider):
    alice = rider("alice7", crossings_on=True)
    bob = rider("bob7", crossings_on=True)
    intruder = rider("intrus", crossings_on=True)
    ping(client, alice, *LYON, speed_kmh=80)
    ping(client, bob, *LYON, speed_kmh=80)
    crossing_id = client.get("/api/crossings", headers=alice["headers"]).json()["results"][0][
        "crossing_id"
    ]

    assert (
        client.post(
            f"/api/crossings/{crossing_id}/salut", headers=intruder["headers"]
        ).status_code
        == 404
    )


# --- Effacement -------------------------------------------------------------


def test_disabling_crossings_purges_stored_positions(client, rider):
    account = rider("purge1", crossings_on=True)
    ping(client, account, *LYON)

    result = client.put(
        "/api/me/crossings", json={"enabled": False}, headers=account["headers"]
    ).json()
    assert result["enabled"] is False
    assert result["purged_pings"] >= 1

    with get_connection() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM location_pings WHERE user_id = ?", (account["user_id"],)
        ).fetchone()["n"]
    assert remaining == 0


def test_purge_endpoint_erases_crossings_and_pings(client, rider):
    alice = rider("purge2", crossings_on=True)
    bob = rider("purge3", crossings_on=True)
    ping(client, alice, *LYON, speed_kmh=80)
    ping(client, bob, *LYON, speed_kmh=80)
    assert client.get("/api/crossings", headers=alice["headers"]).json()["count"] == 1

    assert client.delete("/api/crossings", headers=alice["headers"]).json()["deleted_rows"] >= 2
    assert client.get("/api/crossings", headers=alice["headers"]).json()["count"] == 0
    # Le croisement disparaît aussi pour l'autre : c'est une donnée partagée.
    assert client.get("/api/crossings", headers=bob["headers"]).json()["count"] == 0


def test_account_deletion_removes_crossing_data(client, rider):
    alice = rider("del-cross", crossings_on=True)
    bob = rider("del-cross2", crossings_on=True)
    ping(client, alice, *LYON, speed_kmh=80)
    ping(client, bob, *LYON, speed_kmh=80)

    client.request(
        "DELETE",
        "/api/me",
        json={"password": TEST_PASSWORD, "confirmation": "SUPPRIMER"},
        headers=alice["headers"],
    )
    with get_connection() as conn:
        pings = conn.execute(
            "SELECT COUNT(*) AS n FROM location_pings WHERE user_id = ?", (alice["user_id"],)
        ).fetchone()["n"]
        crossed = conn.execute(
            "SELECT COUNT(*) AS n FROM crossings WHERE user_a_id = ? OR user_b_id = ?",
            (alice["user_id"], alice["user_id"]),
        ).fetchone()["n"]
    assert pings == 0 and crossed == 0


# --- Balades ----------------------------------------------------------------


def ride_body(**overrides) -> dict:
    body = {
        "title": "Cols du Vercors",
        "description": "170 km de virages.",
        "start_city": "Lyon",
        "start_latitude": LYON[0],
        "start_longitude": LYON[1],
        "start_at": in_days(3),
        "distance_km": 220,
        "pace": "sportif",
        "route_type": "cols",
        "max_participants": 4,
        "visibility": "public",
        "bike_categories": ["roadster", "trail"],
    }
    body.update(overrides)
    return body


def test_creating_a_ride_enrols_the_organiser(client, rider):
    organiser = rider("orga")
    created = client.post("/api/rides", json=ride_body(), headers=organiser["headers"])
    assert created.status_code == 201
    body = created.json()
    assert body["is_organiser"] is True
    assert body["accepted_count"] == 1
    assert body["bike_categories"] == ["roadster", "trail"]


def test_ride_must_start_in_the_future(client, rider):
    organiser = rider("orga2")
    response = client.post(
        "/api/rides", json=ride_body(start_at=in_days(-1)), headers=organiser["headers"]
    )
    assert response.status_code == 422


def test_ride_rejects_unknown_route_type_and_visibility(client, rider):
    organiser = rider("orga3")
    for bad in [{"route_type": "circuit-de-karting"}, {"visibility": "secret"}]:
        assert (
            client.post("/api/rides", json=ride_body(**bad), headers=organiser["headers"]).status_code
            == 422
        )


def test_public_ride_is_visible_and_joinable(client, rider):
    organiser = rider("orga4")
    joiner = rider("joiner")
    client.post("/api/rides", json=ride_body(), headers=organiser["headers"])

    listed = client.get("/api/rides", headers=joiner["headers"]).json()
    assert listed["count"] == 1
    ride_id = listed["results"][0]["id"]

    assert client.post(f"/api/rides/{ride_id}/join", headers=joiner["headers"]).json() == {
        "ride_id": ride_id,
        "status": "accepte",
    }
    assert client.get("/api/rides", headers=joiner["headers"]).json()["results"][0][
        "accepted_count"
    ] == 2


def test_exact_meeting_point_is_hidden_from_non_participants(client, rider):
    organiser = rider("orga5")
    outsider = rider("outsider")
    client.post("/api/rides", json=ride_body(), headers=organiser["headers"])

    ride = client.get("/api/rides", headers=outsider["headers"]).json()["results"][0]
    assert "start_latitude" not in ride
    assert ride["start_city"] == "Lyon"  # la ville reste publique

    client.post(f"/api/rides/{ride['id']}/join", headers=outsider["headers"])
    joined = client.get("/api/rides", headers=outsider["headers"]).json()["results"][0]
    assert joined["start_latitude"] == pytest.approx(LYON[0])


def test_matchs_only_ride_is_invisible_to_strangers(client, rider):
    organiser = rider("orga6")
    stranger = rider("stranger")
    matched = rider("matched")

    client.post(
        "/api/rides", json=ride_body(visibility="matchs"), headers=organiser["headers"]
    )
    assert client.get("/api/rides", headers=stranger["headers"]).json()["count"] == 0

    # On crée un match entre l'organisateur et `matched`.
    client.post(
        "/api/swipes",
        json={"target_user_id": matched["user_id"], "direction": "like"},
        headers=organiser["headers"],
    )
    client.post(
        "/api/swipes",
        json={"target_user_id": organiser["user_id"], "direction": "like"},
        headers=matched["headers"],
    )
    assert client.get("/api/rides", headers=matched["headers"]).json()["count"] == 1


def test_on_request_ride_needs_organiser_approval(client, rider):
    organiser = rider("orga7")
    candidate = rider("candidat")
    ride_id = client.post(
        "/api/rides", json=ride_body(visibility="sur-demande"), headers=organiser["headers"]
    ).json()["id"]

    assert client.post(f"/api/rides/{ride_id}/join", headers=candidate["headers"]).json()[
        "status"
    ] == "demande"
    # Tant que la demande n'est pas validée, le point de rendez-vous reste caché.
    pending = client.get(f"/api/rides/{ride_id}", headers=candidate["headers"]).json()
    assert "start_latitude" not in pending
    assert pending["accepted_count"] == 1

    approved = client.post(
        f"/api/rides/{ride_id}/participants/{candidate['user_id']}",
        json={"decision": "accepte"},
        headers=organiser["headers"],
    )
    assert approved.status_code == 200
    detail = client.get(f"/api/rides/{ride_id}", headers=candidate["headers"]).json()
    assert detail["start_latitude"] == pytest.approx(LYON[0])


def test_only_the_organiser_can_decide_on_requests(client, rider):
    organiser = rider("orga8")
    candidate = rider("candidat2")
    intruder = rider("intrus2")
    ride_id = client.post(
        "/api/rides", json=ride_body(visibility="sur-demande"), headers=organiser["headers"]
    ).json()["id"]
    client.post(f"/api/rides/{ride_id}/join", headers=candidate["headers"])

    assert (
        client.post(
            f"/api/rides/{ride_id}/participants/{candidate['user_id']}",
            json={"decision": "accepte"},
            headers=intruder["headers"],
        ).status_code
        == 404
    )


def test_pending_requests_are_visible_to_the_organiser_only(client, rider):
    organiser = rider("orga9")
    candidate = rider("candidat3")
    other = rider("autre")
    ride_id = client.post(
        "/api/rides", json=ride_body(visibility="sur-demande"), headers=organiser["headers"]
    ).json()["id"]
    client.post(f"/api/rides/{ride_id}/join", headers=candidate["headers"])

    organiser_view = client.get(f"/api/rides/{ride_id}", headers=organiser["headers"]).json()
    assert any(p["status"] == "demande" for p in organiser_view["participants"])

    other_view = client.get(f"/api/rides/{ride_id}", headers=other["headers"]).json()
    assert all(p["status"] == "accepte" for p in other_view["participants"])


def test_ride_refuses_riders_beyond_capacity(client, rider):
    organiser = rider("orga10")
    ride_id = client.post(
        "/api/rides", json=ride_body(max_participants=2), headers=organiser["headers"]
    ).json()["id"]

    first = rider("cap1")
    assert client.post(f"/api/rides/{ride_id}/join", headers=first["headers"]).status_code == 200

    second = rider("cap2")
    full = client.post(f"/api/rides/{ride_id}/join", headers=second["headers"])
    assert full.status_code == 409
    assert "complète" in full.json()["detail"]


def test_participant_can_leave_but_organiser_must_cancel(client, rider):
    organiser = rider("orga11")
    joiner = rider("joiner2")
    ride_id = client.post("/api/rides", json=ride_body(), headers=organiser["headers"]).json()["id"]
    client.post(f"/api/rides/{ride_id}/join", headers=joiner["headers"])

    assert client.delete(f"/api/rides/{ride_id}/join", headers=joiner["headers"]).status_code == 204
    assert (
        client.delete(f"/api/rides/{ride_id}/join", headers=organiser["headers"]).status_code == 400
    )

    assert client.delete(f"/api/rides/{ride_id}", headers=organiser["headers"]).status_code == 204
    # Une balade annulée disparaît des listes.
    assert client.get("/api/rides", headers=joiner["headers"]).json()["count"] == 0


def test_only_the_organiser_can_cancel(client, rider):
    organiser = rider("orga12")
    joiner = rider("joiner3")
    ride_id = client.post("/api/rides", json=ride_body(), headers=organiser["headers"]).json()["id"]
    assert client.delete(f"/api/rides/{ride_id}", headers=joiner["headers"]).status_code == 404


def test_blocked_riders_do_not_see_each_others_rides(client, rider):
    organiser = rider("orga13")
    blocked = rider("bloque")
    client.post("/api/rides", json=ride_body(), headers=organiser["headers"])
    client.post(
        "/api/blocks", json={"target_user_id": blocked["user_id"]}, headers=organiser["headers"]
    )
    assert client.get("/api/rides", headers=blocked["headers"]).json()["count"] == 0


def test_ride_list_filters_by_pace_and_route_type(client, rider):
    organiser = rider("orga14")
    client.post("/api/rides", json=ride_body(), headers=organiser["headers"])
    client.post(
        "/api/rides",
        json=ride_body(title="Sortie tranquille", pace="tranquille", route_type="cotier"),
        headers=organiser["headers"],
    )
    viewer = rider("viewer-rides")

    assert client.get("/api/rides?pace=tranquille", headers=viewer["headers"]).json()["count"] == 1
    assert client.get("/api/rides?route_type=cols", headers=viewer["headers"]).json()["count"] == 1
    assert (
        client.get("/api/rides?max_distance_km=1", headers=viewer["headers"]).json()["count"] == 2
    )


def test_rides_require_a_profile(client, register):
    account = register("sansprofil-balade@moto.example.com")
    assert client.get("/api/rides", headers=account["headers"]).status_code == 409
