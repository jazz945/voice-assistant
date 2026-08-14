# Brancher Stripe

Tout le code est en place. Il ne manque que quatre valeurs, que **toi seul**
renseignes : elles ne passent ni par le dépôt, ni par une conversation.

## 1. Côté Stripe

1. Crée un compte sur <https://dashboard.stripe.com>. Reste en **mode test**
   tant que tu n'as pas de société déclarée — les clés commencent par `sk_test_`
   et aucun argent réel ne circule.
2. **Produits → Ajouter un produit** → « MotoMatch Plus ». Crée deux tarifs
   *récurrents* : un mensuel et un semestriel. Note les identifiants, de la
   forme `price_1AbC...`.
3. **Développeurs → Webhooks → Ajouter un point de terminaison** :
   - URL : `https://ton-domaine.fr/api/subscription/webhook`
   - Évènements à envoyer :
     `checkout.session.completed`, `customer.subscription.created`,
     `customer.subscription.updated`, `customer.subscription.deleted`,
     `invoice.paid`
   - Récupère le **secret de signature** (`whsec_...`).

## 2. Côté serveur

Dans `deploy/.env` :

```bash
MOTOMATCH_STRIPE_SECRET_KEY=sk_test_...
MOTOMATCH_STRIPE_PRICE_MONTHLY=price_...
MOTOMATCH_STRIPE_PRICE_BIANNUAL=price_...
MOTOMATCH_PAYMENT_WEBHOOK_SECRET=whsec_...
MOTOMATCH_STRIPE_SUCCESS_URL=https://ton-domaine.fr/?abonnement=ok
MOTOMATCH_STRIPE_CANCEL_URL=https://ton-domaine.fr/?abonnement=annule
```

Puis `docker compose up -d`. Tant que les quatre premières manquent, le bouton
« S'abonner » répond `prestataire_non_configure` au lieu de rediriger : la
configuration à moitié faite se voit tout de suite, pas au moment où quelqu'un
clique.

## 3. Essayer sans argent réel

```bash
stripe listen --forward-to localhost:8000/api/subscription/webhook
```

Carte de test : `4242 4242 4242 4242`, n'importe quelle date future, n'importe
quel CVC.

## Ce qui est déjà géré

| | |
|---|---|
| Signature | HMAC-SHA256 comparé en temps constant |
| Rejeu | Horodatage hors tolérance (5 min) refusé |
| Doublon | `UNIQUE (provider, event_id)` — un webhook rejoué est sans effet |
| Renouvellement | Retrouvé par identifiant client Stripe quand nos métadonnées manquent |
| Résiliation | `customer.subscription.deleted` ferme l'abonnement |
| Double clic | Clé d'idempotence à la création de session |
| Panne Stripe | 502 générique, aucun détail interne renvoyé |

## Ce qui n'est pas géré

- **Rien pour iOS ni Android.** Apple (règle 3.1.1) et Google imposent leur
  facturation, 15 à 30 % de commission ; router vers Stripe depuis l'application
  fait rejeter la soumission. Il faut StoreKit et Play Billing, avec validation
  des reçus côté serveur — un autre chantier.
- **Aucune obligation légale.** Société déclarée, TVA, CGV, et le droit de
  rétractation de 14 jours sur un abonnement en France. Ce n'est pas du code, et
  ça se règle avant d'encaisser le premier euro.
- **Pas d'échec de paiement traité.** `invoice.payment_failed` n'est pas écouté :
  un abonnement impayé expire simplement à son échéance.
