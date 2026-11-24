"""Service de lecture passive du bus BaillZoning : TCP (Waveshare) -> image des registres -> MQTT.

Lecture seule : ce service n'envoie jamais d'octet vers la Waveshare, donc rien n'est émis sur le
bus RS-485. Les commandes et l'émulation de la passerelle 10 viendront dans une étape séparée.

Configuration (variables d'environnement) :
  WAVESHARE_HOST (obligatoire), WAVESHARE_PORT (502), MQTT_PREFIX (baillzoning)
  MQTT_HOST (localhost), MQTT_PORT (1883), MQTT_USER, MQTT_PASSWORD (optionnels)
  THERMOSTAT_COUNT (5), THERMOSTAT_NAMES, VERIFIED_THERMOSTATS (voir commands.py)

Usage :
  service.py --dry-run [--duration 60]   affiche les messages, sans MQTT
  service.py                              publie sur MQTT
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime

from pathlib import Path

from bus_model import full_state, thermostat_key
from bus_profile import ProfileError, current
from bus_state import BusState, Journal
from commands import VERIFIED, CommandManager, block_base, climate_range_for_mode
from emulator import GatewayEmulator
from ha_discovery import climate_messages, discovery_messages
from modbus_rtu import Frame, parse_rtu
from rtu_stream import RtuStreamDecoder

def _thermostat_config():
    """Nombre de thermostats et noms, depuis l'environnement.

    THERMOSTAT_COUNT : nombre de thermostats (défaut 5).
    THERMOSTAT_NAMES : « 1:Salon,2:Chambre,... » ; un thermostat absent de la liste s'appelle « Thermostat N ».
    """
    count = int(os.environ.get("THERMOSTAT_COUNT", "5"))
    names = {}
    for item in os.environ.get("THERMOSTAT_NAMES", "").split(","):
        if ":" in item:
            num, name = item.split(":", 1)
            if num.strip().isdigit() and name.strip():
                names[int(num)] = name.strip()
    numbers = list(range(1, count + 1))
    return numbers, {f"th{n}": names.get(n, f"Thermostat {n}") for n in numbers}


THERMOSTAT_NUMBERS, THERMOSTAT_NAMES = _thermostat_config()
PROFILE = current()
PUBLISH_EVERY = PROFILE.timing["publish_every"]
HEARTBEAT_EVERY = PROFILE.timing["heartbeat_every"]
STALE_AFTER = PROFILE.timing["stale_after"]
RECONNECT_DELAY = 5
CLEANUP_WAIT = 3
UNIT10_STALE_AFTER = PROFILE.timing["uc_stale_after"]  # sans trame de l'UC vers l'esclave 10 : données thermostat non à jour
EMU_UNIT = PROFILE.units["emulator"]
UC_UNIT = PROFILE.units["uc"]
RESPOND = os.environ.get("EMU_RESPOND", "1") == "1"  # 0 = pas de réponse à l'UC (lecture seule)
STATE_FILE = Path(os.environ.get("EMU_STATE", Path(__file__).parent / "data" / "emulator_state.json"))
SEED_FILE = Path(__file__).parent / "data" / "phase4" / "frames.jsonl"
SAVE_EVERY = PROFILE.timing["save_every"]
PRESET_BY_CODE = {code: name for name, code in PROFILE.preset_commands().items()}
COMMAND_STATUS: dict[int, str] = {}  # 0 = régulation (mode), 1..N = thermostats
REPUBLISH = threading.Event()
REQUESTED_PRESET: dict[int, str] = {}  # préréglage demandé, en attente de confirmation de l'UC
STATUS_BY_EVENT = {
    "commande_en_file": "en_attente",
    "commande_envoyee": "en_cours",
    "commande_confirmee": "appliquee",
    "deja_applique": "appliquee",
    "commande_non_confirmee": "echec",
}


def load_emulator_state(emu: GatewayEmulator):
    """Reprend les dernières valeurs de l'esclave 10 : fichier d'état, sinon amorçage depuis un journal."""
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        emu.holding = {int(k): v for k, v in data["holding"].items()}
        emu.input = {int(k): v for k, v in data["input"].items()}
        log(f"état de l'émulateur repris : {len(emu.holding)} registres, {len(emu.input)} de commande")
    elif SEED_FILE.exists():
        state = BusState()
        for line in SEED_FILE.open(encoding="utf-8"):
            f = json.loads(line)
            state.feed(Frame(f["ts"], f["unit"], f["fc"], f["kind"], f["crc_ok"], bytes.fromhex(f["raw"]),
                             f["addr"], f["count"], f["values"]))
        emu.holding = {a: v for (u, t, a), v in state.image.items() if u == EMU_UNIT and t == "hold"}
        emu.input = {a: v for (u, t, a), v in state.image.items() if u == EMU_UNIT and t == "input"}
        log("état de l'émulateur amorcé depuis le journal de campagne")


def save_emulator_state(emu: GatewayEmulator):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"holding": emu.holding, "input": emu.input}), encoding="utf-8")


class Reader:
    """Lit le flux TCP et met à jour l'image. Ne fait que recevoir."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.state = BusState()
        self.lock = threading.Lock()
        self.last_frame = 0.0
        self.last_unit10 = 0.0
        self.frames = 0
        self.emu = GatewayEmulator()
        load_emulator_state(self.emu)
        self.emu_replies = 0
        self.emu_unanswered = 0
        self.commands = CommandManager(command_log)
        journal_dir = os.environ.get("JOURNAL_DIR")
        self.journal = Journal(Path(journal_dir)) if journal_dir else None
        self.connected = False
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=10)
            except OSError as err:
                log(f"connexion Waveshare impossible : {err}")
                self.stop.wait(RECONNECT_DELAY)
                continue
            log(f"connecté à la Waveshare {self.host}:{self.port} (émulateur {'actif' if RESPOND else 'coupé'})")
            self.connected = True
            decoder = RtuStreamDecoder()
            sock.settimeout(5)
            try:
                while not self.stop.is_set():
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    for frame in decoder.feed(data, time.time()):
                        self._apply(frame)
                        self._respond(sock, frame)
            except OSError as err:
                log(f"liaison interrompue : {err}")
            finally:
                self.connected = False
                sock.close()
            self.stop.wait(RECONNECT_DELAY)

    def _journal(self, frame: Frame, changes):
        if self.journal:
            self.journal.write_frame(frame)
            self.journal.write_changes(changes)
            self.journal.flush()

    def _apply(self, frame: Frame):
        with self.lock:
            changes = self.state.feed(frame)
            self._journal(frame, changes)
            self.last_frame = frame.ts
            if frame.unit == UC_UNIT and frame.crc_ok and frame.fc == 16 and frame.kind == "req":
                self.last_unit10 = frame.ts
            self.frames += 1

    def _respond(self, sock, frame: Frame):
        """Émulateur de la passerelle 10 : répond uniquement aux requêtes de l'UC vers l'esclave 10."""
        if not RESPOND or frame.unit != EMU_UNIT or frame.kind != "req" or not frame.crc_ok:
            return
        with self.lock:
            reply = self.emu.handle(frame.raw)
        if reply is None:
            self.emu_unanswered += 1
            return
        try:
            sock.sendall(reply)
            self.emu_replies += 1
            with self.lock:
                ack = parse_rtu(reply, frame.ts)
                self._journal(ack, self.state.feed(ack))
        except OSError as err:
            log(f"réponse non envoyée : {err}")

    def snapshot(self):
        with self.lock:
            return dict(self.state.image)

    def online(self):
        return self.connected and (time.time() - self.last_frame) < STALE_AFTER


