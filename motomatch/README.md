# MotoMatch — application de rencontre pour motards

Une application de rencontre complète, pensée pour les motards : le score de
compatibilité ne repose pas seulement sur la distance, mais sur ce qui décide
vraiment si deux personnes rouleront bien ensemble — la famille de moto, le
rythme de conduite, la cylindrée, les pratiques et l'expérience.

Backend FastAPI + SQLite, interface web sans aucune dépendance JavaScript, et
une posture de sécurité pensée pour une application de rencontre : Argon2id,
sessions révocables, protection contre la trilatération géographique, blocage,
signalement et suppression de compte. Détail complet dans **[SECURITY.md](SECURITY.md)**.

Le chemin vers l'App Store et Google Play est décrit dans
**[DEPLOIEMENT_STORES.md](DEPLOIEMENT_STORES.md)**.

## Démarrage rapide

```bash
pip install -r motomatch/requirements.txt

# Charge une douzaine de profils de démonstration (mot de passe : Vercors-Col-2024)
python -m motomatch.seed --reset

uvicorn motomatch.main:app --reload
```

Ouvre ensuite <http://127.0.0.1:8000>. La documentation interactive de l'API est
sur <http://127.0.0.1:8000/docs>.

Compte de démonstration : `camille@motomatch.example.com` / `Vercors-Col-2024`.

Le fichier SQLite est créé à côté du code ; on peut le déplacer avec la variable
d'environnement `MOTOMATCH_DB`.

## Fonctionnalités

- **Inscription et session** — Argon2id, jeton d'accès court (15 min) et jeton
  de rafraîchissement rotatif (30 jours), tous deux révocables et hachés au repos.
- **Sécurité du compte** — écran « appareils connectés », changement de mot de
  passe, export des données (RGPD) et suppression définitive.
- **Sécurité des personnes** — blocage bidirectionnel et signalement typé.
- **Profil moto** — marque, modèle, année, cylindrée, famille de moto, rythme,
  années de permis, kilométrage annuel, distance maximale acceptée pour un
  rendez-vous, pratiques et bio.
- **Découverte classée** — les candidats sont triés par score de compatibilité
  et accompagnés du détail du calcul et de quelques phrases d'explication.
- **Filtres** — rayon, tranche d'âge, famille de moto, pratique.
- **Like / pass et matchs** — un match est créé dès que le like est réciproque ;
  les profils déjà évalués disparaissent du deck.
- **Messagerie** — conversation par match, réservée aux deux participants.

## Score de compatibilité

Le calcul vit dans `matching.py`. Six composantes, chacune normalisée entre 0 et
1, puis pondérées :

| Composante | Poids | Ce qu'elle mesure |
|---|---|---|
| `distance` | 30 % | Distance orthodromique comparée au rayon que **les deux** personnes acceptent de parcourir — c'est le plus restrictif des deux qui compte. |
| `styles` | 25 % | Indice de Jaccard entre les pratiques déclarées (balade, col, circuit, off-road…). |
| `category` | 15 % | Affinité entre familles de motos : une sportive et un supermotard se suivent bien, une sportive et un custom beaucoup moins. |
| `pace` | 15 % | Écart de rythme de conduite, de `tranquille` à `tres-sportif`. |
| `engine` | 8 % | Rapport des cylindrées : un 125 et un 1250 ne tiennent pas la même allure. |
| `experience` | 7 % | Écart d'années de permis. |

Un profil qui n'a déclaré aucune pratique obtient un score neutre (0,5) sur cette
composante plutôt qu'une pénalité — un profil incomplet ne doit pas être exclu.

La matrice d'affinité entre familles de motos (`_CATEGORY_AFFINITY`) est
symétrique et volontairement lisible : c'est le premier endroit à ajuster pour
faire évoluer le classement.

## API

