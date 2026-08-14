"""Tests de l'abonnement, des quotas et des paiements.

Deux angles : que le bridage marche comme annoncé, et surtout qu'on ne puisse
pas s'offrir un abonnement sans payer.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy

import pytest

from motomatch import billing, payments
from motomatch.config import get_settings
from motomatch.db import get_connection


def other_profile(base: dict, **overrides) -> dict:
    profile = deepcopy(base)
    profile.update(overrides)
    return profile


@pytest.fixture
def rider(client, register, rider_profile):
    def _rider(name: str, **overrides):
        return register(
            f"{name}@moto.example.com",
            profile=other_profile(rider_profile, display_name=name.capitalize(), **overrides),
        )

    return _rider


def make_plus(user_id: int, expires_at: str | None = "2099-01-01T00:00:00+00:00") -> None:
    """Passe un compte en Plus directement en base, sans passer par le paiement."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO subscriptions (user_id, tier, expires_at, provider) "
            "VALUES (?, 'plus', ?, 'test') "
            "ON CONFLICT(user_id) DO UPDATE SET tier='plus', expires_at=excluded.expires_at",
            (user_id, expires_at),
        )


# --- Paliers et échéance ----------------------------------------------------


def test_free_tier_is_the_default(client, rider):
    account = rider("gratuit")
    body = client.get("/api/subscription", headers=account["headers"]).json()
    assert body["tier"] == billing.FREE
    assert body["entitlements"]["unlimited_likes"] is False
    assert body["entitlements"]["sees_who_liked"] is False
    assert body["likes"]["limit"] == get_settings().free_daily_likes


def test_an_expired_subscription_falls_back_to_free(client, rider):
    account = rider("expire")
    make_plus(account["user_id"], expires_at="2020-01-01T00:00:00+00:00")
    body = client.get("/api/subscription", headers=account["headers"]).json()
    assert body["tier"] == billing.FREE


def test_a_subscription_without_expiry_stays_active(client, rider):
    account = rider("offert")
    make_plus(account["user_id"], expires_at=None)
    assert client.get("/api/subscription", headers=account["headers"]).json()["tier"] == "plus"


@pytest.mark.parametrize("expiry", ["pas-une-date", "", "2020-13-45"])
def test_an_unreadable_expiry_is_treated_as_expired(expiry):
    """En cas de doute sur l'échéance, on retire les droits plutôt que de les
    accorder : se tromper dans ce sens coûte un mécontentement, dans l'autre un
    abonnement gratuit à vie."""
    assert billing.active_tier({"tier": "plus", "expires_at": expiry}) == billing.FREE


# --- Quota de likes ---------------------------------------------------------


def test_free_users_hit_the_daily_like_quota(client, rider, monkeypatch):
    # Deux likes suffisent à montrer le bridage, et le nombre de comptes créés
    # reste sous la limite d'inscription par IP — qui est là pour de bonnes
    # raisons et qu'un test n'a pas à contourner.
    monkeypatch.setenv("MOTOMATCH_FREE_DAILY_LIKES", "2")
    from motomatch.config import reload_settings

    reload_settings()

    viewer = rider("quota")
    cibles = [rider(f"cible{i}") for i in range(3)]

    for index in range(2):
        response = client.post(
            "/api/swipes",
            json={"target_user_id": cibles[index]["user_id"], "direction": "like"},
            headers=viewer["headers"],
        )
        assert response.status_code == 200
        assert response.json()["quota"]["used"] == index + 1

    refuse = client.post(
        "/api/swipes",
        json={"target_user_id": cibles[2]["user_id"], "direction": "like"},
        headers=viewer["headers"],
    )
    assert refuse.status_code == 402
    assert refuse.json()["detail"]["quota"]["remaining"] == 0

    reload_settings()


def test_passes_do_not_consume_the_like_quota(client, rider, monkeypatch):
    monkeypatch.setenv("MOTOMATCH_FREE_DAILY_LIKES", "2")
    from motomatch.config import reload_settings

    reload_settings()

    viewer = rider("passe")
    cibles = [rider(f"p{i}") for i in range(4)]
    for cible in cibles[:3]:
        assert client.post(
            "/api/swipes",
            json={"target_user_id": cible["user_id"], "direction": "pass"},
            headers=viewer["headers"],
        ).status_code == 200

    # Le quota est intact malgré trois passes.
    like = client.post(
        "/api/swipes",
        json={"target_user_id": cibles[3]["user_id"], "direction": "like"},
        headers=viewer["headers"],
    )
    assert like.status_code == 200
    assert like.json()["quota"]["used"] == 1
    reload_settings()


