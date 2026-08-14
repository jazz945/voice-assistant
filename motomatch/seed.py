"""Jeu de données de démonstration : une douzaine de motards répartis en France.

Usage : `python -m motomatch.seed [--reset]`
Tous les comptes utilisent le mot de passe `roadtrip2024`.
"""

from __future__ import annotations

import argparse

from .db import get_connection, init_db, reset_db
from . import repository as repo
from .schemas import ProfileInput
from .security import hash_password

DEMO_PASSWORD = "roadtrip2024"

DEMO_RIDERS: list[dict] = [
    {
        "email": "camille@motomatch.example.com",
        "display_name": "Camille",
        "birth_year": 1993,
        "gender": "femme",
        "seeking": "hommes, femmes",
        "city": "Lyon",
        "latitude": 45.7640,
        "longitude": 4.8357,
        "bio": "Les cols du Vercors le dimanche, la mécanique le samedi. Je pars tôt, je rentre tard.",
        "bike_brand": "Yamaha",
        "bike_model": "MT-09",
        "bike_year": 2021,
        "engine_cc": 890,
        "bike_category": "roadster",
        "riding_styles": ["balade", "col", "mecanique"],
        "pace": "sportif",
        "experience_years": 9,
        "annual_km": 14000,
        "max_travel_km": 150,
    },
    {
        "email": "nordine@motomatch.example.com",
        "display_name": "Nordine",
        "birth_year": 1988,
        "gender": "homme",
        "seeking": "femmes",
        "city": "Grenoble",
        "latitude": 45.1885,
        "longitude": 5.7245,
        "bio": "Trail poussiéreux et bivouac. Si le GPS dit 'route non revêtue', c'est par là qu'on passe.",
        "bike_brand": "KTM",
        "bike_model": "890 Adventure R",
        "bike_year": 2022,
        "engine_cc": 889,
        "bike_category": "trail",
        "riding_styles": ["off-road", "voyage", "col"],
        "pace": "sportif",
        "experience_years": 15,
        "annual_km": 20000,
        "max_travel_km": 300,
    },
    {
        "email": "elodie@motomatch.example.com",
        "display_name": "Élodie",
        "birth_year": 1996,
        "gender": "femme",
        "seeking": "hommes",
        "city": "Saint-Étienne",
        "latitude": 45.4397,
        "longitude": 4.3872,
        "bio": "Piste au Bol d'Or, balade tranquille le reste du temps. Débutante sur circuit, patiente demandée.",
        "bike_brand": "Kawasaki",
        "bike_model": "Ninja 650",
        "bike_year": 2020,
        "engine_cc": 649,
        "bike_category": "sportive",
        "riding_styles": ["circuit", "balade"],
        "pace": "modere",
        "experience_years": 4,
        "annual_km": 8000,
        "max_travel_km": 120,
    },
    {
        "email": "thomas@motomatch.example.com",
        "display_name": "Thomas",
        "birth_year": 1985,
        "gender": "homme",
        "seeking": "femmes",
        "city": "Lyon",
        "latitude": 45.7500,
        "longitude": 4.8500,
        "bio": "Grand routier : 6 pays l'an dernier. Sacoches toujours prêtes, café obligatoire tous les 150 km.",
        "bike_brand": "BMW",
        "bike_model": "R 1250 RT",
        "bike_year": 2019,
        "engine_cc": 1254,
        "bike_category": "routiere",
        "riding_styles": ["voyage", "balade", "rassemblement"],
        "pace": "modere",
        "experience_years": 22,
        "annual_km": 25000,
        "max_travel_km": 500,
    },
    {
        "email": "sofia@motomatch.example.com",
        "display_name": "Sofia",
        "birth_year": 1991,
        "gender": "femme",
        "seeking": "hommes, femmes",
        "city": "Marseille",
        "latitude": 43.2965,
        "longitude": 5.3698,
        "bio": "Calanques au lever du soleil. Custom, chrome et virées côtières sans jamais dépasser 90.",
        "bike_brand": "Harley-Davidson",
        "bike_model": "Sportster S",
        "bike_year": 2022,
        "engine_cc": 1252,
        "bike_category": "custom",
        "riding_styles": ["balade", "rassemblement"],
        "pace": "tranquille",
        "experience_years": 7,
        "annual_km": 6000,
        "max_travel_km": 200,
    },
    {
        "email": "julien@motomatch.example.com",
        "display_name": "Julien",
        "birth_year": 1999,
        "gender": "homme",
        "seeking": "femmes",
        "city": "Villeurbanne",
        "latitude": 45.7719,
        "longitude": 4.8902,
        "bio": "Supermotard et wheeling dans les carrières. Je répare tout moi-même, y compris mes erreurs.",
        "bike_brand": "Husqvarna",
        "bike_model": "701 Supermoto",
        "bike_year": 2023,
        "engine_cc": 693,
        "bike_category": "supermotard",
        "riding_styles": ["off-road", "urbain", "mecanique"],
        "pace": "tres-sportif",
        "experience_years": 5,
        "annual_km": 9000,
        "max_travel_km": 80,
    },
    {
        "email": "marion@motomatch.example.com",
        "display_name": "Marion",
        "birth_year": 1990,
        "gender": "femme",
        "seeking": "hommes",
        "city": "Chambéry",
        "latitude": 45.5646,
        "longitude": 5.9178,
        "bio": "Trail léger, cartes papier et cols alpins. Je préfère une pause photo à un chrono.",
        "bike_brand": "Honda",
        "bike_model": "CB500X",
        "bike_year": 2021,
        "engine_cc": 471,
        "bike_category": "trail",
        "riding_styles": ["col", "voyage", "balade"],
        "pace": "modere",
        "experience_years": 11,
        "annual_km": 12000,
        "max_travel_km": 250,
    },
    {
        "email": "karim@motomatch.example.com",
        "display_name": "Karim",
        "birth_year": 1994,
        "gender": "homme",
        "seeking": "femmes",
        "city": "Paris",
        "latitude": 48.8566,
        "longitude": 2.3522,
        "bio": "Roadster et périph au petit matin. Sorties Normandie une fois par mois, départ 7h pile.",
        "bike_brand": "Triumph",
        "bike_model": "Street Triple 765",
        "bike_year": 2023,
        "engine_cc": 765,
        "bike_category": "roadster",
        "riding_styles": ["urbain", "balade", "circuit"],
        "pace": "sportif",
        "experience_years": 8,
        "annual_km": 16000,
        "max_travel_km": 200,
    },
    {
        "email": "lea@motomatch.example.com",
        "display_name": "Léa",
        "birth_year": 1997,
        "gender": "femme",
        "seeking": "hommes, femmes",
        "city": "Bordeaux",
        "latitude": 44.8378,
        "longitude": -0.5792,
        "bio": "Sportive assumée, journées circuit à Nogaro. Cherche quelqu'un qui sait aussi ralentir.",
        "bike_brand": "Ducati",
        "bike_model": "Panigale V2",
        "bike_year": 2022,
        "engine_cc": 955,
        "bike_category": "sportive",
        "riding_styles": ["circuit", "mecanique"],
        "pace": "tres-sportif",
        "experience_years": 6,
        "annual_km": 10000,
        "max_travel_km": 400,
    },
    {
        "email": "pierre@motomatch.example.com",
        "display_name": "Pierre",
        "birth_year": 1980,
        "gender": "homme",
        "seeking": "femmes",
        "city": "Annecy",
        "latitude": 45.8992,
        "longitude": 6.1294,
        "bio": "Vingt ans de cols savoyards. Guide bénévole pour les sorties club, thermos dans le top-case.",
        "bike_brand": "Moto Guzzi",
        "bike_model": "V85 TT",
        "bike_year": 2020,
        "engine_cc": 853,
        "bike_category": "trail",
        "riding_styles": ["col", "balade", "rassemblement"],
        "pace": "modere",
        "experience_years": 24,
        "annual_km": 15000,
        "max_travel_km": 300,
    },
    {
        "email": "ines@motomatch.example.com",
        "display_name": "Inès",
        "birth_year": 1995,
        "gender": "femme",
        "seeking": "hommes",
        "city": "Lyon",
        "latitude": 45.7700,
        "longitude": 4.8300,
        "bio": "Roadster, garage collectif et sorties improvisées. Je monte aussi en duo sans râler.",
        "bike_brand": "Suzuki",
        "bike_model": "SV650",
        "bike_year": 2019,
        "engine_cc": 645,
        "bike_category": "roadster",
        "riding_styles": ["balade", "mecanique", "urbain"],
        "pace": "modere",
        "experience_years": 6,
        "annual_km": 9000,
        "max_travel_km": 150,
    },
    {
        "email": "vincent@motomatch.example.com",
        "display_name": "Vincent",
        "birth_year": 1987,
        "gender": "homme",
        "seeking": "femmes",
        "city": "Valence",
        "latitude": 44.9333,
        "longitude": 4.8917,
        "bio": "Cross le week-end, trail pour aller au terrain. Boue obligatoire, douche ensuite.",
        "bike_brand": "Yamaha",
        "bike_model": "WR450F",
        "bike_year": 2021,
        "engine_cc": 450,
        "bike_category": "cross",
        "riding_styles": ["off-road", "mecanique"],
        "pace": "sportif",
        "experience_years": 13,
        "annual_km": 5000,
        "max_travel_km": 120,
    },
]


def seed(reset: bool = False) -> int:
    """Insère les profils de démonstration. Retourne le nombre de comptes créés."""
    if reset:
        reset_db()
    else:
        init_db()

    created = 0
    password_hash = hash_password(DEMO_PASSWORD)
    with get_connection() as conn:
        for rider in DEMO_RIDERS:
            data = dict(rider)
            email = data.pop("email")
            existing = repo.get_user_by_email(conn, email)
            if existing is not None:
                user_id = int(existing["id"])
            else:
                user_id = repo.create_user(conn, email, password_hash)
                created += 1
            repo.upsert_profile(conn, user_id, ProfileInput(**data))
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Charge les profils de démonstration MotoMatch.")
    parser.add_argument("--reset", action="store_true", help="vide la base avant l'insertion")
    args = parser.parse_args()
    created = seed(reset=args.reset)
    print(f"{created} nouveaux comptes créés, {len(DEMO_RIDERS)} profils à jour.")
    print(f"Mot de passe commun : {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
