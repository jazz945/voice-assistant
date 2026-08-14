"""Tests de l'installabilité en application (PWA).

Chrome refuse d'installer une application si l'un de ces éléments manque : le
manifeste, un service worker de portée racine, ou une icône d'au moins 192 px.
Ces tests figent ces conditions pour qu'une refonte ne les casse pas en silence.
"""

from __future__ import annotations

import json
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


def test_manifest_is_served_with_the_right_media_type(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")


def test_manifest_meets_android_install_criteria(client):
    manifest = client.get("/manifest.webmanifest").json()

    assert manifest["name"] and manifest["short_name"]
    assert manifest["start_url"] == "/"
    # `standalone` est ce qui retire la barre d'adresse une fois installée.
    assert manifest["display"] == "standalone"

    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert "192x192" in sizes  # exigée par Chrome pour proposer l'installation
    assert "512x512" in sizes  # écran de démarrage
    # Sans icône `maskable`, Android plaque l'icône sur un fond blanc.
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])


def test_service_worker_is_served_from_the_root(client):
    """Sa portée est celle du répertoire qui le sert : il doit être à la racine."""
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    # Servi depuis /static/, il ne contrôlerait pas la page d'accueil.
    assert client.get("/static/sw.js").status_code == 200  # le fichier existe bien


def test_service_worker_never_caches_the_api():
    """Profils, messages et positions ne doivent pas rester dans le Cache Storage."""
    source = (STATIC / "sw.js").read_text()
    assert 'url.pathname.startsWith("/api/")' in source
    assert "return;" in source


def test_declared_icons_all_exist(client):
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())
    for icon in manifest["icons"]:
        assert (STATIC.parent / icon["src"].lstrip("/")).exists(), icon["src"]
        assert client.get(icon["src"]).status_code == 200


def test_index_declares_the_manifest_and_mobile_metadata(client):
    html = client.get("/").text
    assert 'rel="manifest"' in html
    assert 'name="theme-color"' in html
    assert 'rel="apple-touch-icon"' in html


def test_security_headers_allow_the_service_worker(client):
    csp = client.get("/").headers["content-security-policy"]
    assert "worker-src 'self'" in csp
    assert "manifest-src 'self'" in csp


# --- Avatars et couleurs de famille ----------------------------------------


def test_every_bike_family_has_a_colour_in_both_files():
    """Les teintes sont écrites deux fois : en JS pour dessiner les casques,
    en CSS pour teinter les cartes. Rien n'empêche l'une de dériver de l'autre,
    d'où ce test — et la vérification qu'aucune famille reconnue par l'API n'est
    oubliée des deux côtés."""
    import re

    from motomatch.matching import BIKE_CATEGORIES

    js = (STATIC / "avatars.js").read_text()
    css = (STATIC / "styles.css").read_text()

    in_js = set(re.findall(r"^  (\w+): \{ light", js, re.M))
    in_css = set(re.findall(r"\.famille-(\w+)\s*\{", css)) - {"inconnue"}

    assert in_js == in_css, f"teintes désynchronisées : {in_js ^ in_css}"
    assert set(BIKE_CATEGORIES) == in_js, (
        f"familles sans couleur : {set(BIKE_CATEGORIES) - in_js}"
    )


def test_avatars_never_use_inline_styles(client):
    """La politique de sécurité interdit `style-src 'unsafe-inline'` : un attribut
    `style` serait silencieusement ignoré, et la couleur ne s'appliquerait pas.
    C'est arrivé une fois — d'où ce garde-fou."""
    import re

    js = (STATIC / "avatars.js").read_text() + (STATIC / "app.js").read_text()
    # `data-style="…"` est un attribut de données légitime : on ne cherche que
    # l'attribut `style` lui-même.
    assert not re.search(r'(?<![-\w])style="', js)
    assert "style-src 'self'" in client.get("/").headers["content-security-policy"]


def test_a_missing_photo_falls_back_to_the_drawn_helmet():
    """Le casque est toujours rendu, la photo se pose par-dessus : une URL morte
    laisse donc apparaître le casque, sans gestionnaire `onerror` — que le CSP
    bloquerait de toute façon."""
    import re

    js = (STATIC / "avatars.js").read_text()
    # Le mot apparaît dans le commentaire qui explique son absence : on cherche
    # le gestionnaire lui-même, pas la mention.
    assert not re.search(r"onerror\s*[=:]", js)
    assert "avatar-photo" in js and "helmetSvg" in js


def test_the_service_worker_ships_the_avatar_module():
    shell = (STATIC / "sw.js").read_text()
    assert "/static/avatars.js" in shell