def test_plus_users_have_no_like_quota(client, rider, monkeypatch):
    monkeypatch.setenv("MOTOMATCH_FREE_DAILY_LIKES", "1")
    from motomatch.config import reload_settings

    reload_settings()

    viewer = rider("illimite")
    make_plus(viewer["user_id"])
    for index in range(4):
        cible = rider(f"c{index}")
        response = client.post(
            "/api/swipes",
            json={"target_user_id": cible["user_id"], "direction": "like"},
            headers=viewer["headers"],
        )
        assert response.status_code == 200, response.text
        assert response.json()["quota"]["unlimited"] is True
    reload_settings()


# --- Qui t'a liké -----------------------------------------------------------


def test_free_users_only_see_the_count_of_who_liked_them(client, rider):
    viewer = rider("rideau")
    admirateur = rider("admirateur")
    client.post(
        "/api/swipes",
        json={"target_user_id": viewer["user_id"], "direction": "like"},
        headers=admirateur["headers"],
    )

    body = client.get("/api/likes/received", headers=viewer["headers"]).json()
    assert body["locked"] is True
    assert body["count"] == 1
    assert body["results"] == []  # aucun nom, aucune photo, aucune ville


def test_plus_users_see_who_liked_them(client, rider):
    viewer = rider("payant")
    make_plus(viewer["user_id"])
    admirateur = rider("admirateur2")
    client.post(
        "/api/swipes",
        json={"target_user_id": viewer["user_id"], "direction": "like"},
        headers=admirateur["headers"],
    )

    body = client.get("/api/likes/received", headers=viewer["headers"]).json()
    assert body["locked"] is False
    assert body["results"][0]["profile"]["display_name"] == "Admirateur2"
    # Même en payant, les coordonnées d'autrui ne sortent pas.
    assert "latitude" not in body["results"][0]["profile"]


def test_already_answered_likes_leave_the_list(client, rider):
    viewer = rider("repondu")
    make_plus(viewer["user_id"])
    admirateur = rider("admirateur3")
    client.post(
        "/api/swipes",
        json={"target_user_id": viewer["user_id"], "direction": "like"},
        headers=admirateur["headers"],
    )
    client.post(
        "/api/swipes",
        json={"target_user_id": admirateur["user_id"], "direction": "like"},
        headers=viewer["headers"],
    )
    assert client.get("/api/likes/received", headers=viewer["headers"]).json()["count"] == 0


# --- Boost ------------------------------------------------------------------


def test_boost_requires_a_subscription(client, rider):
    account = rider("sansboost")
    assert client.post("/api/boost", json={}, headers=account["headers"]).status_code == 402


def test_boost_lifts_a_profile_in_the_ranking(client, rider):
    viewer = rider("spectateur")
    # Profil volontairement peu compatible : sans boost il finit dernier.
    faible = rider(
        "faible", latitude=48.8566, longitude=2.3522, bike_category="custom",
        pace="tranquille", engine_cc=125, riding_styles=["urbain"],
    )
    rider("fort")

    avant = client.get("/api/discover", headers=viewer["headers"]).json()["results"]
    rang_avant = [r["profile"]["display_name"] for r in avant].index("Faible")

    make_plus(faible["user_id"])
    assert client.post("/api/boost", json={}, headers=faible["headers"]).status_code == 200

    apres = client.get("/api/discover", headers=viewer["headers"]).json()["results"]
    noms = [r["profile"]["display_name"] for r in apres]
    assert noms.index("Faible") <= rang_avant

    boosté = next(r for r in apres if r["profile"]["display_name"] == "Faible")
    # Le score acheté est distingué du score de compatibilité : sans cela, un
    # profil remonté par l'argent passerait pour un profil compatible.
    assert boosté["boosted"] is True
    assert boosté["score"] > boosté["compatibility_score"]


def test_only_one_boost_at_a_time(client, rider):
    account = rider("doubleboost")
    make_plus(account["user_id"])
    assert client.post("/api/boost", json={}, headers=account["headers"]).status_code == 200
    assert client.post("/api/boost", json={}, headers=account["headers"]).status_code == 409


