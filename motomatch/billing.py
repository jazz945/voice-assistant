"""Abonnement MotoMatch Plus : quotas, avantages et boosts.

## Ce que ce module fait au produit, dit franchement

Le modèle retenu est celui de Tinder : brider la version gratuite, puis vendre
la levée du bridage. Il convertit bien, et il a un coût que le code ne peut pas
masquer. Trois endroits font mal, et ils sont commentés là où ils agissent :

1. **Le quota de likes** dégrade volontairement l'usage gratuit. La frustration
   n'est pas un effet de bord, c'est le moteur de la conversion.
2. **Masquer qui vous a liké** retient une information que l'application
   possède déjà et que l'utilisateur a déjà méritée. On vend un rideau.
3. **Le boost fausse le classement par compatibilité** — la seule chose qui
   distingue vraiment MotoMatch. Un profil payant remonte au-dessus d'un profil
   objectivement plus compatible.

Le point 3 est le plus coûteux : il abîme la fonction qui fait la valeur du
produit. Le bonus est donc borné et affiché comme tel dans la réponse de l'API
(`boosted: true`), pour que personne — utilisateur comme développeur — ne
confonde un score de compatibilité avec un score acheté.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

FREE = "gratuit"
PLUS = "plus"
TIERS = (FREE, PLUS)


@dataclass(frozen=True)
class Entitlements:
    """Ce à quoi un palier donne droit."""

    tier: str
    daily_likes: int | None          # None = illimité
    sees_who_liked: bool
    monthly_boosts: int
    can_rewind: bool                 # revenir sur le dernier profil passé
    advanced_filters: bool

    @property
    def unlimited_likes(self) -> bool:
        return self.daily_likes is None


def entitlements_for(tier: str, cfg) -> Entitlements:
    if tier == PLUS:
        return Entitlements(
            tier=PLUS,
            daily_likes=None,
            sees_who_liked=True,
            monthly_boosts=cfg.plus_monthly_boosts,
            can_rewind=True,
            advanced_filters=True,
        )
    return Entitlements(
        tier=FREE,
        daily_likes=cfg.free_daily_likes,
        sees_who_liked=False,
        monthly_boosts=0,
        can_rewind=False,
        advanced_filters=False,
    )


def active_tier(subscription: sqlite3.Row | None) -> str:
    """Palier réellement actif.

    Un abonnement expiré retombe sur le gratuit sans qu'aucune tâche de fond
    n'ait à passer : l'échéance est lue à chaque requête. Une tâche planifiée
    qui prendrait du retard laisserait sinon des comptes payants gratuits, ou
    l'inverse.
    """
    if subscription is None or subscription["tier"] != PLUS:
        return FREE
    expires = subscription["expires_at"]
    if expires is None:
        return PLUS  # abonnement sans échéance (offert, test)
    return PLUS if _still_valid(expires) else FREE


def _still_valid(expires_at: str) -> bool:
    from datetime import datetime, timezone

    try:
        moment = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment > datetime.now(timezone.utc)


@dataclass
class LikeQuota:
    """État du quota de likes du jour."""

    limit: int | None
    used: int
    resets_at: str

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit

    def payload(self) -> dict:
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "resets_at": self.resets_at,
            "unlimited": self.limit is None,
        }


def boost_bonus(cfg, boosted: bool) -> float:
    """Points ajoutés au score de compatibilité d'un profil boosté.

    Volontairement borné : au-delà, un profil payant sans rien en commun
    passerait devant un profil réellement compatible, et le classement ne
    voudrait plus rien dire. Même à cette valeur, il fausse déjà l'ordre — c'est
    le prix du modèle, pas un détail d'implémentation.
    """
    return cfg.boost_score_bonus if boosted else 0.0


# --- Offre affichée ---------------------------------------------------------


@dataclass(frozen=True)
class Offer:
    """Description de l'offre, pour l'écran d'abonnement.

    Les montants ne sont qu'un affichage : le prix qui fait foi est celui du
    prestataire de paiement. Les dupliquer ici ne sert qu'à composer la page.
    """

    code: str
    label: str
    price_cents: int
    currency: str = "EUR"
    period: str = "mois"
    highlights: list[str] = field(default_factory=list)

    def payload(self) -> dict:
        return {
            "code": self.code,
            "label": self.label,
            "price": self.price_cents / 100,
            "currency": self.currency,
            "period": self.period,
            "highlights": self.highlights,
        }


def offers(cfg) -> list[Offer]:
    avantages = [
        "Likes illimités",
        "Voir qui t'a liké",
        f"{cfg.plus_monthly_boosts} boosts par mois",
        "Revenir sur le dernier profil passé",
        "Filtres avancés",
    ]
    return [
        Offer("plus_1m", "MotoMatch Plus — 1 mois", cfg.price_monthly_cents, highlights=avantages),
        Offer(
            "plus_6m",
            "MotoMatch Plus — 6 mois",
            cfg.price_biannual_cents,
            period="6 mois",
            highlights=avantages,
        ),
    ]
