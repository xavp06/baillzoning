"""Commandes depuis MQTT/HA vers la zone de commande FC4 de l'esclave 10.

Les adresses, codes et temporisations viennent du profil (bus_profile.py). Ce module fixe les règles :
- seuls les thermostats autorisés (VERIFIED_THERMOSTATS) sont commandables ;
- consignes bornées par mode (HOT_MIN/HOT_MAX, COLD_MIN/COLD_MAX, défaut : limites du profil) ;
- une commande à la fois : la suivante part quand la précédente est confirmée par l'UC ;
- une commande n'est envoyée que si la valeur diffère de celle de l'UC ;
- file : une seule commande en attente par thermostat et par champ (la plus récente) ;
- signal remis à zéro dès la confirmation, avec un filet de sécurité (clear_after) ;
- arrêt d'urgence : vide la file et bloque toute commande.
"""

import os
from dataclasses import dataclass

from bus_profile import current

P = current()
UC = P.units["uc"]
SIGNAL = P.command["signal"]
MODE_SIGNAL = P.mode["signal"]
MODE_CODE = P.mode["code"]
MODE_UPDATE = P.mode["update_flag"]
MODE_RETURN = P.mode["uc_return"]
VERIFIED = {int(x) for x in os.environ.get("VERIFIED_THERMOSTATS", "").split(",") if x.strip().isdigit()}
BOUNDS_HOT = (float(os.environ.get("HOT_MIN", P.limits["hot"][0])), float(os.environ.get("HOT_MAX", P.limits["hot"][1])))
BOUNDS_COLD = (float(os.environ.get("COLD_MIN", P.limits["cold"][0])), float(os.environ.get("COLD_MAX", P.limits["cold"][1])))
MIN_GAP = P.timing["min_gap"]          # délai minimal entre deux commandes (la suivante attend la confirmation de la précédente)
CLEAR_AFTER = P.timing["clear_after"]  # filet de sécurité : remise à zéro du signal si l'UC n'a pas confirmé
CONFIRM_WAIT = P.timing["confirm_wait"]
MAX_QUEUE = P.timing["max_queue"]
MODE_CODES = P.mode_codes()
MODE_NAMES = P.mode_names()
PRESET_COMMANDS = P.preset_commands()          # « comfort » / « eco » -> code
PRESET_CONFORT = PRESET_COMMANDS["comfort"]
PRESET_CODES = set(P.preset_names())
MODE_PAYLOADS = P.payload_modes()              # « auto » / « off » -> valeur de marche
GLOBAL = 0  # identifiant « régulation » pour le mode général

# champ -> (bit du masque, offset de la valeur dans la commande, offset du retour UC)
FIELDS = P.command_fields()


def bounds_for_field(field: str) -> tuple[float, float]:
    return BOUNDS_COLD if field.startswith("consigne_froid") else BOUNDS_HOT


def climate_range_for_mode(mode_name: str | None) -> tuple[float, float]:
    """Plage affichée sur le climate HA selon le mode général de la régulation."""
    if mode_name == "chauffage":
        return BOUNDS_HOT
    if mode_name == "froid":
        return BOUNDS_COLD
    return (min(BOUNDS_HOT[0], BOUNDS_COLD[0]), max(BOUNDS_HOT[1], BOUNDS_COLD[1]))


def block_base(n: int) -> int:
    return P.thermostat_base(n)


def cmd_base(n: int) -> int:
    return P.command_base(n)


@dataclass
class Pending:
    n: int
    field: str
    encoded: int
    sent_at: float


