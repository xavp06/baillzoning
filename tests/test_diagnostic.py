"""Tests du mode diagnostic : rapport à partir d'une image synthétique, sans bus.

Lancer depuis la racine du projet : python3 -m unittest discover -s tests -t . -v
"""

import unittest
from collections import Counter

from bus_profile import Profile, DEFAULT_PROFILE, PROFILES_DIR
from diagnostic import ABSENT, ECART, HORS_PROFIL, NON_OBSERVE, OK, A_VERIFIER, analyse

P = Profile.load(str(PROFILES_DIR / f"{DEFAULT_PROFILE}.json"))


def _image(thermostats=(1, 2, 3, 4, 5), firmware=82):
    img = {}
    uc, intesis = 10, 32
    if firmware is not None:
        img[(uc, "hold", 0)] = firmware
    img[(uc, "hold", 1)] = 1
    img[(uc, "hold", 8)] = 2
    img[(intesis, "hold", 0)] = 1
    img[(intesis, "hold", 1)] = 1
    img[(intesis, "hold", 9)] = 100
    img[(intesis, "hold", 11)] = 0
    for n in thermostats:
        b = P.thermostat_base(n)
        img[(uc, "hold", b + 0)] = 43690
        img[(uc, "hold", b + 1)] = 195
        img[(uc, "hold", b + 2)] = 0
        img[(uc, "hold", b + 3)] = 1
        for off in (4, 5, 6, 7):
            img[(uc, "hold", b + off)] = 200
        img[(uc, "hold", b + 8)] = 1
        img[(uc, "hold", b + 9)] = 4
        img[(uc, "hold", b + 10)] = 25
        img[(uc, "hold", b + 12)] = 200
    return img


def _stats(**over):
    base = {"frames": 1000, "bad_crc": 0, "req_units": Counter({10: 500, 32: 300}), "writes_gw": 50,
            "fc4_gw": 0, "duration_s": 600.0}
    base.update(over)
    return base


def _statuts(report, texte=None):
    return [c for c in report["controles"] if texte is None or texte in c["controle"]]


class DiagnosticTest(unittest.TestCase):
    def test_bus_conforme(self):
        r = analyse(_image(), _stats(), P)
        self.assertEqual([c for c in r["controles"] if c["statut"] in (ECART, HORS_PROFIL)], [])
        self.assertEqual(r["resume"].get(OK, 0) > 0, True)

    def test_thermostat_absent_du_profil(self):
        r = analyse(_image(thermostats=(1, 2, 3, 4)), _stats(), P)
        self.assertTrue(any(c["statut"] == ABSENT and "thermostat 5" in c["controle"] for c in r["controles"]))

    def test_thermostat_hors_profil(self):
        r = analyse(_image(thermostats=(1, 2, 3, 4, 5, 6)), _stats(), P)
        self.assertTrue(any(c["statut"] == HORS_PROFIL and "thermostat 6" in c["controle"] for c in r["controles"]))

    def test_valeur_de_preset_inconnue(self):
        img = _image()
        img[(10, "hold", P.thermostat_base(2) + 8)] = 7
        r = analyse(img, _stats(), P)
        self.assertTrue(any(c["statut"] == ECART and "thermostat 2" in c["controle"] for c in r["controles"]))

    def test_temperature_implausible(self):
        img = _image()
        img[(10, "hold", P.thermostat_base(3) + 1)] = 990
        r = analyse(img, _stats(), P)
        self.assertTrue(any(c["statut"] == ECART and "thermostat 3" in c["controle"] for c in r["controles"]))

    def test_firmware_different(self):
        r = analyse(_image(firmware=83), _stats(), P)
        self.assertTrue(any(c["statut"] == ECART and c["controle"] == "firmware UC" for c in r["controles"]))

    def test_firmware_non_observe(self):
        r = analyse(_image(firmware=None), _stats(), P)
        self.assertTrue(any(c["statut"] == NON_OBSERVE and c["controle"] == "firmware UC" for c in r["controles"]))

    def test_trames_au_crc_invalide(self):
        r = analyse(_image(), _stats(bad_crc=50), P)
        self.assertTrue(any(c["statut"] == A_VERIFIER and "CRC" in c["controle"] for c in r["controles"]))

    def test_aucune_ecriture_vers_la_passerelle(self):
        r = analyse(_image(), _stats(writes_gw=0), P)
        self.assertTrue(any(c["statut"] == A_VERIFIER and "écritures" in c["controle"] for c in r["controles"]))



if __name__ == "__main__":
    unittest.main()
