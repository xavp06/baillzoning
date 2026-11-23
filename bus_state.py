"""Image des registres en mémoire, alimentée par les trames décodées.

Règles :
- une trame au CRC invalide n'est jamais appliquée ;
- une requête FC3/FC4 mémorise l'adresse, la réponse associée met à jour l'image ;
- une écriture FC16 est appliquée à l'acquittement (réponse de 8 octets), pas à la requête.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from modbus_rtu import Frame

TABLES = {3: "hold", 4: "input", 16: "hold"}


@dataclass
class Change:
    ts: float
    unit: int
    table: str
    addr: int
    old: int | None
    new: int
    source: str  # "read" ou "write"

    def as_dict(self):
        return {"ts": self.ts, "unit": self.unit, "table": self.table, "addr": self.addr,
                "old": self.old, "new": self.new, "source": self.source}


class BusState:
    def __init__(self):
        self.image: dict[tuple[int, str, int], int] = {}
        self.pending: dict[int, tuple[int, int, list[int]]] = {}
        self.stats = {"frames": 0, "bad_crc": 0, "changes": 0}

    def feed(self, frame: Frame) -> list[Change]:
        self.stats["frames"] += 1
        if not frame.crc_ok:
            self.stats["bad_crc"] += 1
            return []

        if frame.kind == "req":
            if frame.fc in (3, 4):
                self.pending[frame.unit] = (frame.fc, frame.addr, [])
            elif frame.fc == 16:
                self.pending[frame.unit] = (16, frame.addr, frame.values)
            return []

        if frame.kind == "resp":
            pending = self.pending.pop(frame.unit, None)
            if pending is None:
                return []
            fc, addr, written = pending
            if fc in (3, 4):
                return self._apply(frame.ts, frame.unit, TABLES[fc], addr, frame.values, "read")
            if fc == 16:
                return self._apply(frame.ts, frame.unit, TABLES[fc], addr, written, "write")

        return []

    def _apply(self, ts, unit, table, start, values, source):
        out = []
        for i, value in enumerate(values):
            key = (unit, table, start + i)
            old = self.image.get(key)
            if old != value:
                self.image[key] = value
                out.append(Change(ts, unit, table, start + i, old, value, source))
        self.stats["changes"] += len(out)
        return out


class Journal:
    """Journal JSON Lines : trames brutes et changements, append-only."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.frames = open(directory / "frames.jsonl", "a", encoding="utf-8")
        self.events = open(directory / "events.jsonl", "a", encoding="utf-8")

    def write_frame(self, frame: Frame):
        self.frames.write(json.dumps({
            "ts": frame.ts, "unit": frame.unit, "fc": frame.fc, "kind": frame.kind,
            "crc_ok": frame.crc_ok, "addr": frame.addr, "count": frame.count,
            "values": frame.values, "raw": frame.raw.hex(),
        }) + "\n")

    def write_changes(self, changes: list[Change]):
        for change in changes:
            self.events.write(json.dumps(change.as_dict()) + "\n")

    def flush(self):
        self.frames.flush()
        self.events.flush()

    def close(self):
        self.frames.close()
        self.events.close()
