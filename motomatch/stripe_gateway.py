"""Client Stripe minimal : création de sessions de paiement.

Écrit sur `urllib` plutôt qu'avec la bibliothèque officielle. Le projet tient
sur quatre dépendances ; en ajouter une de 3 Mo pour deux appels HTTP serait
disproportionné, et l'API de Stripe est du formulaire encodé, pas un protocole
compliqué.

## Ce qui ne doit jamais arriver ici

- **La clé secrète ne sort pas.** Elle n'est ni journalisée, ni renvoyée, ni
  incluse dans un message d'erreur. Les exceptions ci-dessous ne portent que le
  code HTTP et le message de Stripe.
- **Le montant ne vient pas du client.** L'appelant passe un identifiant de
  tarif (`price_...`) défini dans le tableau de bord Stripe ; le prix est fixé
  là-bas. Accepter un montant de l'utilisateur reviendrait à le laisser choisir
  ce qu'il paie.
- **Cette classe n'accorde aucun droit.** Elle ouvre une session de paiement,
  rien de plus. L'abonnement est activé par le webhook signé (`payments.py`),
  seul témoin qu'un paiement a réellement abouti.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_BASE = "https://api.stripe.com/v1"
TIMEOUT_SECONDS = 20


class StripeError(Exception):
    """Appel refusé par Stripe, ou injoignable."""


@dataclass(frozen=True)
class CheckoutSession:
    id: str
    url: str


class StripeGateway:
    """Enveloppe autour des deux appels dont l'application a besoin."""

    def __init__(self, secret_key: str, api_base: str = API_BASE) -> None:
        if not secret_key:
            raise StripeError("aucune clé Stripe configurée")
        self._secret_key = secret_key
        self._api_base = api_base.rstrip("/")

    def create_checkout_session(
        self,
        *,
        price_id: str,
        user_id: int,
        success_url: str,
        cancel_url: str,
        customer_email: str | None = None,
        idempotency_key: str | None = None,
    ) -> CheckoutSession:
        """Ouvre une session de paiement pour un abonnement.

        `client_reference_id` porte notre identifiant utilisateur jusqu'au
        webhook : c'est ce qui permet, au retour, de savoir quel compte activer.
        Sans lui, il faudrait deviner à partir de l'adresse e-mail — fragile dès
        qu'une personne paie avec une autre adresse que celle de son compte.
        """
        fields: list[tuple[str, str]] = [
            ("mode", "subscription"),
            ("line_items[0][price]", price_id),
            ("line_items[0][quantity]", "1"),
            ("success_url", success_url),
            ("cancel_url", cancel_url),
            ("client_reference_id", str(user_id)),
            # Repris dans l'abonnement créé, donc présent sur les évènements de
            # renouvellement où `client_reference_id` n'existe plus.
            ("subscription_data[metadata][user_id]", str(user_id)),
        ]
        if customer_email:
            fields.append(("customer_email", customer_email))

        payload = self._post("/checkout/sessions", fields, idempotency_key)
        url = payload.get("url")
        if not url:
            raise StripeError("Stripe n'a pas renvoyé d'URL de paiement")
        return CheckoutSession(id=str(payload.get("id", "")), url=str(url))

    def _post(
        self, path: str, fields: list[tuple[str, str]], idempotency_key: str | None
    ) -> dict:
        request = urllib.request.Request(
            f"{self._api_base}{path}",
            data=urllib.parse.urlencode(fields).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._secret_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                # Une clé d'idempotence évite qu'un double clic, ou un renvoi
                # après temporisation, crée deux sessions et deux paiements.
                **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            # On ne remonte que ce que Stripe dit, jamais la requête envoyée :
            # elle contient l'en-tête d'autorisation.
            try:
                detail = json.loads(error.read() or b"{}").get("error", {}).get("message", "")
            except ValueError:
                detail = ""
            raise StripeError(f"Stripe a refusé l'appel ({error.code}) : {detail}") from None
        except urllib.error.URLError as error:
            raise StripeError(f"Stripe injoignable : {error.reason}") from None