| Méthode | Route | Description |
|---|---|---|
| `GET` | `/api/meta` | Valeurs autorisées (familles, pratiques, rythmes). |
| `POST` | `/api/auth/register` | Création de compte (attestation de majorité requise). |
| `POST` | `/api/auth/login` | Connexion, renvoie un couple de jetons. |
| `POST` | `/api/auth/refresh` | Rotation du jeton de rafraîchissement. |
| `POST` | `/api/auth/logout` | Invalide la session courante. |
| `POST` | `/api/auth/logout-all` | Déconnecte tous les appareils. |
| `GET` | `/api/auth/sessions` | Appareils connectés. |
| `DELETE` | `/api/auth/sessions/{id}` | Révoque une session. |
| `GET` | `/api/me` | Compte et profil de l'utilisateur connecté. |
| `PUT` | `/api/me/profile` | Crée ou met à jour le profil moto. |
| `POST` | `/api/me/password` | Change le mot de passe et ferme les autres sessions. |
| `GET` | `/api/me/export` | Export des données personnelles (RGPD). |
| `DELETE` | `/api/me` | Suppression définitive du compte. |
| `GET` | `/api/discover` | Candidats triés par compatibilité. Paramètres : `max_distance_km`, `min_age`, `max_age`, `categories`, `styles`, `limit`. |
| `POST` | `/api/swipes` | `{"target_user_id": 3, "direction": "like"}` → `{"matched": true, "match_id": 1}`. |
| `GET` | `/api/matches` | Matchs, avec le dernier message de chaque conversation. |
| `GET` | `/api/matches/{id}/messages` | Fil de discussion. |
| `POST` | `/api/matches/{id}/messages` | Envoi d'un message. |
| `POST` | `/api/blocks` | Bloque un utilisateur. |
| `GET` | `/api/blocks` | Liste des personnes bloquées. |
| `DELETE` | `/api/blocks/{user_id}` | Débloque un utilisateur. |
| `POST` | `/api/reports` | Signale un utilisateur (et le bloque). |

Les réponses de `/api/discover` et `/api/matches` **ne contiennent jamais** les
coordonnées GPS d'autrui, et les distances sont arrondies par paliers.

Exemple :

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"camille@motomatch.example.com","password":"Vercors-Col-2024"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

curl -s "localhost:8000/api/discover?max_distance_km=150&styles=col" \
  -H "Authorization: Bearer $TOKEN"
```

## Organisation du code

```
motomatch/
├── main.py         routes FastAPI et sérialisation
├── config.py       configuration par variables d'environnement
├── matching.py     score de compatibilité et distance géographique
├── privacy.py      grille géographique, anti-trilatération
├── schemas.py      validation des entrées (Pydantic)
├── repository.py   requêtes SQL
├── db.py           schéma SQLite et connexions
├── middleware.py   en-têtes de sécurité, taille des requêtes
├── audit.py        journal des évènements de sécurité
├── security/
│   ├── passwords.py  Argon2id et politique de mots de passe
│   ├── tokens.py     jetons d'accès et de rafraîchissement
│   └── ratelimit.py  limitation de débit et verrouillage
├── seed.py         profils de démonstration
├── static/         interface web (HTML/CSS/JS sans dépendance)
└── tests/          97 tests (algorithme, API, sécurité)
```

## Tests

```bash
python -m pytest motomatch/tests -q
```

Chaque test s'exécute sur une base SQLite temporaire ; aucune donnée de
développement n'est touchée. `tests/test_security.py` couvre spécifiquement les
attaques : force brute, rejeu de jeton, énumération par mesure de latence,
trilatération, accès aux données d'autrui.

## Configuration

Tous les réglages passent par des variables d'environnement préfixées
`MOTOMATCH_` (voir `.env.example`). En mode `production`, l'application **refuse
de démarrer** si le secret est resté à sa valeur de développement, si les hôtes
autorisés valent `*`, si une origine CORS est en `http://` ou si la documentation
est restée active.

## Limites connues

Ce projet est une base solide, pas encore un produit publiable. Les points
ouverts sont listés sans détour dans [SECURITY.md](SECURITY.md#limites-connues)
et [DEPLOIEMENT_STORES.md](DEPLOIEMENT_STORES.md) — les principaux :

- pas de vérification d'e-mail, donc l'inscription révèle l'existence d'un compte ;
- pas de réinitialisation de mot de passe ni de second facteur ;
- photos non hébergées ni modérées (simples URL HTTPS externes) ;
- signalements collectés mais sans interface de modération ;
- limitation de débit mono-processus (SQLite), à porter sur Redis ;
- messagerie sans chiffrement de bout en bout ni temps réel (WebSocket) ;
- aucun audit de sécurité externe n'a été mené.
