"""Émulateur de la passerelle BaillConnect (esclave Modbus 10), sans transport réseau.

Comportement :
- FC16 (écriture de l'UC) : mémorise les valeurs, répond par l'acquittement standard ;
- FC3 (relecture) : restitue ce qui a été écrit par l'UC ;
- FC4 (zone de commande) : restitue l'état de la zone de commande, alimenté par le site
  (non implémenté ici : à brancher sur le site ou HA plus tard).

Cette classe ne fait que transformer une requête en réponse : elle ne transmet rien.
"""

from bus_profile import current
from modbus_rtu import crc16, parse_rtu


class GatewayEmulator:
    def __init__(self, unit: int | None = None):
        self.unit = unit if unit is not None else current().units["emulator"]
        self.holding: dict[int, int] = {}
        self.input: dict[int, int] = {}

    def handle(self, request: bytes) -> bytes | None:
        frame = parse_rtu(request)
        if not frame.crc_ok or frame.unit != self.unit or frame.kind != "req":
            return None
        if frame.fc == 16:
            for i, value in enumerate(frame.values):
                self.holding[frame.addr + i] = value
            body = bytes([self.unit, 16]) + frame.addr.to_bytes(2, "big") + frame.count.to_bytes(2, "big")
            return _with_crc(body)
        if frame.fc == 3:
            return self._read(self.holding, frame.addr, frame.count, 3)
        if frame.fc == 4:
            return self._read(self.input, frame.addr, frame.count, 4)
        return None

    def known(self, addr: int, count: int, table: dict[int, int]) -> bool:
        return all(a in table for a in range(addr, addr + count))

    def _read(self, table, addr, count, fc):
        if not self.known(addr, count, table):
            return None
        data = b"".join(table[a].to_bytes(2, "big") for a in range(addr, addr + count))
        return _with_crc(bytes([self.unit, fc, len(data)]) + data)


def _with_crc(body: bytes) -> bytes:
    return body + crc16(body).to_bytes(2, "little")
