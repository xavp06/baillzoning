"""Non-régression hors ligne : rejoue un journal de trames et enregistre ce que le service produit.

Pour chaque requête de l'UC vers l'émulateur, la réponse est enregistrée (empreinte).
Pour l'image décodée, l'état publié (full_state) est enregistré toutes les N trames.

Usage :
  python3 regression_check.py <frames.jsonl> --record reference.json   (avant modification)
  python3 regression_check.py <frames.jsonl> --compare reference.json  (après modification)
"""

import argparse
import hashlib
import json
from pathlib import Path

from bus_model import full_state
from bus_state import BusState
from emulator import GatewayEmulator
from modbus_rtu import Frame

STEP = 200
THERMOSTATS = [1, 2, 3, 4, 5]


def run(frames_file: Path) -> dict:
    state = BusState()
    emu = GatewayEmulator()
    replies = []
    snapshots = []
    count = 0
    for line in frames_file.open(encoding="utf-8"):
        f = json.loads(line)
        frame = Frame(f["ts"], f["unit"], f["fc"], f["kind"], f["crc_ok"], bytes.fromhex(f["raw"]),
                      f["addr"], f["count"], f["values"])
        state.feed(frame)
        if frame.unit == 10 and frame.crc_ok and frame.kind == "req":
            reply = emu.handle(frame.raw)
            replies.append(None if reply is None else hashlib.sha256(reply).hexdigest())
        count += 1
        if count % STEP == 0:
            snapshots.append(full_state(state.image, THERMOSTATS))
    snapshots.append(full_state(state.image, THERMOSTATS))
    return {
        "frames": count,
        "replies": replies,
        "snapshots": snapshots,
        "emulator_holding": {str(k): v for k, v in sorted(emu.holding.items())},
        "emulator_input": {str(k): v for k, v in sorted(emu.input.items())},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("frames", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", type=Path)
    group.add_argument("--compare", type=Path)
    args = parser.parse_args()

    result = run(args.frames)
    if args.record:
        args.record.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        print(f"référence enregistrée : {result['frames']} trames, {len(result['replies'])} requêtes, "
              f"{len(result['snapshots'])} états")
        return 0
    reference = json.loads(args.compare.read_text(encoding="utf-8"))
    if result == reference:
        print(f"identique : {result['frames']} trames, {len(result['replies'])} requêtes, "
              f"{len(result['snapshots'])} états")
        return 0
    for key in result:
        if result[key] != reference.get(key):
            print(f"différence sur « {key} »")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
