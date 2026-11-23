"""Découpage d'un flux Modbus RTU continu (TCP transparent) en trames.

Le TCP ne respecte pas les frontières des trames : une trame peut arriver coupée, ou plusieurs
trames groupées. Ce décodeur reconnaît chaque trame par sa structure et son CRC.

Pour une trame commençant à un offset donné, les longueurs possibles sont :
- FC3/FC4 : 8 (requête) ou 5 + byte count (réponse)
- FC16    : 8 (acquittement) ou 9 + byte count (requête)
- exception (FC | 0x80) : 5
Une candidate est acceptée si son CRC est valide. Sinon, on élimine l'octet de tête et on
resynchronise.
"""

from modbus_rtu import Frame, crc16, parse_rtu

MAX_PENDING = 300  # au-delà, on abandonne le premier octet (flux désynchronisé)


class RtuStreamDecoder:
    def __init__(self):
        self.buf = bytearray()
        self.discarded = 0

    def feed(self, data: bytes, ts: float = 0.0) -> list[Frame]:
        self.buf += data
        frames: list[Frame] = []
        while len(self.buf) >= 4:
            frame, need = self._try_one(bytes(self.buf), ts)
            if frame is not None:
                frames.append(frame)
                del self.buf[:len(frame.raw)]
                continue
            if need is not None and len(self.buf) < need:
                break  # trame incomplète : attendre la suite du flux
            if len(self.buf) > MAX_PENDING or need is None:
                del self.buf[0]
                self.discarded += 1
                continue
            del self.buf[0]
            self.discarded += 1
        return frames

    def _candidates(self, data: bytes):
        unit, fc = data[0], data[1]
        base = fc & 0x7F
        if fc & 0x80:
            return [5]
        if base in (3, 4):
            cands = [8]
            if len(data) >= 3:
                cands.append(5 + data[2])
            return cands
        if base == 16:
            cands = [8]
            if len(data) >= 7:
                cands.append(9 + data[6])
            return cands
        return []

    def _try_one(self, data: bytes, ts: float):
        cands = self._candidates(data)
        if not cands:
            return None, None
        longest = max(cands)
        for length in sorted(set(cands)):
            if len(data) < length:
                continue
            body, crc = data[:length - 2], data[length - 2:length]
            if crc16(body) == int.from_bytes(crc, "little"):
                try:
                    return parse_rtu(data[:length], ts), None
                except ValueError:
                    continue
        if len(data) < longest:
            return None, longest  # il manque encore des octets pour trancher
        return None, None