class CommandManager:
    def __init__(self, log):
        self.log = log
        self.queue: list[tuple[int, str, int]] = []
        self.pending: Pending | None = None
        self.last_sent = 0.0
        self.last_n = None
        self.enabled = True

    def _current_preset(self, image, n):
        return image.get((UC, "hold", block_base(n) + P.read["preset"]["offset"]))

    def _already_applied(self, n, field, value, image) -> bool:
        if field == "uc_mode":
            return image.get((UC, "hold", MODE_RETURN)) == value
        _, _, uc_off = FIELDS[field]
        return image.get((UC, "hold", block_base(n) + uc_off)) == value

    def _enqueue(self, n, field, value, image) -> str | None:
        if self._already_applied(n, field, value, image):
            self.log("deja_applique", thermostat=n, field=field, value=value)
            return None
        if self.pending is not None and self.pending.n == n and self.pending.field == field and self.pending.encoded == value:
            return None  # déjà en cours d'envoi
        self.queue = [q for q in self.queue if (q[0], q[1]) != (n, field)]
        if len(self.queue) >= MAX_QUEUE:
            return "file pleine"
        self.queue.append((n, field, value))
        self.log("commande_en_file", thermostat=n, field=field, value=value)
        return None

    def submit(self, n: int, name: str, payload: str, image, respond_on: bool) -> str | None:
        """Ajoute une commande à la file. Retourne un message d'erreur, ou None si acceptée."""
        if not self.enabled:
            return "commandes arrêtées"
        if not respond_on:
            return "émulateur arrêté : pas de commande"
        if name == "uc_mode":
            code = MODE_CODES.get(payload.strip().lower())
            if code is None:
                return f"mode inconnu : {', '.join(MODE_CODES)}"
            return self._enqueue(GLOBAL, "uc_mode", code, image)
        if n not in VERIFIED:
            return f"thermostat {n} non vérifié pour la commande"
        if name == "mode":
            value = MODE_PAYLOADS.get(payload.strip().lower())
            if value is None:
                return f"mode attendu : {' ou '.join(MODE_PAYLOADS)}"
            field = "marche"
        elif name == "preset":
            value = PRESET_COMMANDS.get(payload.strip().lower())
            if value is None:
                return f"préréglage attendu : {' ou '.join(PRESET_COMMANDS)}"
            field = "preset"
        elif name in ("consigne_basse", "consigne_haute"):
            try:
                temp = float(payload)
            except ValueError:
                return "consigne non numérique"
            preset = self._current_preset(image, n)
            if preset not in PRESET_CODES:
                return "préréglage inconnu, consigne refusée"
            if name == "consigne_basse":
                field = "consigne_chaud_confort" if preset == PRESET_CONFORT else "consigne_chaud_eco"
            else:
                field = "consigne_froid_confort" if preset == PRESET_CONFORT else "consigne_froid_eco"
            low, high = bounds_for_field(field)
            if not low <= temp <= high:
                return f"consigne hors bornes {low:g}-{high:g} pour {field}"
            value = int(round(temp * P.command_scale(field)))
        else:
            return f"commande inconnue : {name}"
        return self._enqueue(n, field, value, image)

    def stop(self):
        self.enabled = False
        self.queue.clear()
        self.log("arret_commandes", pending=self.pending is not None)

    def resume(self):
        self.enabled = True
        self.log("reprise_commandes")

    def tick(self, emu, image, now: float):
        """À appeler régulièrement : confirmation, remise à zéro, puis envoi de la commande suivante."""
        if self.pending is not None:
            p = self.pending
            if p.field == "uc_mode":
                confirmed = image.get((UC, "hold", MODE_RETURN)) == p.encoded
            else:
                _, _, uc_off = FIELDS[p.field]
                confirmed = image.get((UC, "hold", block_base(p.n) + uc_off)) == p.encoded
            if confirmed:
                self.log("commande_confirmee", thermostat=p.n, field=p.field, value=p.encoded,
                         delai_s=round(now - p.sent_at, 1))
                self.pending = None
                self._clear(emu, self.last_n)
            elif now - p.sent_at > CONFIRM_WAIT:
                self.log("commande_non_confirmee", thermostat=p.n, field=p.field, value=p.encoded)
                self.pending = None
                self._clear(emu, self.last_n)
            return

        if emu.input.get(SIGNAL) == 1 and self.last_n is not None and now - self.last_sent > CLEAR_AFTER:
            self._clear(emu, self.last_n)

        if not self.enabled or not self.queue or now - self.last_sent < MIN_GAP:
            return
        if emu.input.get(SIGNAL, 0) != 0:
            return
        n, field, value = self.queue.pop(0)
        if field == "uc_mode":
            emu.input[MODE_CODE] = value
            emu.input[MODE_UPDATE] = 1
        else:
            mask, cmd_off, _ = FIELDS[field]
            base = cmd_base(n)
            emu.input[base] = mask
            emu.input[base + cmd_off] = value
        emu.input[SIGNAL] = 1
        self.pending = Pending(n, field, value, now)
        self.last_sent = now
        self.last_n = n
        self.log("commande_envoyee", thermostat=n, field=field, value=value)

    def _clear(self, emu, n):
        emu.input[SIGNAL] = 0
        if n == GLOBAL:
            emu.input[MODE_UPDATE] = 0
            self.log("zone_commande_remise_a_zero", mode=True)
        elif n is not None:
            emu.input[cmd_base(n)] = 0
            self.log("zone_commande_remise_a_zero", thermostat=n)
