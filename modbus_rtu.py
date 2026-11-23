"""Décodage de trames Modbus RTU (une trame par datagramme UDP).

La longueur de chaque trame est déduite du protocole, pas de la taille du datagramme :
Wireshark ajoute parfois du padding en fin de trame (réponses marquées « Malformed »).
"""

from dataclasses import dataclass, field


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


@dataclass
class Frame:
    ts: float
    unit: int
    fc: int
    kind: str  # "req", "resp", "exc" ou "other"
    crc_ok: bool
    raw: bytes
    addr: int | None = None
    count: int | None = None
    values: list[int] = field(default_factory=list)
    trailing: bytes = b""


def _regs(data: bytes) -> list[int]:
    return [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data) - 1, 2)]


def parse_rtu(data: bytes, ts: float = 0.0) -> Frame:
    if len(data) < 4:
        raise ValueError(f"trame trop courte ({len(data)} octets)")

    unit, fc = data[0], data[1]
    base = fc & 0x7F

    if fc & 0x80:
        length, kind = 5, "exc"
        frame = Frame(ts, unit, base, kind, False, b"")
    elif base in (3, 4):
        if len(data) == 8:
            length, kind = 8, "req"
            frame = Frame(ts, unit, base, kind, False, b"",
                          addr=int.from_bytes(data[2:4], "big"),
                          count=int.from_bytes(data[4:6], "big"))
        else:
            bytecount = data[2]
            length, kind = 5 + bytecount, "resp"
            frame = Frame(ts, unit, base, kind, False, b"", values=_regs(data[3:3 + bytecount]))
    elif base == 16:
        if len(data) == 8:
            length, kind = 8, "resp"
            frame = Frame(ts, unit, base, kind, False, b"",
                          addr=int.from_bytes(data[2:4], "big"),
                          count=int.from_bytes(data[4:6], "big"))
        else:
            bytecount = data[6]
            length, kind = 9 + bytecount, "req"
            frame = Frame(ts, unit, base, kind, False, b"",
                          addr=int.from_bytes(data[2:4], "big"),
                          count=int.from_bytes(data[4:6], "big"),
                          values=_regs(data[7:7 + bytecount]))
    else:
        length, kind = len(data), "other"
        frame = Frame(ts, unit, base, kind, False, b"")

    if len(data) < length:
        raise ValueError(f"trame tronquée : {len(data)} octets, {length} attendus")

    frame.raw = data[:length]
    frame.trailing = data[length:]
    frame.crc_ok = crc16(data[:length - 2]) == int.from_bytes(data[length - 2:length], "little")
    return frame
