"""Génère les icônes PNG de l'application.

Encodeur PNG minimal en pur Python : évite d'ajouter Pillow comme dépendance
juste pour produire cinq fichiers qui ne changent jamais. Les icônes sont
versionnées dans `static/icons/` ; ce script sert à les régénérer.

Usage : `python -m motomatch.tools.make_icons`

Le motif est une route qui file vers l'horizon, ligne médiane discontinue —
lisible à 48 px comme à 512. Le fond orange va jusqu'aux bords : l'icône reste
correcte une fois rognée en cercle par Android (`purpose: maskable`).
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ICON_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"

# Palette reprise de styles.css.
ORANGE_LIGHT = (255, 138, 61)
ORANGE_DARK = (217, 107, 34)
ASPHALT = (26, 18, 6)
LINE = (255, 245, 235)

# Fraction du côté occupée par le dessin : garde une marge pour le rognage
# circulaire d'Android, qui peut retirer jusqu'à 10 % sur chaque bord.
SAFE_ZONE = 0.62


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _road_bounds(y: float) -> tuple[float, float]:
    """Demi-largeur de la route à une hauteur donnée (0 en haut, 1 en bas).

    La route part d'un point de fuite étroit en haut pour s'élargir vers le bas.
    """
    return 0.035 + 0.30 * (y**1.7), y


def render(size: int) -> bytes:
    """Construit l'image RGB, ligne par ligne."""
    rows: list[bytes] = []
    centre = 0.5
    # La route est centrée verticalement et ne touche aucun bord : Android
    # rogne l'icône en cercle, un motif à fleur de bord serait tronqué.
    top = (1 - SAFE_ZONE) / 2
    bottom = top + SAFE_ZONE

    for py in range(size):
        row = bytearray()
        v = py / (size - 1)
        for px in range(size):
            u = px / (size - 1)

            # Fond : dégradé diagonal orange.
            colour = _lerp(ORANGE_LIGHT, ORANGE_DARK, (u + v) / 2)

            if top <= v <= bottom:
                # Progression le long de la route, 0 à l'horizon.
                t = (v - top) / (bottom - top)
                half_width, _ = _road_bounds(t)
                if abs(u - centre) <= half_width:
                    colour = ASPHALT
                    # Ligne médiane discontinue, dont les tirets s'allongent
                    # en se rapprochant — ce qui donne la perspective.
                    dash_width = max(0.006, half_width * 0.11)
                    phase = math.sqrt(max(0.0, t)) * 5.2
                    if abs(u - centre) <= dash_width and phase % 1.0 < 0.55:
                        colour = LINE
            row.extend(colour)
        rows.append(bytes(row))

    return _encode_png(size, size, rows)


def _encode_png(width: int, height: int, rows: list[bytes]) -> bytes:
    """Assemble un PNG RGB 8 bits, filtre 0 sur chaque ligne."""
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)  # 8 bits, truecolour
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


SIZES = {
    "icon-192.png": 192,   # Android, taille minimale exigée pour l'installation
    "icon-512.png": 512,   # Android, écran de démarrage
    "icon-maskable-512.png": 512,
    "apple-touch-icon.png": 180,  # iOS, écran d'accueil
    "favicon-64.png": 64,
}

# Icône de lanceur de l'application Android, une taille par densité d'écran.
ANDROID_MIPMAPS = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}

ANDROID_RES = Path(__file__).resolve().parent.parent.parent / "android" / "app" / "src" / "main" / "res"


def main() -> None:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        path = ICON_DIR / name
        path.write_bytes(render(size))
        print(f"{path.relative_to(ICON_DIR.parent.parent)} — {size}×{size}, {path.stat().st_size} o")

    if ANDROID_RES.parent.exists():
        for density, size in ANDROID_MIPMAPS.items():
            folder = ANDROID_RES / f"mipmap-{density}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "ic_launcher.png").write_bytes(render(size))
        print(f"icônes de lanceur Android — {len(ANDROID_MIPMAPS)} densités")


if __name__ == "__main__":
    main()
