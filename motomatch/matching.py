"""Algorithme de compatibilité entre motards.

Le score n'est pas un simple filtre géographique : deux motards s'entendent
surtout s'ils peuvent *rouler ensemble*. On pondère donc la distance, mais aussi
la famille de moto, le rythme de conduite, la cylindrée et l'expérience.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Mapping, Sequence

# Familles de motos reconnues par l'application.
BIKE_CATEGORIES = (
    "sportive",
    "roadster",
    "trail",
    "routiere",
    "custom",
    "supermotard",
    "cross",
    "scooter",
)

# Pratiques déclarées sur le profil.
RIDING_STYLES = (
    "balade",
    "voyage",
    "circuit",
    "col",
    "off-road",
    "urbain",
    "rassemblement",
    "mecanique",
)

# Rythme de conduite, ordonné du plus tranquille au plus vif.
PACE_LEVELS = ("tranquille", "modere", "sportif", "tres-sportif")

# Affinité entre familles de motos : à quel point deux montures se suivent bien
# sur la même route. 1.0 = même univers, 0.2 = rythmes incompatibles.
_CATEGORY_AFFINITY: dict[frozenset[str], float] = {
    frozenset({"sportive", "supermotard"}): 0.7,
    frozenset({"sportive", "roadster"}): 0.75,
    frozenset({"sportive", "routiere"}): 0.5,
    frozenset({"sportive", "trail"}): 0.45,
    frozenset({"sportive", "custom"}): 0.2,
    frozenset({"sportive", "cross"}): 0.3,
    frozenset({"sportive", "scooter"}): 0.15,
    frozenset({"roadster", "supermotard"}): 0.7,
    frozenset({"roadster", "trail"}): 0.65,
    frozenset({"roadster", "routiere"}): 0.6,
    frozenset({"roadster", "custom"}): 0.45,
    frozenset({"roadster", "cross"}): 0.3,
    frozenset({"roadster", "scooter"}): 0.3,
    frozenset({"trail", "routiere"}): 0.8,
    frozenset({"trail", "cross"}): 0.7,
    frozenset({"trail", "supermotard"}): 0.55,
    frozenset({"trail", "custom"}): 0.4,
    frozenset({"trail", "scooter"}): 0.25,
    frozenset({"routiere", "custom"}): 0.7,
    frozenset({"routiere", "supermotard"}): 0.35,
    frozenset({"routiere", "cross"}): 0.2,
    frozenset({"routiere", "scooter"}): 0.3,
    frozenset({"custom", "supermotard"}): 0.25,
    frozenset({"custom", "cross"}): 0.2,
    frozenset({"custom", "scooter"}): 0.35,
    frozenset({"supermotard", "cross"}): 0.75,
    frozenset({"supermotard", "scooter"}): 0.25,
    frozenset({"cross", "scooter"}): 0.15,
}

# Poids des composantes du score final (somme = 1.0).
WEIGHTS = {
    "distance": 0.30,
    "styles": 0.25,
    "category": 0.15,
    "pace": 0.15,
    "engine": 0.08,
    "experience": 0.07,
}

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique en kilomètres entre deux points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def parse_styles(raw: str | Sequence[str] | None) -> list[str]:
    """Normalise les pratiques, stockées en CSV dans la base."""
    if raw is None:
        return []
    values = raw.split(",") if isinstance(raw, str) else list(raw)
    seen: list[str] = []
    for value in values:
        style = value.strip().lower()
        if style and style not in seen:
            seen.append(style)
    return seen


def age_from_birth_year(birth_year: int, today: date | None = None) -> int:
    return (today or date.today()).year - birth_year


def category_affinity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return _CATEGORY_AFFINITY.get(frozenset({a, b}), 0.4)


def _distance_score(distance_km: float, viewer_max: int, candidate_max: int) -> float:
    """1.0 en dessous du rayon partagé, décroissance douce au-delà."""
    reach = max(20, min(viewer_max, candidate_max))
    if distance_km <= reach:
        return 1.0
    # Au-delà du rayon commun, on tolère encore le double avant d'annuler.
    overshoot = (distance_km - reach) / reach
    return max(0.0, 1.0 - overshoot)


def _styles_score(viewer: Sequence[str], candidate: Sequence[str]) -> float:
    """Indice de Jaccard entre les pratiques déclarées."""
    a, b = set(viewer), set(candidate)
    if not a or not b:
        return 0.5  # profil incomplet : score neutre plutôt que pénalisant
    return len(a & b) / len(a | b)


def _pace_score(viewer: str, candidate: str) -> float:
    try:
        gap = abs(PACE_LEVELS.index(viewer) - PACE_LEVELS.index(candidate))
    except ValueError:
        return 0.5
    return max(0.0, 1.0 - gap / (len(PACE_LEVELS) - 1))


def _engine_score(viewer_cc: int, candidate_cc: int) -> float:
    """Rapproche les cylindrées : un 125 et un 1200 ne roulent pas au même rythme."""
    if viewer_cc <= 0 or candidate_cc <= 0:
        return 0.5
    ratio = min(viewer_cc, candidate_cc) / max(viewer_cc, candidate_cc)
    return ratio**0.5  # racine : tolère un écart modéré sans tout écraser


def _experience_score(viewer_years: int, candidate_years: int) -> float:
    gap = abs(viewer_years - candidate_years)
    return max(0.0, 1.0 - gap / 20)


def compatibility(
    viewer: Mapping[str, object],
    candidate: Mapping[str, object],
    distance_km: float | None = None,
) -> dict[str, object]:
    """Score de compatibilité sur 100 et détail des composantes.

    `viewer` et `candidate` sont des profils (lignes SQLite ou dictionnaires).
    """
    if distance_km is None:
        distance_km = haversine_km(
            float(viewer["latitude"]),
            float(viewer["longitude"]),
            float(candidate["latitude"]),
            float(candidate["longitude"]),
        )

    parts = {
        "distance": _distance_score(
            distance_km, int(viewer["max_travel_km"]), int(candidate["max_travel_km"])
        ),
        "styles": _styles_score(
            parse_styles(viewer["riding_styles"]), parse_styles(candidate["riding_styles"])
        ),
        "category": category_affinity(
            str(viewer["bike_category"]), str(candidate["bike_category"])
        ),
        "pace": _pace_score(str(viewer["pace"]), str(candidate["pace"])),
        "engine": _engine_score(int(viewer["engine_cc"]), int(candidate["engine_cc"])),
        "experience": _experience_score(
            int(viewer["experience_years"]), int(candidate["experience_years"])
        ),
    }
    score = sum(parts[key] * weight for key, weight in WEIGHTS.items())
    return {
        "score": round(score * 100, 1),
        "distance_km": round(distance_km, 1),
        "breakdown": {key: round(value, 3) for key, value in parts.items()},
        "highlights": _highlights(viewer, candidate, parts),
    }


def _highlights(
    viewer: Mapping[str, object],
    candidate: Mapping[str, object],
    parts: Mapping[str, float],
) -> list[str]:
    """Petites phrases affichées sur la carte pour expliquer le score."""
    notes: list[str] = []
    shared = [s for s in parse_styles(viewer["riding_styles"]) if s in parse_styles(candidate["riding_styles"])]
    if shared:
        notes.append("Pratiques communes : " + ", ".join(shared))
    if parts["category"] >= 0.9:
        notes.append(f"Même famille de moto ({candidate['bike_category']})")
    if parts["pace"] == 1.0:
        notes.append(f"Même rythme de conduite ({candidate['pace']})")
    if parts["engine"] >= 0.85:
        notes.append("Cylindrées compatibles pour rouler ensemble")
    if parts["experience"] >= 0.9:
        notes.append("Expérience de conduite similaire")
    return notes[:3]
