"""Mode diagnostic : lecture passive du bus et comparaison au profil.

Ce mode ne répond jamais à l'UC, n'envoie aucun octet sur le bus, ne publie rien sur MQTT et
n'enregistre ni l'état de l'émulateur ni les commandes. Il produit un rapport (JSON et texte)
qui indique si le bus observé correspond au profil chargé.
"""

import json
import os
import socket
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from bus_model import _hold, intesis_values, thermostat_values, uc_values
from bus_profile import current
from bus_state import BusState
from intesis_errors import COMM_ERROR, INTESIS_ERRORS
from rtu_stream import RtuStreamDecoder

OK = "ok"
ECART = "ecart"
ABSENT = "absent"
NON_OBSERVE = "non_observe"
HORS_PROFIL = "hors_profil"
A_VERIFIER = "a_verifier"

PLAUSIBLE = {  # bornes plausibles des valeurs décodées
    "temperature": (-10.0, 50.0),
    "consigne_chaud_confort": (5.0, 35.0),
    "consigne_chaud_eco": (5.0, 35.0),
    "consigne_froid_confort": (5.0, 35.0),
    "consigne_froid_eco": (5.0, 35.0),
    "consigne_effective": (5.0, 35.0),
    "tension_pile": (1.5, 3.6),
}
ZONES = {0, 1, 2}


def _add(checks, controle, attendu, observe, statut):
    checks.append({"controle": controle, "attendu": attendu, "observe": observe, "statut": statut})


def _check_thermostat(checks, P, image, n):
    uc = P.units["uc"]
    base = P.thermostat_base(n)
    raw_marker = _hold(image, uc, base + P.thermo["block"]["marker"])
    validated = n <= P.thermo["validated_count"]
    controle = f"thermostat {n} : bloc à l'adresse {base}"
    if raw_marker is None:
        if validated:
            _add(checks, controle, "présent (validé par le profil)", "non observé", ABSENT)
        return
    if not validated:
        _add(checks, controle, "hors du profil", f"marqueur {raw_marker}", HORS_PROFIL)
    elif raw_marker != P.marker_value():
        _add(checks, controle, f"marqueur {P.marker_value()}", f"marqueur {raw_marker}", A_VERIFIER)
        return
    else:
        _add(checks, controle, f"marqueur {P.marker_value()}", "marqueur valide", OK)

    values = thermostat_values(image, n)
    problemes = []
    absents = []
    for name, spec in P.read.items():
        raw = _hold(image, uc, base + spec["offset"])
        if raw is None:
            absents.append(name)
            continue
        if spec["type"] == "enum" and str(raw) not in spec["enum"]:
            problemes.append(f"{name} = {raw} (inconnu)")
        if name == "zone" and raw not in ZONES:
            problemes.append(f"zone = {raw}")
        if name in PLAUSIBLE and name in values:
            low, high = PLAUSIBLE[name]
            if not low <= values[name] <= high:
                problemes.append(f"{name} = {values[name]} (hors {low}–{high})")
    if absents:
        _add(checks, f"thermostat {n} : champs lus", "tous présents", f"absents : {', '.join(absents)}", NON_OBSERVE)
    if problemes:
        _add(checks, f"thermostat {n} : valeurs", "plausibles", "; ".join(problemes), ECART)
    elif not absents:
        _add(checks, f"thermostat {n} : valeurs", "plausibles", "toutes plausibles", OK)