def test_monthly_boost_allowance_is_enforced(client, rider, monkeypatch):
    monkeypatch.setenv("MOTOMATCH_PLUS_MONTHLY_BOOSTS", "1")
    from motomatch.config import reload_settings

    reload_settings()
    account = rider("unboost")
    make_plus(account["user_id"])
    assert client.post("/api/boost", json={}, headers=account["headers"]).status_code == 200

    # On termine le boost en cours pour isoler la limite mensuelle.
    with get_connection() as conn:
        conn.execute(
            "UPDATE boosts SET expires_at = datetime('now', '-1 minute') WHERE user_id = ?",
            (account["user_id"],),
        )
    response = client.post("/api/boost", json={}, headers=account["headers"])
    assert response.status_code == 409
    assert "déjà utilisés" in response.json()["detail"]
    reload_settings()


# --- Revenir en arrière -----------------------------------------------------


def test_rewind_requires_a_subscription(client, rider):
    viewer = rider("sansrewind")
    cible = rider("cible-rewind")
    client.post(
        "/api/swipes",
        json={"target_user_id": cible["user_id"], "direction": "pass"},
        headers=viewer["headers"],
    )
    assert client.delete("/api/swipes/last", headers=viewer["headers"]).status_code == 402


def test_rewind_puts_the_profile_back_in_the_deck(client, rider):
    viewer = rider("rewind")
    make_plus(viewer["user_id"])
    cible = rider("revenu")
    client.post(
        "/api/swipes",
        json={"target_user_id": cible["user_id"], "direction": "pass"},
        headers=viewer["headers"],
    )
    noms = [
        r["profile"]["display_name"]
        for r in client.get("/api/discover", headers=viewer["headers"]).json()["results"]
    ]
    assert "Revenu" not in noms

    assert client.delete("/api/swipes/last", headers=viewer["headers"]).status_code == 200
    noms = [
        r["profile"]["display_name"]
        for r in client.get("/api/discover", headers=viewer["headers"]).json()["results"]
    ]
    assert "Revenu" in noms


def test_a_like_that_created_a_match_cannot_be_undone(client, rider):
    viewer = rider("matche")
    make_plus(viewer["user_id"])
    autre = rider("autre-matche")
    client.post(
        "/api/swipes",
        json={"target_user_id": viewer["user_id"], "direction": "like"},
        headers=autre["headers"],
    )
    client.post(
        "/api/swipes",
        json={"target_user_id": autre["user_id"], "direction": "like"},
        headers=viewer["headers"],
    )
    response = client.delete("/api/swipes/last", headers=viewer["headers"])
    assert response.status_code == 409


# --- Paiement : on ne s'abonne pas tout seul --------------------------------


def webhook(client, body: dict, secret: str, timestamp: int | None = None):
    payload = json.dumps(body).encode()
    return client.post(
        "/api/subscription/webhook",
        content=payload,
        headers={
            "x-signature": payments.sign_payload(payload, secret, timestamp),
            "content-type": "application/json",
        },
    )


def test_checkout_never_grants_the_subscription(client, rider):
    """Le point le plus important du module : payer se constate côté
    prestataire, jamais côté client."""
    account = rider("checkout")
    response = client.post(
        "/api/subscription/checkout", json={"offer_code": "plus_1m"}, headers=account["headers"]
    )
    assert response.status_code == 200
    assert client.get("/api/subscription", headers=account["headers"]).json()["tier"] == "gratuit"


def test_webhook_without_a_valid_signature_is_refused(client, rider, monkeypatch):
    monkeypatch.setenv("MOTOMATCH_PAYMENT_WEBHOOK_SECRET", "secret-de-test-suffisamment-long")
    from motomatch.config import reload_settings

    reload_settings()
    account = rider("faux")
    body = {"id": "evt_1", "type": payments.SUBSCRIPTION_ACTIVATED, "user_id": account["user_id"]}

    # Sans en-tête du tout
    assert client.post("/api/subscription/webhook", json=body).status_code == 400
    # Avec une signature fabriquée avec le mauvais secret
    assert webhook(client, body, "mauvais-secret").status_code == 400
    assert client.get("/api/subscription", headers=account["headers"]).json()["tier"] == "gratuit"
    reload_settings()


