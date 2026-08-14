# MotoMatch — application de rencontre pour motards

Une application de rencontre complète, pensée pour les motards : le score de
compatibilité ne repose pas seulement sur la distance, mais sur ce qui décide
vraiment si deux personnes rouleront bien ensemble — la famille de moto, le
rythme de conduite, la cylindrée, les pratiques et l'expérience.

Backend FastAPI + SQLite, interface web sans aucune dépendance JavaScript, et
une posture de sécurité pensée pour une application de rencontre : Argon2id,
sessions révocables, protection contre la trilatération géographique, blocage,
signalement et suppression de compte. Détail complet dans **[SECURITY.md](SECURITY.md)**.

Pour l'héberger chez soi — Docker, HTTPS automatique, sauvegardes, démarrage
au boot : **[../deploy/README.md](../deploy/README.md)**.

L'interface est **installable sur Android et iOS** (PWA : manifeste, service
worker, icônes) et une application Android existe dans
**[../android/](../android/README.md)** ; le chemin vers l'App Store et Google
Play est décrit dans **[DEPLOIEMENT_STORES.md](DEPLOIEMENT_STORES.md)**.

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
- **Couleur par famille de moto** — chaque famille a sa teinte, reprise sur le
  casque, le filet de la carte et le cercle de score : on repère une sportive
  d'un coup d'œil sans lire. À défaut de photo, le profil affiche un **casque
  dessiné** plutôt qu'un visage inventé (`static/avatars.js`).
- **Croisements** — comme Happn, mais pour la route : quand deux motards passent
  au même endroit au même moment, l'application le retient et précise *comment*
  ils se sont croisés (en roulant ou à l'arrêt, en sens inverse ou dans le même
  sens). Un **salut motard** peut être envoyé ; s'il est rendu, c'est un match.
  Opt-in strict, aucune coordonnée GPS conservée — voir [SECURITY.md](SECURITY.md).
- **Balades** — créer une sortie (départ, date, distance, rythme, type de route,
  motos bienvenues) avec trois niveaux d'**autorisation** : ouverte à tous,
  réservée à ses matchs, ou sur validation de l'organisateur. Le point de
  rendez-vous exact n'est révélé qu'aux participants acceptés.

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
| `PUT` | `/api/me/crossings` | Active ou coupe les croisements (coupure = effacement). |
| `POST` | `/api/crossings/ping` | Signale une position (réduite à une cellule). |
| `GET` | `/api/crossings` | Motards croisés, avec contexte et sens. |
| `POST` | `/api/crossings/{id}/salut` | Salut motard ; rendu, il crée un match. |
| `DELETE` | `/api/crossings` | Efface tout l'historique de croisements. |
| `POST` | `/api/rides` | Crée une balade. |
| `GET` | `/api/rides` | Balades à venir visibles. Filtres : `max_distance_km`, `pace`, `route_type`. |
| `GET` | `/api/rides/{id}` | Détail et participants. |
| `POST` | `/api/rides/{id}/join` | Rejoint, ou dépose une demande. |
| `DELETE` | `/api/rides/{id}/join` | Se désiste. |
| `POST` | `/api/rides/{id}/participants/{user_id}` | Accepte ou refuse une demande (organisateur). |
| `DELETE` | `/api/rides/{id}` | Annule la balade (organisateur). |

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
├── crossings.py    détection et classification des croisements
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
├── tools/          génération des icônes
└── tests/          149 tests (algorithme, API, sécurité, croisements, balades, PWA)
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
- croisements : une position déclarée par un client modifié n'est pas détectée ;
- balades sans messagerie de groupe ni rappel avant le départ ;
- aucun audit de sécurité externe n'a été mené.