def analyse(image: dict, stats: dict, P=None) -> dict:
    """Compare l'image observée et les statistiques de trafic au profil. Ne lit rien d'autre."""
    P = P or current()
    checks = []
    uc = P.units["uc"]

    fw = _hold(image, uc, P.uc["firmware"]["offset"])
    attendu_fw = P.data["firmware"]["uc"]
    if fw is None:
        _add(checks, "firmware UC", attendu_fw, "non observé", NON_OBSERVE)
    else:
        _add(checks, "firmware UC", attendu_fw, fw, OK if fw == attendu_fw else ECART)

    reqs = stats["req_units"]
    for label, unit in (("passerelle 10 (écritures de l'UC)", P.units["emulator"]),
                        ("Intesis", P.units["intesis"])):
        _add(checks, f"requêtes de l'UC vers {label}", f"unité {unit}", reqs.get(unit, 0),
             OK if reqs.get(unit, 0) else NON_OBSERVE)

    if stats["writes_gw"]:
        _add(checks, "écritures de l'UC vers la passerelle 10", "présentes", stats["writes_gw"], OK)
    else:
        _add(checks, "écritures de l'UC vers la passerelle 10", "présentes", 0, A_VERIFIER)
    _add(checks, "lectures de la zone de commande (FC4) par l'UC", "présentes", stats["fc4_gw"],
         OK if stats["fc4_gw"] else A_VERIFIER)

    mode_raw = _hold(image, uc, P.mode["uc_return"])
    if mode_raw is None:
        _add(checks, "mode général (retour UC)", "code connu", "non observé", NON_OBSERVE)
    else:
        known = str(mode_raw) in {str(c) for c in P.mode_codes().values()}
        _add(checks, "mode général (retour UC)", "code connu", mode_raw, OK if known else ECART)

    for n in range(1, P.thermo["max_count"] + 1):
        _check_thermostat(checks, P, image, n)

    intesis = P.units["intesis"]
    code = _hold(image, intesis, P.intesis["error_code"]["offset"])
    if code is None:
        _add(checks, "Intesis : code d'erreur", "lisible", "non observé", NON_OBSERVE)
    elif code in (0, COMM_ERROR) or code in INTESIS_ERRORS:
        _add(checks, "Intesis : code d'erreur", "connu", code, OK)
    else:
        _add(checks, "Intesis : code d'erreur", "connu", code, A_VERIFIER)

    frames = stats["frames"]
    bad = stats["bad_crc"]
    if frames:
        _add(checks, "trames au CRC invalide", "< 1 %", f"{bad} sur {frames}", OK if bad * 100 < frames else A_VERIFIER)

    summary = Counter(c["statut"] for c in checks)
    return {"profil": P.name, "controles": checks, "resume": dict(summary)}


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def format_report(report: dict, stats: dict) -> str:
    lines = [f"Rapport diagnostic — profil {report['profil']}",
             f"Durée : {stats['duration_s']:.0f} s, {stats['frames']} trames, {stats['bad_crc']} au CRC invalide", ""]
    for c in report["controles"]:
        lines.append(f"[{c['statut']:>11}] {c['controle']} : attendu {c['attendu']}, observé {c['observe']}")
    lines.append("")
    lines.append("Résumé : " + ", ".join(f"{k} {v}" for k, v in sorted(report["resume"].items())))
    return "\n".join(lines)


def run(host: str, port: int, duration: int, output: Path) -> int:
    P = current()
    state = BusState()
    stats = {"frames": 0, "bad_crc": 0, "req_units": Counter(), "writes_gw": 0, "fc4_gw": 0, "duration_s": 0.0}
    print(f"{datetime.now():%H:%M:%S} diagnostic : {host}:{port} pendant {duration} s, lecture seule", flush=True)
    started = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=10)
    except OSError as err:
        raise SystemExit(f"diagnostic : connexion à {host}:{port} impossible ({err})")
    sock.settimeout(5)
    decoder = RtuStreamDecoder()
    try:
        while time.time() - started < duration:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            for frame in decoder.feed(data, time.time()):
                state.feed(frame)
                stats["frames"] += 1
                if not frame.crc_ok:
                    stats["bad_crc"] += 1
                if frame.kind == "req":
                    stats["req_units"][frame.unit] += 1
                    if frame.unit == P.units["emulator"] and frame.fc == 16:
                        stats["writes_gw"] += 1
                    if frame.unit == P.units["emulator"] and frame.fc == 4:
                        stats["fc4_gw"] += 1
    finally:
        sock.close()
    stats["duration_s"] = time.time() - started
    report = analyse(state.image, stats, P)
    report["duree_s"] = round(stats["duration_s"], 1)
    report["trames"] = stats["frames"]
    report["date"] = datetime.now().isoformat(timespec="seconds")
    write_report(report, output)
    print(format_report(report, stats), flush=True)
    print(f"rapport enregistré : {output}", flush=True)
    return 0


def default_output() -> Path:
    return Path(os.environ.get("DIAGNOSTIC_OUTPUT", Path(__file__).parent / "data" / "diagnostic.json"))
