"""Tests de la file de commandes, avec un émulateur factice. Vérifie les écritures produites.

Lancer depuis la racine du projet : python3 -m unittest test_commands -v
"""

import os
import unittest

os.environ["VERIFIED_THERMOSTATS"] = "2,3"

import commands  # noqa: E402
from commands import CommandManager  # noqa: E402


class FakeEmu:
    def __init__(self):
        self.input = {}


def _image(preset_thermo2=1, uc_mode=None, uc_hot=None):
    image = {(10, "hold", 36 + 8): preset_thermo2}
    if uc_mode is not None:
        image[(10, "hold", 8)] = uc_mode
    if uc_hot is not None:
        image[(10, "hold", 36 + 4)] = uc_hot
    return image


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.cm = CommandManager(lambda event, **data: self.logs.append((event, data)))
        self.emu = FakeEmu()

    def test_consigne_confort_chaud_ecrit_masque_et_valeur(self):
        err = self.cm.submit(2, "consigne_basse", "20.5", _image(), True)
        self.assertIsNone(err)
        self.cm.tick(self.emu, _image(), 100.0)
        self.assertEqual(self.emu.input[20], 2)     # masque : consigne chaud confort
        self.assertEqual(self.emu.input[22], 205)   # valeur en dixièmes
        self.assertEqual(self.emu.input[0], 1)      # signal

    def test_preset_eco_ecrit_code_2(self):
        self.assertIsNone(self.cm.submit(3, "preset", "eco", _image(), True))
        self.cm.tick(self.emu, _image(), 100.0)
        self.assertEqual(self.emu.input[30], 32)
        self.assertEqual(self.emu.input[36], 2)

    def test_marche_off_ecrit_zero(self):
        self.assertIsNone(self.cm.submit(2, "mode", "off", _image(), True))
        self.cm.tick(self.emu, _image(), 100.0)
        self.assertEqual(self.emu.input[20], 1)
        self.assertEqual(self.emu.input[21], 0)

    def test_mode_general_ecrit_code_registre_4(self):
        self.assertIsNone(self.cm.submit(0, "uc_mode", "chauffage", _image(), True))
        self.cm.tick(self.emu, _image(), 100.0)
        self.assertEqual(self.emu.input[4], 2)
        self.assertEqual(self.emu.input[3], 1)
        self.assertEqual(self.emu.input[0], 1)

    def test_thermostat_non_verifie_refuse(self):
        err = self.cm.submit(4, "preset", "eco", _image(), True)
        self.assertIn("non vérifié", err)

    def test_consigne_hors_bornes_refusee(self):
        err = self.cm.submit(2, "consigne_basse", "31", _image(), True)
        self.assertIn("hors bornes", err)

    def test_consigne_froid_utilise_bornes_froid(self):
        err = self.cm.submit(2, "consigne_haute", "15", _image(preset_thermo2=2), True)
        self.assertIn("consigne_froid_eco", err)

    def test_emulateur_arrete_refuse(self):
        self.assertIn("émulateur arrêté", self.cm.submit(2, "preset", "eco", _image(), False))

    def test_deja_applique_ignore(self):
        self.assertIsNone(self.cm.submit(2, "consigne_basse", "20.5", _image(uc_hot=205), True))
        self.assertEqual(self.cm.queue, [])
        self.assertEqual(self.logs[-1][0], "deja_applique")

    def test_confirmation_remet_le_signal_a_zero(self):
        self.cm.submit(2, "preset", "eco", _image(), True)
        self.cm.tick(self.emu, _image(), 100.0)
        self.cm.tick(self.emu, {(10, "hold", 36 + 8): 2}, 110.0)
        self.assertEqual(self.emu.input[0], 0)
        self.assertEqual(self.emu.input[20], 0)
        self.assertEqual(self.logs[-2][0], "commande_confirmee")

    def test_constantes_du_profil(self):
        self.assertEqual(commands.MIN_GAP, 5)
        self.assertEqual(commands.CLEAR_AFTER, 75)
        self.assertEqual(commands.MAX_QUEUE, 10)
        self.assertEqual(commands.MODE_CODES["chauffage"], 2)


if __name__ == "__main__":
    unittest.main()
