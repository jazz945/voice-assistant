# MotoMatch — application de rencontre pour motards

Une application de rencontre complète, pensée pour les motards : le score de
compatibilité ne repose pas seulement sur la distance, mais sur ce qui décide
vraiment si deux personnes rouleront bien ensemble — la famille de moto, le
rythme de conduite, la cylindrée, les pratiques et l'expérience.

Backend FastAPI + SQLite, interface web sans aucune dépendance JavaScript.

## Démarrage rapide

```bash
pip install -r motomatch/requirements.txt

# Charge une douzaine de profils de démonstration (mot de passe : roadtrip2024)
python -m motomatch.seed --reset

uvicorn motomatch.main:app --reload
```

Ouvre ensuite <http://127.0.0.1:8000>. La documentation interactive de l'API est
sur <http://127.0.0.1:8000/docs>.

Compte de démonstration : `camille@motomatch.example.com` / `roadtrip2024`.

Le fichier SQLite est créé à côté du code ; on peut le déplacer avec la variable
d'environnement `MOTOMATCH_DB`.

## Fonctionnalités

- **Inscription et session** — mot de passe haché en PBKDF2-HMAC-SHA256, jetons
  de session porteurs (`Authorization: Bearer …`).
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
| `POST` | `/api/auth/register` | Création de compte, renvoie un jeton. |
| `POST` | `/api/auth/login` | Connexion. |
| `POST` | `/api/auth/logout` | Invalide le jeton courant. |
| `GET` | `/api/me` | Compte et profil de l'utilisateur connecté. |
| `PUT` | `/api/me/profile` | Crée ou met à jour le profil moto. |
| `GET` | `/api/discover` | Candidats triés par compatibilité. Paramètres : `max_distance_km`, `min_age`, `max_age`, `categories`, `styles`, `limit`. |
| `POST` | `/api/swipes` | `{"target_user_id": 3, "direction": "like"}` → `{"matched": true, "match_id": 1}`. |
| `GET` | `/api/matches` | Matchs, avec le dernier message de chaque conversation. |
| `GET` | `/api/matches/{id}/messages` | Fil de discussion. |
| `POST` | `/api/matches/{id}/messages` | Envoi d'un message. |

Exemple :

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"camille@motomatch.example.com","password":"roadtrip2024"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')

curl -s "localhost:8000/api/discover?max_distance_km=150&styles=col" \
  -H "Authorization: Bearer $TOKEN"
```

## Organisation du code

```
motomatch/
├── main.py         routes FastAPI et sérialisation
├── matching.py     score de compatibilité et distance géographique
├── schemas.py      validation des entrées (Pydantic)
├── repository.py   requêtes SQL
├── db.py           schéma SQLite et connexions
├── security.py     hachage des mots de passe, jetons
├── seed.py         profils de démonstration
├── static/         interface web (HTML/CSS/JS sans dépendance)
└── tests/          44 tests (algorithme + API de bout en bout)
```

## Tests

```bash
python -m pytest motomatch/tests -q
```

Chaque test s'exécute sur une base SQLite temporaire ; aucune donnée de
développement n'est touchée.

## Limites connues

Ce projet est une base fonctionnelle, pas un produit prêt pour la production :

- les jetons de session n'expirent pas et il n'y a pas de rafraîchissement ;
- pas de limitation de débit sur la connexion ni de vérification d'e-mail ;
- les photos sont de simples URL, sans envoi ni modération de fichiers ;
- le champ `seeking` est du texte libre et n'est pas utilisé comme filtre ;
- pas de signalement ni de blocage d'utilisateur ;
- la messagerie se rafraîchit à l'envoi, sans temps réel (WebSocket).
