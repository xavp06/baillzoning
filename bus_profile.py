"""Chargement et validation du profil de carte du bus (adresses, codes, unités).

Un profil est un fichier JSON dans profiles/, décrit par profiles/schema.json.
Il est choisi par la variable PROFILE : un nom (baillzoning-uc82) ou un chemin vers un fichier .json.
Un profil invalide empêche le démarrage : mieux vaut un arrêt qu'une écriture au mauvais endroit.
"""

import json
import os
from pathlib import Path

import jsonschema

PROFILES_DIR = Path(__file__).parent / "profiles"
SCHEMA_FILE = PROFILES_DIR / "schema.json"
DEFAULT_PROFILE = "baillzoning-uc82"


class ProfileError(RuntimeError):
    pass


def _path_for(spec: str) -> Path:
    if spec.endswith(".json") or "/" in spec:
        return Path(spec)
    return PROFILES_DIR / f"{spec}.json"


def _check_semantics(data: dict) -> None:
    thermo = data["thermostats"]
    if thermo["validated_count"] > thermo["max_count"]:
        raise ProfileError("validated_count dépasse max_count")

    read = thermo["read"]
    command = thermo["command"]
    bits = [f["bit"] for f in command["fields"].values()]
    if len(bits) != len(set(bits)):
        raise ProfileError("deux champs de commande partagent le même bit de masque")
    for name, f in command["fields"].items():
        if name not in read:
            raise ProfileError(f"champ de commande « {name} » absent de la lecture")
        if read[name]["offset"] != f["uc_offset"]:
            raise ProfileError(f"« {name} » : uc_offset {f['uc_offset']} ≠ offset de lecture {read[name]['offset']}")

    codes = data["mode"]["codes"]
    if len(set(codes.values())) != len(codes):
        raise ProfileError("deux modes partagent le même code")
    if data["mode"]["uc_return"] != data["uc"]["mode_return"]["offset"]:
        raise ProfileError("mode.uc_return et uc.mode_return.offset doivent être identiques")
    if set(data["uc"]["mode_return"]["enum"]) != {str(c) for c in codes.values()}:
        raise ProfileError("le retour UC du mode ne correspond pas aux codes de mode")

    presets = thermo["presets"]
    if set(presets["values"]) != {str(c) for c in presets["commands"].values()}:
        raise ProfileError("les codes de préréglage lus et commandés ne correspondent pas")
    if set(read["preset"]["enum"]) != set(presets["values"]):
        raise ProfileError("le champ preset lu ne correspond pas à presets.values")


def load_profile(spec: str | None = None) -> dict:
    spec = spec or os.environ.get("PROFILE") or DEFAULT_PROFILE
    path = _path_for(spec)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"profil illisible : {path} ({exc})") from exc

    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        where = "/".join(str(p) for p in errors[0].absolute_path) or "racine"
        raise ProfileError(f"profil invalide ({path.name}) à {where} : {errors[0].message}")

    _check_semantics(data)
    return data


class Profile:
    """Accès typé au profil."""

    def __init__(self, data: dict):
        self.data = data
        self.name = data["name"]
        self.units = data["units"]
        self.uc = data["uc"]
        self.intesis = data["intesis"]
        self.thermo = data["thermostats"]
        self.read = self.thermo["read"]
        self.command = self.thermo["command"]
        self.mode = data["mode"]
        self.timing = data["timing"]
        self.limits = data["limits"]

    @classmethod
    def load(cls, spec: str | None = None) -> "Profile":
        return cls(load_profile(spec))

    def thermostat_base(self, n: int) -> int:
        b = self.thermo["block"]
        return b["base"] + b["stride"] * (n - 1)

    def command_base(self, n: int) -> int:
        return self.command["base"] + self.command["stride"] * (n - 1)

    def command_fields(self) -> dict:
        """Champ -> (bit du masque, offset de la valeur, offset du retour UC)."""
        return {name: (f["bit"], f["offset"], f["uc_offset"]) for name, f in self.command["fields"].items()}

    def command_scale(self, name: str) -> float:
        return self.command["fields"][name].get("scale", 1)

    def marker_value(self) -> int:
        return self.thermo["block"]["marker_value"]

    def mode_codes(self) -> dict:
        return dict(self.mode["codes"])

    def mode_names(self) -> dict:
        return {code: name for name, code in self.mode["codes"].items()}

    def preset_names(self) -> dict:
        """Code lu dans le bloc -> nom bus (ex. 1 -> « confort »)."""
        return {int(k): v for k, v in self.read["preset"]["enum"].items()}

    def preset_commands(self) -> dict:
        """Nom demandé depuis HA -> code envoyé (ex. « comfort » -> 1)."""
        return dict(self.thermo["presets"]["commands"])

    def payload_modes(self) -> dict:
        return dict(self.thermo["payloads"]["mode"])

    def check_verified(self, numbers) -> None:
        """Refuse un thermostat autorisé aux commandes dont le bloc n'est pas validé par le profil."""
        if self.command["confidence"] != "validated":
            if numbers:
                raise ProfileError("profil : zone de commande non validée, aucun thermostat ne peut être commandé")
            return
        for n in numbers:
            if n > self.thermo["validated_count"]:
                raise ProfileError(f"thermostat {n} autorisé aux commandes, mais hors des blocs validés par le profil ({self.thermo['validated_count']})")


_current: Profile | None = None


def current() -> Profile:
    """Profil du processus, chargé à la première utilisation depuis la variable PROFILE."""
    global _current
    if _current is None:
        _current = Profile.load()
    return _current


def use(profile: Profile) -> None:
    """Remplace le profil du processus (tests)."""
    global _current
    _current = profile
