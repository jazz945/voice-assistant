"""Tests de l'algorithme de compatibilité."""

from __future__ import annotations

import pytest

from motomatch.matching import (
    WEIGHTS,
    _distance_score,
    _engine_score,
    _pace_score,
    _styles_score,
    category_affinity,
    compatibility,
    haversine_km,
    parse_styles,
)


def rider(**overrides):
    base = {
        "latitude": 45.7640,
        "longitude": 4.8357,
        "max_travel_km": 150,
        "riding_styles": "balade,col",
        "bike_category": "roadster",
        "pace": "sportif",
        "engine_cc": 890,
        "experience_years": 9,
    }
    base.update(overrides)
    return base


def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_haversine_lyon_marseille():
    # Distance réelle à vol d'oiseau : ~277 km.
    distance = haversine_km(45.7640, 4.8357, 43.2965, 5.3698)
    assert 270 < distance < 285


def test_haversine_same_point_is_zero():
    assert haversine_km(45.0, 4.0, 45.0, 4.0) == pytest.approx(0.0)


def test_parse_styles_normalises_and_dedupes():
    assert parse_styles(" Balade , col ,balade, ") == ["balade", "col"]
    assert parse_styles(None) == []
    assert parse_styles(["Circuit", "circuit"]) == ["circuit"]


def test_identical_riders_score_is_maximal():
    result = compatibility(rider(), rider())
    assert result["score"] == pytest.approx(100.0)
    assert result["distance_km"] == 0.0


def test_opposite_riders_score_low():
    far_away = rider(
        latitude=48.8566,
        longitude=2.3522,
        riding_styles="off-road",
        bike_category="cross",
        pace="tranquille",
        engine_cc=125,
        experience_years=1,
        max_travel_km=30,
    )
    assert compatibility(rider(), far_away)["score"] < 25


def test_distance_score_inside_shared_reach():
    assert _distance_score(40, viewer_max=100, candidate_max=80) == 1.0
    # Au-delà du rayon commun le score décroît, puis s'annule au double.
    assert 0 < _distance_score(120, 100, 80) < 1
    assert _distance_score(200, 100, 80) == 0.0


def test_distance_score_uses_the_more_restrictive_radius():
    assert _distance_score(90, viewer_max=500, candidate_max=50) < 1.0


def test_category_affinity_is_symmetric_and_bounded():
    assert category_affinity("sportive", "custom") == category_affinity("custom", "sportive")
    assert category_affinity("trail", "trail") == 1.0
    assert category_affinity("trail", "routiere") > category_affinity("sportive", "custom")


def test_category_affinity_defaults_for_unknown_pairs():
    assert category_affinity("roadster", "inconnue") == 0.4


def test_styles_score_is_jaccard():
    assert _styles_score(["balade", "col"], ["balade", "col"]) == 1.0
    assert _styles_score(["balade", "col"], ["balade"]) == pytest.approx(0.5)
    assert _styles_score(["circuit"], ["off-road"]) == 0.0
    # Profil sans pratique déclarée : neutre, pas pénalisant.
    assert _styles_score([], ["circuit"]) == 0.5


def test_pace_score_decreases_with_gap():
    assert _pace_score("modere", "modere") == 1.0
    assert _pace_score("tranquille", "tres-sportif") == 0.0
    assert 0 < _pace_score("modere", "sportif") < 1


def test_engine_score_penalises_large_gaps():
    assert _engine_score(650, 650) == 1.0
    assert _engine_score(125, 1250) < _engine_score(650, 900)


def test_highlights_mention_shared_practices():
    result = compatibility(rider(), rider(riding_styles="balade,col"))
    assert any("Pratiques communes" in note for note in result["highlights"])
    assert len(result["highlights"]) <= 3


def test_explicit_distance_overrides_coordinates():
    result = compatibility(rider(), rider(), distance_km=42.0)
    assert result["distance_km"] == 42.0
