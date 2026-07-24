"""Tests for the gate logic (version gate, age gate) and config parsing.

Pure Python — pacman's vercmp is injected so the suite runs anywhere:
    python -m unittest discover -s tests
"""

import unittest
from pathlib import Path

from cellar import config as config_mod
from cellar.core import SECONDS_PER_DAY, Status, evaluate

NOW = 1_800_000_000.0
DAY = SECONDS_PER_DAY


def fake_vercmp(a: str, b: str) -> int:
    """Simplified pacman vercmp: dotted numeric versions like '1.2.3-1'."""

    def parts(version: str):
        pkgver, _, pkgrel = version.partition("-")
        return [int(x) for x in pkgver.split(".")] + [int(pkgrel or 0)]

    pa, pb = parts(a), parts(b)
    return (pa > pb) - (pa < pb)


def run(installed, aur_info, *, config=None, ignored=frozenset(), now=NOW):
    config = config or config_mod.Config()
    return evaluate(installed, aur_info, config, ignored, now, fake_vercmp)


def aur_pkg(name, version, age_days):
    return {"Name": name, "Version": version, "LastModified": int(NOW - age_days * DAY)}


class VersionGateTests(unittest.TestCase):
    def test_equal_versions_up_to_date_even_if_just_pushed(self):
        """A cosmetic PKGBUILD edit (fresh LastModified, same version) must
        not make a current package look aging: the version gate comes first."""
        [report] = run({"foo": "1.0-1"}, {"foo": aur_pkg("foo", "1.0-1", age_days=0)})
        self.assertIs(report.status, Status.UP_TO_DATE)

    def test_installed_newer_than_aur_is_up_to_date(self):
        [report] = run({"foo": "2.0-1"}, {"foo": aur_pkg("foo", "1.9-1", age_days=0)})
        self.assertIs(report.status, Status.UP_TO_DATE)


class AgeGateTests(unittest.TestCase):
    def test_young_update_is_held(self):
        [report] = run({"foo": "1.0-1"}, {"foo": aur_pkg("foo", "1.1-1", age_days=2)})
        self.assertIs(report.status, Status.AGING)
        self.assertAlmostEqual(report.remaining_days(NOW), 5.0, places=5)

    def test_aged_update_is_ready(self):
        [report] = run({"foo": "1.0-1"}, {"foo": aur_pkg("foo", "1.1-1", age_days=8)})
        self.assertIs(report.status, Status.READY)

    def test_exact_threshold_is_ready(self):
        [report] = run({"foo": "1.0-1"}, {"foo": aur_pkg("foo", "1.1-1", age_days=7)})
        self.assertIs(report.status, Status.READY)

    def test_just_under_threshold_is_held(self):
        info = {"Name": "foo", "Version": "1.1-1", "LastModified": int(NOW - 7 * DAY + 60)}
        [report] = run({"foo": "1.0-1"}, {"foo": info})
        self.assertIs(report.status, Status.AGING)

    def test_missing_last_modified_fails_safe(self):
        info = {"Name": "foo", "Version": "1.1-1"}
        [report] = run({"foo": "1.0-1"}, {"foo": info})
        self.assertIs(report.status, Status.AGING)
        self.assertIsNone(report.remaining_days(NOW))

    def test_clock_resets_on_each_new_version(self):
        """v2 aged past the threshold but v3 is fresh: the package is held
        entirely — no version stepping."""
        [report] = run({"foo": "1.0-1"}, {"foo": aur_pkg("foo", "3.0-1", age_days=1)})
        self.assertIs(report.status, Status.AGING)
        self.assertEqual(report.latest, "3.0-1")