def log(message):
    print(datetime.now().strftime("%H:%M:%S"), message, flush=True)


def command_log(event, **data):
    if data.get("field") == "preset":
        n = data.get("thermostat", 0)
        if event == "commande_en_file":
            REQUESTED_PRESET[n] = PRESET_BY_CODE.get(data.get("value"), "")
        elif event in ("commande_confirmee", "commande_non_confirmee", "deja_applique"):
            REQUESTED_PRESET.pop(n, None)
    if event in STATUS_BY_EVENT:
        COMMAND_STATUS[data.get("thermostat", 0)] = STATUS_BY_EVENT[event]
        if event == "commande_non_confirmee":
            REPUBLISH.set()
    log(f"[commande] {event} {json.dumps(data, ensure_ascii=False)}")
    with open(Path(__file__).parent / "data" / "commands.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "event": event, **data}, ensure_ascii=False) + "\n")


COMMAND_TOPIC = re.compile(r"^(?P<prefix>[^/]+)/thermostat/th(?P<n>[1-9][0-9]*)/(?P<name>mode|preset|consigne_basse|consigne_haute)/set$")


class Publisher:
    def __init__(self, prefix, dry_run, mqtt_cfg):
        self.prefix = prefix
        self.dry_run = dry_run
        self.client = None
        self.last = {}
        self.retained = {}
        self.collecting = False
        self.on_command = None
        self.on_stop = None
        self.on_resume = None
        self.climate_mode = None
        if not dry_run:
            import paho.mqtt.client as mqtt
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="baillzoning-service")
            if mqtt_cfg.get("user"):
                self.client.username_pw_set(mqtt_cfg["user"], mqtt_cfg.get("password"))
            self.client.will_set(f"{prefix}/status", "offline", retain=True)
            self.client.on_connect = self._on_connect
            self.client.on_message = self._on_message
            self.client.connect(mqtt_cfg["host"], mqtt_cfg["port"], keepalive=60)
            self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log("connecté à MQTT")
        self.collecting = True
        self.retained = {}
        client.subscribe(f"{self.prefix}/#")
        client.subscribe("homeassistant/+/+/config")
        client.subscribe(f"{self.prefix}/commande/arret")
        for topic, payload in discovery_messages(self.prefix, THERMOSTAT_NAMES):
            client.publish(topic, payload, retain=True)
        client.publish(f"{self.prefix}/status", "online", retain=True)
        self.last = {}
        self.climate_mode = None

    def _on_message(self, client, userdata, msg):
        if self.collecting and msg.retain and msg.payload:
            self.retained[msg.topic] = True
        if msg.retain:
            return  # messages retenus (anciens) : jamais exécutés
        payload = msg.payload.decode(errors="replace")
        if msg.topic == f"{self.prefix}/commande/arret":
            (self.on_stop if payload.upper() == "ON" else self.on_resume)()
            return
        if msg.topic == f"{self.prefix}/uc/mode/set" and self.on_command:
            error = self.on_command(0, "uc_mode", payload)
            if error:
                COMMAND_STATUS[0] = "refusee"
                log(f"[commande] refusée : mode = {payload} ({error})")
            return
        m = COMMAND_TOPIC.match(msg.topic)
        if m and m.group("prefix") == self.prefix and self.on_command:
            error = self.on_command(int(m.group("n")), m.group("name"), payload)
            if error:
                COMMAND_STATUS[int(m.group("n"))] = "refusee"
                log(f"[commande] refusée : {msg.topic} = {payload} ({error})")

    def cleanup_stale(self):
        """Efface les topics retenus qui ne font plus partie de la nouvelle structure."""
        time.sleep(CLEANUP_WAIT)
        self.collecting = False
        wanted = {t for t, _ in discovery_messages(self.prefix, THERMOSTAT_NAMES)}
        stale = []
        for topic in self.retained:
            if topic.startswith("homeassistant/") and topic not in wanted and "/bz_" in topic:
                stale.append(topic)
            elif topic.startswith(f"{self.prefix}/") and not topic.startswith(
                    (f"{self.prefix}/thermostat/th", f"{self.prefix}/uc/", f"{self.prefix}/intesis/",
                     f"{self.prefix}/status", f"{self.prefix}/heartbeat")):
                stale.append(topic)
            elif topic.startswith(f"{self.prefix}/thermostat/") and not topic.startswith(
                    tuple(f"{self.prefix}/thermostat/{k}/" for k in THERMOSTAT_NAMES)):
                stale.append(topic)
        for topic in stale:
            if self.dry_run:
                log(f"nettoyage (simulation) : {topic}")
            else:
                self.client.publish(topic, "", retain=True)
        log(f"{len(stale)} ancien(s) topic(s) effacé(s)")

    def publish_climate(self, names, climate_range):
        for topic, payload in climate_messages(self.prefix, names, climate_range):
            self.send(topic, payload)

    def send(self, topic, value, retain=True):
        if self.dry_run:
            print(f"  {topic} = {value}")
        else:
            self.client.publish(topic, value, retain=retain)

    def publish_changes(self, state: dict, online: bool):
        status = "online" if online else "offline"
        if self.last.get("__status__") != status:
            self.send(f"{self.prefix}/status", status)
            self.last["__status__"] = status
        for path, value in state.items():
            value = str(value)
            if self.last.get(path) != value:
                self.send(f"{self.prefix}/{path}", value)
                self.last[path] = value

    def heartbeat(self, reader: Reader, started: float):
        payload = {
            "bus_status": "connected" if reader.connected else "disconnected",
            "uptime_sec": int(time.time() - started),
            "frames": reader.frames,
            "emulateur_actif": RESPOND,
            "reponses_passerelle10": reader.emu_replies,
            "passerelle10_age_sec": round(time.time() - reader.last_unit10, 1) if reader.last_unit10 else None,
            "last_frame_age_sec": round(time.time() - reader.last_frame, 1) if reader.last_frame else None,
        }
        self.send(f"{self.prefix}/heartbeat", json.dumps(payload))


