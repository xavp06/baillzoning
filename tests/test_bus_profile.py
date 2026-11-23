"""Tests du profil de bus : validation, et concordance avec les constantes du code actuel.

Lancer depuis la racine du projet : python3 -m unittest test_bus_profile -v
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from bus_profile import DEFAULT_PROFILE, PROFILES_DIR, Profile, ProfileError, load_profile

PROFILE_FILE = PROFILES_DIR / f"{DEFAULT_PROFILE}.json"


def _write_variant(mutate) -> Path:
    data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    mutate(data)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, tmp)
    tmp.close()
    return Path(tmp.name)


class ProfileLoadTest(unittest.TestCase):
    def test_profil_par_defaut_valide(self):
        self.assertEqual(load_profile()["name"], DEFAULT_PROFILE)

    def test_profil_inconnu_refuse(self):
        with self.assertRaises(ProfileError):
            load_profile("inexistant")

    def test_champ_requis_manquant_refuse(self):
        path = _write_variant(lambda d: d["thermostats"].pop("block"))
        with self.assertRaises(ProfileError):
            load_profile(str(path))

    def test_bit_de_masque_en_double_refuse(self):
        def dup(d):
            d["thermostats"]["command"]["fields"]["consigne_froid_eco"]["bit"] = 8
        path = _write_variant(dup)
        with self.assertRaisesRegex(ProfileError, "bit"):
            load_profile(str(path))

    def test_uc_offset_incoherent_refuse(self):
        def bad(d):
            d["thermostats"]["command"]["fields"]["marche"]["uc_offset"] = 4
        path = _write_variant(bad)
        with self.assertRaisesRegex(ProfileError, "uc_offset"):
            load_profile(str(path))

    def test_codes_de_preset_incoherents_refuse(self):
        def bad(d):
            d["thermostats"]["presets"]["commands"]["eco"] = 3
        path = _write_variant(bad)
        with self.assertRaisesRegex(ProfileError, "préréglage"):
            load_profile(str(path))


class ProfileMatchesCodeTest(unittest.TestCase):
    """Le profil par défaut doit reproduire les valeurs de l'ancien code (figées ici)."""

    @classmethod
    def setUpClass(cls):
        cls.p = Profile.load(str(PROFILE_FILE))

    def test_adresses_thermostats(self):
        for n in range(1, 7):
            with self.subTest(n=n):
                self.assertEqual(self.p.thermostat_base(n), 16 + 20 * (n - 1))
                self.assertEqual(self.p.command_base(n), 10 * n)

    def test_champs_de_commande(self):
        self.assertEqual(self.p.command_fields(), {
            "marche": (1, 1, 3),
            "consigne_chaud_confort": (2, 2, 4),
            "consigne_chaud_eco": (4, 3, 5),
            "consigne_froid_confort": (8, 4, 6),
            "consigne_froid_eco": (16, 5, 7),
            "preset": (32, 6, 8),
        })

    def test_codes_de_mode(self):
        self.assertEqual(self.p.mode_codes(), {"arret": 0, "froid": 1, "chauffage": 2, "deshumidification": 3})

    def test_marqueur_et_seuil_pile(self):
        self.assertEqual(self.p.marker_value(), 43690)
        self.assertEqual(self.p.read["tension_pile"]["low_below"], 2.35)

    def test_preset_lu(self):
        self.assertEqual(self.p.preset_names(), {1: "confort", 2: "eco"})

    def test_offsets_lecture_thermostat(self):
        read = self.p.read
        self.assertEqual(read["temperature"]["offset"], 1)
        self.assertEqual(read["marche"]["offset"], 3)
        self.assertEqual(read["preset"]["offset"], 8)
        self.assertEqual(read["servo_etat"]["offset"], 9)
        self.assertEqual(read["tension_pile"]["offset"], 10)
        self.assertEqual(read["zone"]["offset"], 2)
        self.assertEqual(read["consigne_effective"]["offset"], 12)

    def test_timings(self):
        self.assertEqual(self.p.timing["min_gap"], 5)
        self.assertEqual(self.p.timing["clear_after"], 75)
        self.assertEqual(self.p.timing["confirm_wait"], 150)
        self.assertEqual(self.p.timing["max_queue"], 10)

    def test_bornes_par_defaut(self):
        self.assertEqual(tuple(self.p.limits["hot"]), (16, 30))
        self.assertEqual(tuple(self.p.limits["cold"]), (16, 30))

    def test_unites(self):
        self.assertEqual(self.p.units, {"uc": 10, "intesis": 32, "emulator": 10})


class VerifiedThermostatsTest(unittest.TestCase):
    def setUp(self):
        self.p = Profile.load(str(PROFILE_FILE))

    def test_thermostats_valides_acceptes(self):
        self.p.check_verified({1, 2, 3, 4, 5})

    def test_thermostat_non_valide_refuse(self):
        with self.assertRaisesRegex(ProfileError, "hors des blocs validés"):
            self.p.check_verified({1, 6})


if __name__ == "__main__":
    unittest.main()
