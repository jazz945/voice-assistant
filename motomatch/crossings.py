"""Croisements : « on s'est croisés sur la route ».

Le principe est celui de Happn — deux personnes qui passent au même endroit au
même moment se voient proposées — mais transposé au monde motard : ce qui compte
n'est pas d'avoir marché dans la même rue, c'est de **s'être croisés en roulant**,
en sens inverse, sur une belle route.

## Confidentialité

C'est la fonction la plus intrusive de l'application, et elle est traitée comme
telle :

- **Opt-in strict.** Rien n'est enregistré tant que l'utilisateur n'a pas activé
  la fonction, et il peut la couper ou tout effacer à tout moment.
- **Aucune coordonnée GPS brute n'est stockée.** Une position envoyée est
  immédiatement réduite à un identifiant de cellule (500 m par défaut) et à un
  créneau horaire, puis la position d'origine est jetée.
- **Rétention courte.** Les positions sont purgées au bout de 24 heures ; seuls
  les croisements avérés survivent.
- **Réciprocité.** Un croisement n'existe que si les deux personnes ont activé la
  fonction. On ne peut pas se rendre invisible tout en continuant à voir.
- Les personnes bloquées ne se croisent jamais.

Le croisement révèle donc à chacun qu'il a été au même endroit que l'autre — ce
qui est le but de la fonction et ce à quoi les deux ont consenti — mais jamais un
trajet, jamais une position exacte, et jamais l'historique de quelqu'un qui n'a
pas activé la fonction.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from .matching import haversine_km
from .privacy import METERS_PER_DEGREE_LATITUDE

# Contexte du croisement, déduit de la vitesse des deux motards.
CONTEXT_ROAD = "route"        # les deux roulaient
CONTEXT_STOPPED = "arret"     # les deux à l'arrêt (café, station, parking)
CONTEXT_MIXED = "mixte"       # l'un roulait, l'autre non

# Sens relatif, déduit des caps.
DIRECTION_OPPOSITE = "sens-inverse"  # le vrai croisement, celui du salut motard
DIRECTION_SAME = "meme-sens"         # vous alliez au même endroit
DIRECTION_CROSSING = "oblique"
DIRECTION_UNKNOWN = "inconnu"

# Au-delà de cette vitesse, on considère que la personne roule.
MOVING_SPEED_KMH = 20.0


@dataclass(frozen=True)
class PingContext:
    """Ce qu'on retient d'une position, une fois la coordonnée jetée."""

    cell_id: str
    time_bucket: str
    speed_kmh: float | None
    heading_deg: float | None
    ride_id: int | None


def cell_id(latitude: float, longitude: float, cell_meters: int) -> str:
    """Identifiant de la cellule de grille contenant ce point.

    Contrairement à la grille de `privacy.py`, celle-ci est **globale** : pour
    que deux personnes se croisent, elles doivent tomber dans la même cellule,
    ce qu'un décalage propre à chaque utilisateur rendrait impossible. La
    contrepartie — une cellule est une position approximative — est compensée
    par la rétention courte et le caractère opt-in de la fonction.
    """
    lat_step = cell_meters / METERS_PER_DEGREE_LATITUDE
    cos_latitude = max(0.01, math.cos(math.radians(latitude)))
    lon_step = cell_meters / (METERS_PER_DEGREE_LATITUDE * cos_latitude)
    return f"{math.floor(latitude / lat_step)}:{math.floor(longitude / lon_step)}"


def neighbouring_cells(latitude: float, longitude: float, cell_meters: int) -> list[str]:
    """La cellule du point et ses huit voisines.

    Sans cela, deux motards séparés de vingt mètres mais de part et d'autre
    d'une frontière de cellule ne se croiseraient jamais.
    """
    lat_step = cell_meters / METERS_PER_DEGREE_LATITUDE
    cos_latitude = max(0.01, math.cos(math.radians(latitude)))
    lon_step = cell_meters / (METERS_PER_DEGREE_LATITUDE * cos_latitude)
    return [
        cell_id(latitude + d_lat * lat_step, longitude + d_lon * lon_step, cell_meters)
        for d_lat in (-1, 0, 1)
        for d_lon in (-1, 0, 1)
    ]


def cell_centre(cell: str, cell_meters: int) -> tuple[float, float]:
    """Centre approximatif d'une cellule — ce qu'on montre sur une carte.

    On ne restitue jamais la position réelle de quelqu'un, seulement le centre
    de la zone où le croisement a eu lieu. Le demandeur y était lui-même.
    """
    lat_index, lon_index = (int(part) for part in cell.split(":"))
    lat_step = cell_meters / METERS_PER_DEGREE_LATITUDE
    latitude = (lat_index + 0.5) * lat_step
    cos_latitude = max(0.01, math.cos(math.radians(latitude)))
    lon_step = cell_meters / (METERS_PER_DEGREE_LATITUDE * cos_latitude)
    return round(latitude, 4), round((lon_index + 0.5) * lon_step, 4)


def time_bucket(conn: sqlite3.Connection, bucket_minutes: int) -> str:
    """Créneau horaire courant, arrondi vers le bas.

    Sert de clé de déduplication : deux motards arrêtés côte à côte pendant une
    heure produisent un croisement par créneau, pas un par ping.
    """
    row = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M', "
        "  datetime((strftime('%s', 'now') / (? * 60)) * (? * 60), 'unixepoch')"
        ") AS bucket",
        (bucket_minutes, bucket_minutes),
    ).fetchone()
    return str(row["bucket"])


def classify_context(speed_a: float | None, speed_b: float | None) -> str:
    """Roulaient-ils, ou étaient-ils à l'arrêt ?"""
    if speed_a is None or speed_b is None:
        return CONTEXT_MIXED
    moving_a = speed_a >= MOVING_SPEED_KMH
    moving_b = speed_b >= MOVING_SPEED_KMH
    if moving_a and moving_b:
        return CONTEXT_ROAD
    if not moving_a and not moving_b:
        return CONTEXT_STOPPED
    return CONTEXT_MIXED


def classify_direction(heading_a: float | None, heading_b: float | None) -> str:
    """Sens relatif des deux motos.

    Le sens inverse est le croisement au sens propre : celui où l'on se fait un
    signe sans jamais s'arrêter. C'est le cas le plus intéressant à remonter.
    """
    if heading_a is None or heading_b is None:
        return DIRECTION_UNKNOWN
    difference = abs((heading_a - heading_b + 180) % 360 - 180)
    if difference >= 135:
        return DIRECTION_OPPOSITE
    if difference <= 45:
        return DIRECTION_SAME
    return DIRECTION_CROSSING


def describe(context: str, direction: str, times: int) -> str:
    """Phrase affichée sur la carte du croisement."""
    if context == CONTEXT_ROAD and direction == DIRECTION_OPPOSITE:
        base = "Croisés sur la route, en sens inverse"
    elif context == CONTEXT_ROAD and direction == DIRECTION_SAME:
        base = "Vous rouliez dans le même sens"
    elif context == CONTEXT_ROAD:
        base = "Croisés sur la route"
    elif context == CONTEXT_STOPPED:
        base = "Croisés à l'arrêt — pause café ou station"
    else:
        base = "Croisés en chemin"
    return f"{base} · {times} fois" if times > 1 else base


def distance_between_cells(cell_a: str, cell_b: str, cell_meters: int) -> float:
    a = cell_centre(cell_a, cell_meters)
    b = cell_centre(cell_b, cell_meters)
    return haversine_km(a[0], a[1], b[0], b[1])
