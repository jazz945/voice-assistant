"""Sauvegarde de la base MotoMatch, avec rotation.

Usage :
    python backup.py              une sauvegarde, puis sortie
    python backup.py --boucle     une sauvegarde toutes les BACKUP_INTERVAL_SECONDS

Copier le fichier `.db` à chaud est faux : en mode WAL, les écritures récentes
vivent dans `-wal` et une copie brute peut attraper un état incohérent. On passe
donc par l'API `backup()` de SQLite, qui produit un instantané cohérent sans
interrompre les écritures en cours.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def backup_once(source: Path, directory: Path, keep: int) -> Path:
    if not source.exists():
        raise FileNotFoundError(f"base introuvable : {source}")

    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"motomatch-{stamp}.db"

    # L'horodatage est à la seconde : deux sauvegardes lancées coup sur coup
    # tomberaient sur le même nom et la seconde écraserait la première, sans
    # rien dire. On suffixe plutôt que d'écraser — une sauvegarde perdue en
    # silence ne se découvre qu'au moment de la restauration.
    suffix = 1
    while target.exists():
        target = directory / f"motomatch-{stamp}-{suffix}.db"
        suffix += 1

    # Ouverture en lecture seule : la sauvegarde ne peut pas altérer la source.
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    target.chmod(0o600)  # la base contient des données personnelles
    _rotate(directory, keep, keep_always=target)
    return target


def _rotate(directory: Path, keep: int, keep_always: Path | None = None) -> int:
    """Ne conserve que les `keep` sauvegardes les plus récentes.

    Le tri se fait sur la date de modification, pas sur le nom : un suffixe de
    collision (`…Z-1.db`) se classe alphabétiquement *avant* le nom sans suffixe
    alors qu'il est plus récent, et un tri par nom supprimerait donc les
    sauvegardes les plus fraîches en premier.

    `keep_always` protège la sauvegarde qui vient d'être écrite — la supprimer
    dans la foulée n'aurait aucun sens.
    """
    backups = sorted(
        directory.glob("motomatch-*.db"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    removed = 0
    for stale in backups[keep:]:
        if keep_always is not None and stale == keep_always:
            continue
        stale.unlink()
        removed += 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boucle", action="store_true", help="sauvegarder en continu")
    args = parser.parse_args()

    source = Path(_env("MOTOMATCH_DB", "/data/motomatch.db"))
    directory = Path(_env("BACKUP_DIR", "/sauvegardes"))
    keep = int(_env("BACKUP_KEEP", "14"))
    interval = int(_env("BACKUP_INTERVAL_SECONDS", "86400"))

    while True:
        try:
            target = backup_once(source, directory, keep)
            size = target.stat().st_size / 1024
            print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] "
                  f"sauvegarde {target.name} ({size:.0f} Kio), {keep} conservées", flush=True)
        except Exception as error:  # une sauvegarde ratée ne doit pas tuer la boucle
            print(f"échec de la sauvegarde : {error}", file=sys.stderr, flush=True)
            if not args.boucle:
                return 1
        if not args.boucle:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
