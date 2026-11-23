"""Transforme l'image des registres du bus en valeurs lisibles (lecture seule).

Les adresses, échelles et codes viennent du profil (bus_profile.py, profiles/*.json).
Ce module ne fait que les décoder.
"""

import json

from bus_profile import current
from intesis_errors import COMM_ERROR, INTESIS_ERRORS

P = current()
UC = P.units["uc"]
INTESIS = P.units["intesis"]
HA_PRESET = {"confort": "comfort", "eco": "eco"}  # nom bus -> nom attendu par HA


def thermostat_base(number: int) -> int:
    return P.thermostat_base(number)


def _hold(image, unit, addr):
    return image.get((unit, "hold", addr))


def thermostat_key(number: int) -> str:
    return f"th{number}"


def _decode(raw, spec):
    """Valeur publiée pour un registre brut, selon sa description dans le profil."""
    if raw is None or raw == spec.get("unknown_value"):
        return None
    kind = spec["type"]
    if kind == "bool":
        return "ON" if raw == spec.get("on_value", 1) else "OFF"
    if kind == "marker":
        return "ON" if raw == P.marker_value() else "OFF"
    if kind == "enum":
        return spec["enum"].get(str(raw), "inconnu")
    if spec.get("scale"):
        return round(raw / spec["scale"], 1)
    return raw


def thermostat_values(image, number: int) -> dict:
    b = P.thermostat_base(number)
    values = {}
    for name, spec in P.read.items():
        raw = _hold(image, UC, b + spec["offset"])
        value = _decode(raw, spec)
        if value is not None:
            values[name] = value
        if "low_below" in spec and raw is not None:
            values["pile_faible"] = "ON" if raw / spec["scale"] < spec["low_below"] else "OFF"
    return values


def uc_values(image) -> dict:
    values = {}
    for name, spec in P.uc.items():
        value = _decode(_hold(image, UC, spec["offset"]), spec)
        if value is not None:
            values[spec.get("publish_as", name)] = value
    return values


def intesis_values(image) -> dict:
    values = {}
    for name, spec in P.intesis.items():
        if name == "error_code":
            continue
        value = _decode(_hold(image, INTESIS, spec["offset"]), spec)
        if value is not None:
            values[spec.get("publish_as", name)] = value
    code = _hold(image, INTESIS, P.intesis["error_code"]["offset"])
    if code is not None:
        values["erreur_code"] = code
        if code == COMM_ERROR:
            values["erreur_texte"] = "Erreur de communication DK-RC-MBS-1 / unité intérieure"
        else:
            name, text = INTESIS_ERRORS.get(code, (f"code {code}", "libellé non disponible"))
            values["erreur_texte"] = f"{name} : {text}" if code else "Aucune erreur"
    return values


def climate_values(values: dict) -> dict:
    """Valeurs dérivées pour l'entité climate HA (mode, consignes visibles, préréglage)."""
    derived = {}
    if "marche" in values:
        derived["mode"] = "auto" if values["marche"] == "ON" else "off"
    preset = values.get("preset")
    if preset in HA_PRESET:
        derived["preset"] = HA_PRESET[preset]
        if "consigne_chaud_confort" in values:
            derived["consigne_basse"] = values["consigne_chaud_confort" if preset == "confort" else "consigne_chaud_eco"]
        if "consigne_froid_confort" in values:
            derived["consigne_haute"] = values["consigne_froid_confort" if preset == "confort" else "consigne_froid_eco"]
    return derived


def thermostat_attributes(values: dict) -> dict:
    """Détails repris dans les attributs du climate (pile, zone, servomoteur, etc.)."""
    keep = ("tension_pile", "pile_faible", "zone", "servo_etat", "consigne_effective", "connecte")
    return {k: values[k] for k in keep if k in values}


def full_state(image, thermostats) -> dict:
    """Retourne {chemin: valeur} pour toutes les entités publiées."""
    state = {}
    for n in thermostats:
        values = thermostat_values(image, n)
        key = thermostat_key(n)
        for name, value in {**values, **climate_values(values)}.items():
            state[f"thermostat/{key}/{name}"] = value
        state[f"thermostat/{key}/attributes"] = json.dumps(thermostat_attributes(values), sort_keys=True)
    for key, value in uc_values(image).items():
        state[f"uc/{key}"] = value
    for key, value in intesis_values(image).items():
        state[f"intesis/{key}"] = value
    return state
