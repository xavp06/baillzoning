"""Découverte MQTT pour Home Assistant (lecture seule).

Structure des topics, calquée sur EMS-ESP :
  <prefix>/status                      online / offline
  <prefix>/thermostat/<thN>/<champ>   une valeur par topic
  <prefix>/thermostat/<th>/attributes      JSON : pile, zone, servomoteur, etc.
  <prefix>/uc/<champ>, <prefix>/intesis/<champ>

L'entité climate suit le modèle de BailClim : modes off/auto, consigne basse (chaud),
consigne haute (froid), préréglage confort/éco. Les commandes ne sont pas encore branchées.
"""

import json

MANUFACTURER = "BaillZoning (lecture bus)"
TEMP = {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"}


def _device(identifier: str, name: str, model: str) -> dict:
    return {"identifiers": [identifier], "name": name, "manufacturer": MANUFACTURER, "model": model}


def _sensor(path, name, device, extra=None):
    return {"path": path, "component": "sensor", "name": name, "device": device, "extra": extra or {}}


def _binary(path, name, device, device_class=None):
    extra = {"device_class": device_class} if device_class else {}
    return {"path": path, "component": "binary_sensor", "name": name, "device": device, "extra": extra}


def build_specs(names: dict[str, str]) -> list[dict]:
    specs = []
    for key, label in names.items():
        dev = _device(f"baillzoning_{key}", f"Thermostat {label}", "THREG (radio)")
        p = f"thermostat/{key}"
        specs += [
            {
                "path": p,
                "component": "climate",
                "name": f"Climatiseur {label}",
                "device": dev,
                "extra": {
                    "modes": ["off", "auto"],
                    "mode_state_topic": "{prefix}/" + f"{p}/mode",
                    "current_temperature_topic": "{prefix}/" + f"{p}/temperature",
                    "temperature_low_state_topic": "{prefix}/" + f"{p}/consigne_basse",
                    "temperature_high_state_topic": "{prefix}/" + f"{p}/consigne_haute",
                    "preset_modes": ["comfort", "eco"],
                    "preset_mode_state_topic": "{prefix}/" + f"{p}/preset",
                    "preset_mode_command_topic": "{prefix}/" + f"{p}/preset/set",
                    "mode_command_topic": "{prefix}/" + f"{p}/mode/set",
                    "temperature_low_command_topic": "{prefix}/" + f"{p}/consigne_basse/set",
                    "temperature_high_command_topic": "{prefix}/" + f"{p}/consigne_haute/set",
                    "json_attributes_topic": "{prefix}/" + f"{p}/attributes",
                    "min_temp": 16,
                    "max_temp": 30,
                    "temp_step": 0.5,
                    "precision": 0.1,
                    "temperature_unit": "C",
                    "optimistic": True,
                },
            },
            _sensor(f"{p}/temperature", "Température", dev, TEMP),
            _sensor(f"{p}/consigne_chaud_confort", "Consigne chaud confort", dev, TEMP),
            _sensor(f"{p}/consigne_chaud_eco", "Consigne chaud éco", dev, TEMP),
            _sensor(f"{p}/consigne_froid_confort", "Consigne froid confort", dev, TEMP),
            _sensor(f"{p}/consigne_froid_eco", "Consigne froid éco", dev, TEMP),
            _sensor(f"{p}/consigne_effective", "Consigne effective", dev, TEMP),
            _sensor(f"{p}/zone", "Zone de programmation", dev),
            _sensor(f"{p}/servo_etat", "État servomoteur", dev),
            _sensor(f"{p}/tension_pile", "Tension pile", dev,
                    {"unit_of_measurement": "V", "device_class": "voltage", "state_class": "measurement"}),
            _binary(f"{p}/pile_faible", "Pile faible (estimée)", dev, "battery"),
            _binary(f"{p}/connecte", "Connecté", dev, "connectivity"),
            _binary(f"{p}/donnees_a_jour", "Données à jour", dev),
            _sensor(f"{p}/commande", "Statut commande", dev),
        ]
    uc = _device("baillzoning_uc", "Régulation BaillZoning", "UC BaillZoning")
    specs += [
        _sensor("uc/firmware_uc", "Firmware UC", uc),
        _binary("uc/marche_regulation", "Marche régulation", uc),
        _sensor("uc/commande", "Statut commande", uc),
        {
            "path": "uc/mode",
            "component": "select",
            "name": "Mode climatisation",
            "device": uc,
            "extra": {
                "options": ["arret", "froid", "chauffage", "deshumidification"],
                "command_topic": "{prefix}/uc/mode/set",
                "icon": "mdi:air-conditioner",
                "optimistic": True,
            },
        },
    ]
    intesis = _device("baillzoning_intesis", "Unité intérieure Daikin", "Intesis DK-RC-MBS-1")
    specs += [
        _binary("intesis/marche", "Marche", intesis),
        _sensor("intesis/mode", "Mode", intesis),
        _sensor("intesis/ventilation", "Vitesse ventilation", intesis),
        _sensor("intesis/heures_fonctionnement", "Heures de fonctionnement", intesis,
                {"unit_of_measurement": "h", "state_class": "total_increasing"}),
        _binary("intesis/alarme", "Alarme", intesis, "problem"),
        _sensor("intesis/erreur_code", "Code erreur", intesis),
        _sensor("intesis/erreur_texte", "Erreur", intesis),
        _sensor("intesis/consigne_reelle", "Consigne réelle", intesis, TEMP),
        _sensor("intesis/temp_reprise", "Température de reprise", intesis, TEMP),
        _sensor("intesis/temp_exterieure", "Température extérieure", intesis, TEMP),
    ]
    return specs


def discovery_messages(prefix: str, names: dict[str, str], climate_range=None):
    """Retourne une liste (topic, payload JSON), à publier en retained.

    climate_range : (min, max) appliqué aux entités climate, selon le mode général.
    """
    messages = []
    for spec in build_specs(names):
        uid = "bz_" + spec["path"].replace("/", "_")
        extra = {k: (v.replace("{prefix}", prefix) if isinstance(v, str) else v)
                 for k, v in spec["extra"].items()}
        if spec["component"] == "climate" and climate_range:
            extra["min_temp"], extra["max_temp"] = climate_range
        config = {
            "name": spec["name"],
            "unique_id": uid,
            "availability_topic": f"{prefix}/status",
            "device": spec["device"],
            **extra,
        }
        if spec["component"] != "climate":
            config["state_topic"] = f"{prefix}/{spec['path']}"
        else:
            config["unique_id"] = f"bz_{spec['path'].replace('/', '_')}"
        topic = f"homeassistant/{spec['component']}/baillzoning/{uid}/config"
        messages.append((topic, json.dumps(config, ensure_ascii=False)))
    return messages


def climate_messages(prefix: str, names: dict[str, str], climate_range):
    """Seules les découvertes climate, avec la plage du mode courant."""
    return [(t, p) for t, p in discovery_messages(prefix, names, climate_range) if "/climate/" in t]
