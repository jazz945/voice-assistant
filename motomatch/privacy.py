"""Protection de la localisation des utilisateurs.

C'est le risque le plus concret d'une application de rencontre géolocalisée :
si l'API renvoie une distance précise, un attaquant qui déplace sa propre
position déclarée trois fois retrouve par trilatération l'adresse exacte de sa
cible. Tinder et Grindr ont tous deux été vulnérables à cette attaque.

Trois mesures se combinent ici :

1. **Les coordonnées d'autrui ne sortent jamais de l'API.** Seul l'utilisateur
   connecté peut lire ses propres `latitude`/`longitude` (cf. `public_profile`).
2. **Les positions comparées sont plaquées sur une grille.** La distance est
   calculée depuis le centre de la cellule du candidat, pas depuis son point
   réel : la trilatération ne peut donc jamais faire mieux que la taille de
   cellule (1 km par défaut).
3. **Le décalage de grille est propre à chaque utilisateur et déterministe.**
   Déterministe est essentiel : un bruit tiré à chaque requête serait moyenné
   par un attaquant qui interroge en boucle, et la position vraie réapparaîtrait.

La distance renvoyée est en plus arrondie par paliers, ce qui gêne la lecture
des franchissements de seuil sans constituer à soi seul une protection.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from typing import Any, Mapping

# Un degré de latitude vaut ~111,32 km partout ; la longitude se resserre vers
# les pôles, d'où le facteur cos(latitude).
METERS_PER_DEGREE_LATITUDE = 111_320.0

# Champs qui ne doivent jamais apparaître dans le profil vu par un tiers.
PRIVATE_PROFILE_FIELDS = frozenset({"latitude", "longitude", "updated_at"})


def _user_grid_offset(user_id: int, secret_key: str, grid_meters: int) -> tuple[float, float]:
    """Décalage stable, propre à l'utilisateur, inférieur à une cellule.

    Dérivé d'un HMAC du secret serveur : impossible à prédire côté client, mais
    identique à chaque appel pour un même utilisateur.
    """
    digest = hmac.new(
        secret_key.encode(), f"geo-grid:{user_id}".encode(), hashlib.sha256
    ).digest()
    # Deux entiers indépendants tirés de l'empreinte, ramenés dans [0, 1).
    north = int.from_bytes(digest[0:8], "big") / float(1 << 64)
    east = int.from_bytes(digest[8:16], "big") / float(1 << 64)
    return north * grid_meters, east * grid_meters


def snap_to_grid(
    latitude: float,
    longitude: float,
    *,
    user_id: int,
    secret_key: str,
    grid_meters: int,
) -> tuple[float, float]:
    """Ramène une position au centre de sa cellule de grille."""
    offset_north, offset_east = _user_grid_offset(user_id, secret_key, grid_meters)

    lat_step = grid_meters / METERS_PER_DEGREE_LATITUDE
    # Près des pôles cos(lat) tend vers 0 : on borne pour éviter une cellule
    # de longitude infinie (et une division par zéro).
    cos_latitude = max(0.01, math.cos(math.radians(latitude)))
    lon_step = grid_meters / (METERS_PER_DEGREE_LATITUDE * cos_latitude)

    shifted_lat = latitude + offset_north / METERS_PER_DEGREE_LATITUDE
    shifted_lon = longitude + offset_east / (METERS_PER_DEGREE_LATITUDE * cos_latitude)

    snapped_lat = math.floor(shifted_lat / lat_step) * lat_step + lat_step / 2
    snapped_lon = math.floor(shifted_lon / lon_step) * lon_step + lon_step / 2

    return (
        max(-90.0, min(90.0, snapped_lat - offset_north / METERS_PER_DEGREE_LATITUDE)),
        snapped_lon - offset_east / (METERS_PER_DEGREE_LATITUDE * cos_latitude),
    )


def bucket_distance(distance_km: float, bucket_km: int) -> float:
    """Arrondit une distance au palier supérieur, avec un plancher au palier."""
    if bucket_km <= 1:
        return round(distance_km, 1)
    return float(max(bucket_km, math.ceil(distance_km / bucket_km) * bucket_km))


def public_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Profil tel qu'un tiers a le droit de le voir : sans coordonnées."""
    return {
        key: value for key, value in profile.items() if key not in PRIVATE_PROFILE_FIELDS
    }
