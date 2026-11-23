"""Table des codes d'erreur de la passerelle Intesis DK-RC-MBS-1 (registre 11).

La table n'est pas incluse dans ce dépôt : elle reproduit le manuel du fabricant, dont le droit
d'auteur interdit la reproduction. Pour afficher les libellés, fournir un fichier JSON local,
{"code": ["nom", "description"]}, et définir INTESIS_ERRORS_FILE. Sans ce fichier, seul le numéro
du code est publié.
"""

import json
import os
from pathlib import Path

COMM_ERROR = 65535  # erreur de communication entre la DK-RC-MBS-1 et l'unité intérieure


def _load() -> dict:
    path = os.environ.get("INTESIS_ERRORS_FILE")
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {int(code): (name, text) for code, (name, text) in data.items()}


INTESIS_ERRORS = _load()