def test_a_valid_webhook_activates_the_subscription(client, rider, monkeypatch):
    secret = "secret-de-test-suffisamment-long"
    monkeypatch.setenv("MOTOMATCH_PAYMENT_WEBHOOK_SECRET", secret)
    from motomatch.config import reload_settings

    reload_settings()
    account = rider("vraipaiement")
    body = {
        "id": "evt_2",
        "type": payments.SUBSCRIPTION_ACTIVATED,
        "user_id": account["user_id"],
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    assert webhook(client, body, secret).json()["handled"] is True
    assert client.get("/api/subscription", headers=account["headers"]).json()["tier"] == "plus"
    reload_settings()


def test_a_replayed_webhook_does_not_extend_twice(client, rider, monkeypatch):
    """Les prestataires rejouent leurs webhooks au moindre doute."""
    secret = "secret-de-test-suffisamment-long"
    monkeypatch.setenv("MOTOMATCH_PAYMENT_WEBHOOK_SECRET", secret)
    from motomatch.config import reload_settings

    reload_settings()
    account = rider("rejeu")
    body = {
        "id": "evt_3",
        "type": payments.SUBSCRIPTION_ACTIVATED,
        "user_id": account["user_id"],
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    assert webhook(client, body, secret).json()["handled"] is True
    second = webhook(client, body, secret).json()
    assert second["handled"] is False and second["reason"] == "deja_traite"
    reload_settings()


def test_an_old_signature_cannot_be_replayed(client, rider, monkeypatch):
    """Sans contrôle d'horodatage, un message signé capté une fois prolongerait
    l'abonnement indéfiniment."""
    secret = "secret-de-test-suffisamment-long"
    monkeypatch.setenv("MOTOMATCH_PAYMENT_WEBHOOK_SECRET", secret)
    from motomatch.config import reload_settings

    reload_settings()
    account = rider("vieux")
    body = {"id": "evt_4", "type": payments.SUBSCRIPTION_ACTIVATED, "user_id": account["user_id"]}
    vieux = int(time.time()) - 4000
    assert webhook(client, body, secret, timestamp=vieux).status_code == 400
    assert client.get("/api/subscription", headers=account["headers"]).json()["tier"] == "gratuit"
    reload_settings()


def test_signature_verification_is_constant_time_and_strict():
    secret = "secret-de-test"
    payload = b'{"id":"x"}'
    header = payments.sign_payload(payload, secret)

    payments.verify_signature(payload, header, secret, 300)  # ne lève pas

    with pytest.raises(payments.SignatureError):
        payments.verify_signature(b'{"id":"y"}', header, secret, 300)  # corps modifié
    with pytest.raises(payments.SignatureError):
        payments.verify_signature(payload, header, "autre-secret", 300)
    with pytest.raises(payments.SignatureError):
        payments.verify_signature(payload, "n'importe quoi", secret, 300)
    with pytest.raises(payments.SignatureError):
        payments.verify_signature(payload, header, "", 300)  # aucun secret configuré


def test_a_webhook_for_an_unknown_account_is_refused(client, monkeypatch):
    secret = "secret-de-test-suffisamment-long"
    monkeypatch.setenv("MOTOMATCH_PAYMENT_WEBHOOK_SECRET", secret)
    from motomatch.config import reload_settings

    reload_settings()
    body = {"id": "evt_5", "type": payments.SUBSCRIPTION_ACTIVATED, "user_id": 99999}
    assert webhook(client, body, secret).status_code == 404
    reload_settings()


def test_cancelling_keeps_access_until_the_paid_term(client, rider):
    account = rider("resilie")
    make_plus(account["user_id"])
    response = client.delete("/api/subscription", headers=account["headers"])
    assert response.status_code == 200
    assert response.json()["access_until"] == "2099-01-01T00:00:00+00:00"
    # L'accès n'est pas coupé : la période est payée.
    assert client.get("/api/subscription", headers=account["headers"]).json()["tier"] == "plus"


def test_offers_are_public_and_priced_by_the_server(client):
    offers = client.get("/api/subscription/offers").json()["offers"]
    assert {o["code"] for o in offers} == {"plus_1m", "plus_6m"}
    assert all(o["price"] > 0 for o in offers)
