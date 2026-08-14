"""Réception des évènements de paiement.

## Pourquoi un webhook signé et pas un simple appel

L'activation de l'abonnement ne doit **jamais** venir du client. Une route du
type « je viens de payer, active-moi » se déclenche depuis n'importe quel
terminal : l'abonnement serait gratuit pour qui sait faire un `curl`. Seul le
prestataire de paiement sait qu'un paiement a réellement eu lieu, et il le dit
par un webhook signé, vérifié ici.

## Ce que ce module ne fait pas

Il n'appelle aucun prestataire. Le format de signature implémenté est celui de
Stripe (`t=…,v1=…`, HMAC-SHA256 sur `timestamp.corps`), largement repris
ailleurs, mais la création des sessions de paiement demande des clés d'API et un
compte : c'est à brancher au moment du déploiement, pas ici.

**Sur mobile, ce chemin ne sert pas.** Apple (règle 3.1.1) et Google imposent
leur propre facturation pour tout abonnement numérique, avec 15 à 30 % de
commission. Passer par un prestataire tiers dans l'application fait rejeter la
soumission. Le webhook ci-dessous vaut pour le web ; une publication sur les
stores demande en plus la validation des reçus StoreKit et Google Play Billing.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

STRIPE = "stripe"

# Évènements traités. Tout le reste est journalisé puis ignoré : un prestataire
# en envoie des dizaines, et réagir à un évènement mal compris est pire que de
# le laisser passer.
SUBSCRIPTION_ACTIVATED = "subscription.activated"
SUBSCRIPTION_RENEWED = "subscription.renewed"
SUBSCRIPTION_CANCELLED = "subscription.cancelled"
HANDLED_EVENTS = (SUBSCRIPTION_ACTIVATED, SUBSCRIPTION_RENEWED, SUBSCRIPTION_CANCELLED)

# Correspondance entre les types d'évènements réels de Stripe et les trois
# actions ci-dessus. Ce qui n'y figure pas est journalisé puis ignoré : Stripe
# émet des dizaines de types, et réagir à un évènement mal compris est pire que
# de le laisser passer.
STRIPE_EVENT_MAP = {
    "checkout.session.completed": SUBSCRIPTION_ACTIVATED,
    "customer.subscription.created": SUBSCRIPTION_ACTIVATED,
    "customer.subscription.updated": SUBSCRIPTION_RENEWED,
    "invoice.paid": SUBSCRIPTION_RENEWED,
    "customer.subscription.deleted": SUBSCRIPTION_CANCELLED,
}


@dataclass(frozen=True)
class SubscriptionEvent:
    """Ce qu'on retient d'un évènement, une fois traduit."""

    action: str
    user_id: int | None
    customer_id: str | None
    subscription_id: str | None
    expires_at: str | None


def _iso_from_epoch(value: object) -> str | None:
    """Stripe date en secondes epoch ; la base stocke de l'ISO 8601 UTC."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def parse_stripe_event(event: dict) -> SubscriptionEvent | None:
    """Traduit un évènement Stripe, ou None s'il ne nous concerne pas.

    L'identifiant de compte est cherché à trois endroits, du plus fiable au
    moins : `client_reference_id` (posé à la création de la session), les
    métadonnées de l'abonnement (reprises sur les renouvellements), puis
    l'identifiant client Stripe — ce dernier ne donne pas l'utilisateur
    directement et laisse l'appelant faire la correspondance en base.
    """
    action = STRIPE_EVENT_MAP.get(str(event.get("type", "")))
    if action is None:
        return None

    obj = (event.get("data") or {}).get("object") or {}
    metadata = obj.get("metadata") or {}

    raw_user = obj.get("client_reference_id") or metadata.get("user_id")
    try:
        user_id = int(raw_user) if raw_user is not None else None
    except (TypeError, ValueError):
        user_id = None

    subscription = obj.get("subscription")
    if isinstance(subscription, dict):
        subscription = subscription.get("id")

    # `checkout.session` n'a pas d'échéance ; elle arrive sur l'abonnement.
    expires_at = _iso_from_epoch(obj.get("current_period_end"))

    return SubscriptionEvent(
        action=action,
        user_id=user_id,
        customer_id=obj.get("customer") if isinstance(obj.get("customer"), str) else None,
        subscription_id=subscription if isinstance(subscription, str) else str(obj.get("id", "")),
        expires_at=expires_at,
    )


class SignatureError(Exception):
    """Signature absente, mal formée, périmée ou fausse."""


@dataclass(frozen=True)
class ParsedSignature:
    timestamp: int
    signatures: tuple[str, ...]


def parse_signature_header(header: str) -> ParsedSignature:
    """Décompose un en-tête `t=1700000000,v1=abc…`.

    Plusieurs `v1` peuvent coexister pendant une rotation de secret côté
    prestataire : on les accepte tous et on compare à chacun.
    """
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as error:
                raise SignatureError("horodatage de signature illisible") from error
        elif key == "v1":
            signatures.append(value)

    if timestamp is None or not signatures:
        raise SignatureError("en-tête de signature incomplet")
    return ParsedSignature(timestamp, tuple(signatures))


def verify_signature(
    payload: bytes, header: str, secret: str, tolerance_seconds: int, now: float | None = None
) -> None:
    """Vérifie la signature d'un webhook, ou lève `SignatureError`.

    Deux contrôles, pas un :

    - **l'empreinte**, comparée en temps constant — une comparaison naïve
      laisserait fuir la signature attendue octet par octet ;
    - **l'horodatage**, dans une fenêtre étroite. Sans lui, un message signé
      capté une fois pourrait être rejoué indéfiniment pour prolonger un
      abonnement à volonté.
    """
    if not secret:
        raise SignatureError("aucun secret de webhook configuré")

    parsed = parse_signature_header(header)
    moment = time.time() if now is None else now
    if abs(moment - parsed.timestamp) > tolerance_seconds:
        raise SignatureError("horodatage hors tolérance")

    signed = f"{parsed.timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in parsed.signatures):
        raise SignatureError("signature invalide")


def sign_payload(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Fabrique un en-tête de signature. Sert aux tests et aux essais locaux."""
    moment = int(time.time()) if timestamp is None else timestamp
    signed = f"{moment}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={moment},v1={digest}"