def _submit(reader: Reader, n: int, name: str, payload: str):
    with reader.lock:
        return reader.commands.submit(n, name, payload, reader.state.image, RESPOND)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="affiche sans publier sur MQTT")
    parser.add_argument("--duration", type=int, default=0, help="arrêt automatique après N secondes")
    parser.add_argument("--diagnostic", action="store_true",
                        help="lecture passive seule : compare le bus au profil, rapport JSON, rien n'est écrit")
    args = parser.parse_args()

    host = os.environ.get("WAVESHARE_HOST")
    if not host:
        raise SystemExit("WAVESHARE_HOST non défini (adresse de la passerelle RS-485/Ethernet)")
    port = int(os.environ.get("WAVESHARE_PORT", "502"))

    if args.diagnostic:
        from diagnostic import default_output, run as run_diagnostic
        return run_diagnostic(host, port, args.duration or 1800, default_output())

    try:
        PROFILE.check_verified(VERIFIED)
    except ProfileError as err:
        raise SystemExit(str(err))
    prefix = os.environ.get("MQTT_PREFIX", "baillzoning")
    mqtt_cfg = {
        "host": os.environ.get("MQTT_HOST", "localhost"),
        "port": int(os.environ.get("MQTT_PORT", "1883")),
        "user": os.environ.get("MQTT_USER"),
        "password": os.environ.get("MQTT_PASSWORD"),
    }

    started = time.time()
    reader = Reader(host, port)
    threading.Thread(target=reader.run, daemon=True).start()
    publisher = Publisher(prefix, args.dry_run, mqtt_cfg)
    if not args.dry_run:
        publisher.cleanup_stale()
    publisher.on_command = lambda n, name, payload: _submit(reader, n, name, payload)
    publisher.on_stop = reader.commands.stop
    publisher.on_resume = reader.commands.resume
    last_heartbeat = 0.0
    last_saved = 0.0

    try:
        while True:
            time.sleep(PUBLISH_EVERY)
            state = full_state(reader.snapshot(), THERMOSTAT_NUMBERS)
            if REPUBLISH.is_set():
                REPUBLISH.clear()
                publisher.last = {}  # republier l'état réel du bus, pour corriger l'affichage optimiste
            snapshot = reader.snapshot()
            for n in THERMOSTAT_NUMBERS:
                if n in REQUESTED_PRESET and REQUESTED_PRESET[n]:
                    key = thermostat_key(n)
                    comfort = REQUESTED_PRESET[n] == "comfort"
                    base = block_base(n)
                    hot_field = "consigne_chaud_confort" if comfort else "consigne_chaud_eco"
                    cool_field = "consigne_froid_confort" if comfort else "consigne_froid_eco"
                    hot = snapshot.get((UC_UNIT, "hold", base + PROFILE.read[hot_field]["offset"]))
                    cool = snapshot.get((UC_UNIT, "hold", base + PROFILE.read[cool_field]["offset"]))
                    state[f"thermostat/{key}/preset"] = REQUESTED_PRESET[n]
                    if hot is not None:
                        state[f"thermostat/{key}/consigne_basse"] = round(hot / PROFILE.read[hot_field]["scale"], 1)
                    if cool is not None:
                        state[f"thermostat/{key}/consigne_haute"] = round(cool / PROFILE.read[cool_field]["scale"], 1)
                state[f"thermostat/{thermostat_key(n)}/commande"] = COMMAND_STATUS.get(n, "inactif")
                attrs_path = f"thermostat/{thermostat_key(n)}/attributes"
                attrs = json.loads(state[attrs_path])
                attrs["commande"] = COMMAND_STATUS.get(n, "inactif")
                state[attrs_path] = json.dumps(attrs, sort_keys=True)
            state["uc/commande"] = COMMAND_STATUS.get(0, "inactif")
            fresh = (time.time() - reader.last_unit10) < UNIT10_STALE_AFTER
            for n in THERMOSTAT_NUMBERS:
                state[f"thermostat/{thermostat_key(n)}/donnees_a_jour"] = "ON" if fresh else "OFF"
            publisher.publish_changes(state, reader.online())
            mode_name = state.get("uc/mode")
            if mode_name and mode_name != publisher.climate_mode:
                publisher.publish_climate(THERMOSTAT_NAMES, climate_range_for_mode(mode_name))
                publisher.climate_mode = mode_name
                log(f"plage des climate : mode {mode_name}, {climate_range_for_mode(mode_name)}")
            with reader.lock:
                reader.commands.tick(reader.emu, reader.state.image, time.time())
            if time.time() - last_saved >= SAVE_EVERY:
                save_emulator_state(reader.emu)
                last_saved = time.time()
            if time.time() - last_heartbeat >= HEARTBEAT_EVERY:
                publisher.heartbeat(reader, started)
                last_heartbeat = time.time()
            if args.duration and time.time() - started >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop.set()
        save_emulator_state(reader.emu)
        if publisher.client:
            publisher.send(f"{prefix}/status", "offline")
            publisher.client.loop_stop()
            publisher.client.disconnect()
    log("arrêt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