class OverrideTests(unittest.TestCase):
    def test_trusted_package_is_ready_immediately(self):
        cfg = config_mod.Config(packages={"foo": config_mod.PackageOverride(trust=True)})
        [report] = run(
            {"foo": "1.0-1"}, {"foo": aur_pkg("foo", "1.1-1", age_days=0)}, config=cfg
        )
        self.assertIs(report.status, Status.READY)
        self.assertTrue(report.trusted)

    def test_per_package_days_override(self):
        cfg = config_mod.Config(packages={"foo": config_mod.PackageOverride(days=3)})
        [report] = run(
            {"foo": "1.0-1"}, {"foo": aur_pkg("foo", "1.1-1", age_days=4)}, config=cfg
        )
        self.assertIs(report.status, Status.READY)

    def test_per_package_days_can_lengthen(self):
        cfg = config_mod.Config(packages={"foo": config_mod.PackageOverride(days=14)})
        [report] = run(
            {"foo": "1.0-1"}, {"foo": aur_pkg("foo", "1.1-1", age_days=10)}, config=cfg
        )
        self.assertIs(report.status, Status.AGING)


class SkipAndIgnoreTests(unittest.TestCase):
    def test_package_absent_from_aur_is_skipped(self):
        [report] = run({"local-thing": "1.0-1"}, {})
        self.assertIs(report.status, Status.NOT_IN_AUR)

    def test_pacman_ignorepkg_is_honored(self):
        [report] = run(
            {"foo": "1.0-1"},
            {"foo": aur_pkg("foo", "1.1-1", age_days=30)},
            ignored=frozenset({"foo"}),
        )
        self.assertIs(report.status, Status.IGNORED)
        self.assertTrue(report.outdated)


class StaleHoldTests(unittest.TestCase):
    def test_long_held_package_is_stale(self):
        [report] = run({"foo": "1.0-1"}, {"foo": aur_pkg("foo", "1.1-1", age_days=2)})
        report.held_since = NOW - 35 * DAY
        self.assertTrue(report.is_stale(NOW, 30))

    def test_recent_hold_is_not_stale(self):
        [report] = run({"foo": "1.0-1"}, {"foo": aur_pkg("foo", "1.1-1", age_days=2)})
        report.held_since = NOW - 5 * DAY
        self.assertFalse(report.is_stale(NOW, 30))


class ConfigTests(unittest.TestCase):
    PATH = Path("/test/config.toml")

    def test_defaults(self):
        cfg = config_mod._parse({}, self.PATH)
        self.assertEqual(cfg.days, 7)
        self.assertIsNone(cfg.helper)
        self.assertEqual(cfg.stale_days, 30)

    def test_full_config(self):
        cfg = config_mod._parse(
            {
                "days": 10,
                "helper": "yay",
                "packages": {"a": {"trust": True}, "b": {"days": 3}},
            },
            self.PATH,
        )
        self.assertEqual(cfg.days, 10)
        self.assertEqual(cfg.helper, "yay")
        self.assertTrue(cfg.is_trusted("a"))
        self.assertEqual(cfg.required_days("b"), 3)
        self.assertEqual(cfg.required_days("unknown"), 10)

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod._parse({"day": 3}, self.PATH)

    def test_unknown_package_key_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod._parse({"packages": {"a": {"trusted": True}}}, self.PATH)

    def test_invalid_helper_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod._parse({"helper": "pikaur"}, self.PATH)

    def test_negative_days_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod._parse({"days": -1}, self.PATH)

    def test_boolean_days_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod._parse({"days": True}, self.PATH)


class AurBatchingTests(unittest.TestCase):
    def test_batches_cover_all_names_in_order(self):
        from cellar.aur import _batches

        names = [f"package-{i}" for i in range(500)]
        batches = list(_batches(names))
        self.assertGreater(len(batches), 1)
        self.assertEqual([n for b in batches for n in b], names)

    def test_batch_urls_stay_under_limit(self):
        from cellar.aur import _MAX_URL_LENGTH, _batches, _quoted_arg, RPC_URL

        names = [f"some-fairly-long-package-name-{i}" for i in range(400)]
        for batch in _batches(names):
            url = RPC_URL + "?" + "&".join(_quoted_arg(n) for n in batch)
            self.assertLessEqual(len(url), _MAX_URL_LENGTH)


if __name__ == "__main__":
    unittest.main()
