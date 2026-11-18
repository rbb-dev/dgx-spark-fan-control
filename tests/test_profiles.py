"""Named profiles, the status file and the zero-RPM guard of userspace/dgx_fan_control.py.

A fake thermal tree and a fake hwmon tree stand in for sysfs: no fan, no root, no Spark.
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "userspace" / "dgx_fan_control.py"
fc = None


def setUpModule():
    global fc
    spec = importlib.util.spec_from_file_location("dgx_fan_control_profiles", MODULE_PATH)
    fc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fc)


class Rig:
    """A fake /sys/class/thermal, a fake hwmon, a profile file and a status file in one temp directory."""

    def __init__(self, test):
        self.root = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.root, True)
        self.thermal = self.root / "thermal"
        self.hwmon = self.root / "hwmon"
        cd = self.thermal / "cooling_device0"
        cd.mkdir(parents=True)
        (cd / "type").write_text("dgx_ec_fan_floor\n")
        (cd / "max_state").write_text("12\n")
        (cd / "cur_state").write_text("0\n")
        self.cd = cd
        self.zone = self.thermal / "thermal_zone0"
        self.zone.mkdir()
        h = self.hwmon / "hwmon3"
        h.mkdir(parents=True)
        (h / "name").write_text("dgx_ec_fan\n")
        self.h = h
        self.profile = self.root / "profile"
        self.status = self.root / "run" / "status.json"
        self.status.parent.mkdir()
        self.set_rpm(3000, 4000)
        self.set_temp(40.0)
        self.t = 1000.0

    def set_temp(self, c):
        (self.zone / "temp").write_text(f"{int(c * 1000)}\n")

    def set_rpm(self, a, b):
        (self.h / "fan1_input").write_text(f"{a}\n")
        (self.h / "fan2_input").write_text(f"{b}\n")

    def set_profile(self, text):
        self.profile.write_text(text)

    def state(self):
        return int((self.cd / "cur_state").read_text())

    def ctl(self):
        return fc.Controller(self.thermal, profile_file=self.profile, status_file=self.status,
                             hwmon_root=self.hwmon, clock=lambda: self.t)

    def doc(self):
        return json.loads(self.status.read_text())

    def steps(self, ctl, n, temp=None):
        for _ in range(n):
            if temp is not None:
                self.set_temp(temp)
            ctl.step()
            self.t += 2.0


def settle(rig, ctl, temp, n=14):
    rig.steps(ctl, n, temp)
    return rig.state()


class Curves(unittest.TestCase):
    def test_profile_table_names_and_floors(self):
        for name in ("stock", "medium", "performance", "max", "hold-2700", "hold-4500", "hold-6300", "hold-9000", "hold-13500"):
            self.assertIn(name, fc.valid_profiles())
        for bad in ("", "Max", "hold-5000", "hold-", "hold-4500x", "quiet", "max\n; rm", "../max"):
            self.assertNotIn(bad, fc.valid_profiles())
        self.assertEqual(fc.FLOOR_RPM[3], 4500)
        self.assertEqual(fc.FLOOR_RPM[12], 13500)
        self.assertEqual(fc.FLOOR_RPM[0], 0)

    def test_curve_targets(self):
        rows = {
            "performance": {40: 0, 50: 3, 54.9: 3, 55: 5, 60: 8, 65: 10, 70: 12, 80: 12},
            "medium": {40: 0, 50: 0, 54.9: 0, 55: 2, 60: 4, 65: 6, 70: 8, 75: 10, 80: 12},
            "stock": {40: 0, 60: 0, 80: 0},
            "max": {40: 12, 60: 12},
            "hold-4500": {40: 3, 50: 3, 55: 5, 70: 12},
            "hold-6300": {40: 5, 54.9: 5, 60: 8},
            "hold-9000": {40: 8, 60: 8, 65: 10, 70: 12},
        }
        for profile, temps in rows.items():
            for t, want in temps.items():
                self.assertEqual(fc.profile_target(profile, t), want, (profile, t))

    def test_unknown_profile_target_refused(self):
        with self.assertRaises(ValueError):
            fc.profile_target("quiet", 50.0)


class Runtime(unittest.TestCase):
    def test_stock_holds_zero_and_writes_nothing_more(self):
        r = Rig(self)
        r.set_profile("stock\n")
        c = r.ctl()
        self.assertEqual(settle(r, c, 85.0, 3), 0)
        self.assertEqual(r.doc()["profile"], "stock")
        self.assertEqual(r.doc()["state"], 0)

    def test_max_writes_12_at_once(self):
        r = Rig(self)
        r.set_profile("max\n")
        c = r.ctl()
        r.steps(c, 1, 30.0)
        self.assertEqual(r.state(), 12)
        d = r.doc()
        self.assertEqual((d["profile"], d["state"], d["floor_rpm"]), ("max", 12, 13500))

    def test_performance_rises_two_per_pass_and_falls_one(self):
        r = Rig(self)
        r.set_profile("performance")
        c = r.ctl()
        r.steps(c, 1, 72.0)
        self.assertEqual(r.state(), 2)
        r.steps(c, 1, 72.0)
        self.assertEqual(r.state(), 4)
        for _ in range(8):
            r.steps(c, 1, 72.0)
        self.assertEqual(r.state(), 12)
        # cool down: one state a pass, only when the curve 4 C hotter is lower
        r.steps(c, 40, 40.0)
        self.assertEqual(r.state(), 0)

    def test_medium_follows_its_own_curve(self):
        r = Rig(self)
        r.set_profile("medium")
        c = r.ctl()
        self.assertEqual(settle(r, c, 57.0), 2)
        self.assertEqual(settle(r, c, 66.0, 20), 6)
        self.assertEqual(settle(r, c, 82.0, 20), 12)

    def test_hold_is_a_floor_that_yields_to_heat(self):
        r = Rig(self)
        r.set_profile("hold-4500")
        c = r.ctl()
        self.assertEqual(settle(r, c, 35.0), 3)
        self.assertEqual(settle(r, c, 72.0, 20), 12)
        self.assertEqual(settle(r, c, 35.0, 40), 3)
        self.assertEqual(r.doc()["floor_rpm"], 4500)

    def test_profile_change_takes_effect_next_pass(self):
        r = Rig(self)
        r.set_profile("max")
        c = r.ctl()
        r.steps(c, 1, 40.0)
        self.assertEqual(r.state(), 12)
        r.set_profile("performance")
        r.steps(c, 1, 40.0)
        self.assertEqual(r.doc()["profile"], "performance")
        self.assertEqual(r.doc()["requested"], "performance")
        # leaving max is not abrupt: down one state a pass
        self.assertEqual(r.state(), 11)
        r.steps(c, 14, 40.0)
        self.assertEqual(r.state(), 0)
        r.set_profile("stock")
        r.steps(c, 1, 40.0)
        self.assertEqual(r.doc()["profile"], "stock")

    def test_bad_profile_files_mean_performance_with_an_error(self):
        cases = {
            "garbage": lambda r: r.set_profile("turbo!!\n"),
            "empty": lambda r: r.set_profile(""),
            "hold-5000": lambda r: r.set_profile("hold-5000"),
            "65 bytes": lambda r: r.set_profile("max" + " " * 62),
            "missing": lambda r: None,
            "directory": lambda r: r.profile.mkdir(),
            "symlink": lambda r: (r.set_profile("max\n"), os.rename(r.profile, r.root / "real"),
                                  os.symlink(r.root / "real", r.profile)),
            "binary": lambda r: r.profile.write_bytes(b"\xff\xfe\x00max"),
        }
        for name, make in cases.items():
            with self.subTest(name):
                r = Rig(self)
                make(r)
                c = r.ctl()
                r.steps(c, 1, 40.0)
                d = r.doc()
                self.assertEqual(d["profile"], "performance", name)
                self.assertIsInstance(d["profile_error"], str, name)
                self.assertTrue(d["profile_error"], name)
                self.assertLess(r.state(), 12, name)

    def test_good_file_clears_the_error(self):
        r = Rig(self)
        r.set_profile("nonsense")
        c = r.ctl()
        r.steps(c, 1, 40.0)
        self.assertTrue(r.doc()["profile_error"])
        r.set_profile("max")
        r.steps(c, 1, 40.0)
        self.assertIsNone(r.doc()["profile_error"])
        self.assertEqual(r.doc()["profile"], "max")

    def test_requested_shows_the_file_when_bad(self):
        r = Rig(self)
        r.set_profile("turbo")
        c = r.ctl()
        r.steps(c, 1, 40.0)
        self.assertEqual(r.doc()["requested"], "turbo")
        self.assertEqual(r.doc()["profile"], "performance")

    def test_temperature_failure_gives_12_except_stock(self):
        for prof, want in (("performance", 12), ("medium", 12), ("max", 12), ("hold-4500", 12), ("stock", 0)):
            with self.subTest(prof):
                r = Rig(self)
                r.set_profile(prof)
                c = r.ctl()
                (r.zone / "temp").unlink()
                for _ in range(8):
                    c.step()
                    r.t += 2
                self.assertEqual(r.state(), want)
                self.assertIsNone(r.doc()["hottest_c"])


class ZeroGuard(unittest.TestCase):
    def test_three_zero_passes_while_warm_force_12_then_release(self):
        r = Rig(self)
        r.set_profile("stock")
        c = r.ctl()
        r.set_rpm(0, 4000)
        r.steps(c, 2, 55.0)
        self.assertEqual(r.state(), 0)
        self.assertFalse(r.doc()["zero_rpm_guard"])
        r.steps(c, 1, 55.0)
        self.assertEqual(r.state(), 12)
        self.assertTrue(r.doc()["zero_rpm_guard"])
        r.steps(c, 3, 55.0)
        self.assertEqual(r.state(), 12)
        r.set_rpm(2700, 4000)
        r.steps(c, 1, 55.0)
        self.assertFalse(r.doc()["zero_rpm_guard"])
        r.steps(c, 14, 55.0)
        self.assertEqual(r.state(), 0)

    def test_cold_box_does_not_trip_it(self):
        r = Rig(self)
        r.set_profile("stock")
        c = r.ctl()
        r.set_rpm(0, 0)
        r.steps(c, 6, 38.0)
        self.assertFalse(r.doc()["zero_rpm_guard"])
        self.assertEqual(r.state(), 0)

    def test_a_good_reading_resets_the_count(self):
        r = Rig(self)
        r.set_profile("stock")
        c = r.ctl()
        r.set_rpm(0, 0)
        r.steps(c, 2, 55.0)
        r.set_rpm(2700, 4000)
        r.steps(c, 1, 55.0)
        r.set_rpm(0, 0)
        r.steps(c, 2, 55.0)
        self.assertFalse(r.doc()["zero_rpm_guard"])

    def test_unreadable_rpm_is_not_zero(self):
        r = Rig(self)
        r.set_profile("stock")
        shutil.rmtree(r.hwmon)
        c = r.ctl()
        r.steps(c, 6, 60.0)
        self.assertFalse(r.doc()["zero_rpm_guard"])
        self.assertIsNone(r.doc()["rpm"][0])


class Status(unittest.TestCase):
    KEYS = {"v", "at", "profile", "requested", "profile_error", "state", "floor_rpm", "hottest_c", "filtered_c",
            "rpm", "zero_rpm_guard", "driver"}

    def test_every_key_and_values(self):
        r = Rig(self)
        r.set_profile("performance")
        r.set_rpm(3620, 5400)
        c = r.ctl()
        r.steps(c, 1, 57.4)
        d = r.doc()
        self.assertEqual(set(d), self.KEYS)
        self.assertEqual(d["v"], 1)
        self.assertEqual(d["at"], 1000.0)
        self.assertEqual(d["rpm"], [3620, 5400])
        self.assertAlmostEqual(d["hottest_c"], 57.4, places=1)
        self.assertEqual(d["state"], r.state())
        self.assertEqual(d["floor_rpm"], fc.FLOOR_RPM[r.state()])
        self.assertEqual(d["driver"], "0.1.4")
        self.assertEqual(os.stat(r.status).st_mode & 0o777, 0o644)

    def test_replaced_atomically_no_temp_left(self):
        r = Rig(self)
        r.set_profile("max")
        c = r.ctl()
        r.steps(c, 3, 40.0)
        self.assertEqual(sorted(p.name for p in r.status.parent.iterdir()), ["status.json"])

    def test_unwritable_status_does_not_stop_the_fans(self):
        r = Rig(self)
        r.set_profile("max")
        r.status = r.root / "nodir" / "status.json"
        c = r.ctl()
        r.steps(c, 1, 40.0)
        self.assertEqual(r.state(), 12)


class Cli(unittest.TestCase):
    def test_hidden_options_and_default_paths(self):
        p = fc.build_parser()
        a = p.parse_args(["--profile-file", "/x/p", "--status-file", "/x/s", "--hwmon-root", "/x/h", "daemon"])
        self.assertEqual((str(a.profile_file), str(a.status_file), str(a.hwmon_root)), ("/x/p", "/x/s", "/x/h"))
        a = p.parse_args(["daemon"])
        self.assertEqual(str(a.profile_file), "/var/lib/dgx-fan-control/profile")
        self.assertEqual(str(a.status_file), "/run/dgx-fan-control/status.json")

    def test_upstream_pieces_are_still_there(self):
        self.assertEqual(fc.curve_state(66.0), 10)
        self.assertEqual(fc.next_state(0, 72.0), 2)


class Unit(unittest.TestCase):
    def test_unit_gives_the_status_file_a_runtime_directory_and_stays_confined(self):
        lines = (MODULE_PATH.parents[1] / "systemd" / "dgx-fan-control.service").read_text().splitlines()
        self.assertIn("RuntimeDirectory=dgx-fan-control", lines)
        self.assertIn("RuntimeDirectoryMode=0755", lines)
        self.assertIn("ProtectSystem=strict", lines)
        self.assertIn("CapabilityBoundingSet=", lines)


if __name__ == "__main__":
    unittest.main()
