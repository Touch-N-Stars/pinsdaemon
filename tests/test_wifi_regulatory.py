import importlib.util
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "wifi_regulatory.py"


def load_module():
    spec = importlib.util.spec_from_file_location("pins_wifi_regulatory", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeRegulatorySystem:
    def __init__(self, configured="DE", runtime="DE", fail=None):
        self.configured = configured
        self.runtime = runtime
        self.fail = fail
        self.calls = []
        self.environments = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.environments.append((list(argv), kwargs.get("env")))
        operation = tuple(argv[1:])
        if self.fail == operation:
            return subprocess.CompletedProcess(argv, 1, "", "simulated failure")
        if operation == ("nonint", "get_wifi_country"):
            return subprocess.CompletedProcess(argv, 0, f"{self.configured}\n", "")
        if operation[:2] == ("nonint", "do_wifi_country"):
            self.configured = operation[2]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if operation[:2] == ("reg", "set"):
            self.runtime = operation[2]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if operation == ("reg", "get"):
            return subprocess.CompletedProcess(
                argv,
                0,
                f"global\ncountry {self.runtime}: DFS-TEST\n\nphy#0\ncountry 99: DFS-UNSET\n",
                "",
            )
        raise AssertionError(f"unexpected command: {argv}")


class WifiRegulatoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def write_cmdline(self, directory, content):
        path = Path(directory) / "cmdline.txt"
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def test_no_country_is_neutral_not_a_hard_coded_default(self):
        self.assertIsNone(self.module.normalize_country(None))
        self.assertIsNone(self.module.normalize_country(""))

    def test_stale_de_boot_override_is_reconciled_to_requested_us(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(
                directory,
                "console=tty1 root=PARTUUID=abc rootwait cfg80211.ieee80211_regdom=DE\n",
            )
            system = FakeRegulatorySystem(configured="DE", runtime="DE")

            result = self.module.apply_country(
                "US", path, run_command=system.run, sleep=lambda _seconds: None
            )

            self.assertEqual(result.configured, "US")
            self.assertEqual(result.boot, "US")
            self.assertEqual(result.runtime, "US")
            self.assertTrue(result.consistent)
            self.assertIn("cfg80211.ieee80211_regdom=US", path.read_text())
            self.assertNotIn("cfg80211.ieee80211_regdom=DE", path.read_text())

    def test_non_us_non_de_country_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(directory, "root=/dev/mmcblk0p2 quiet\n")
            system = FakeRegulatorySystem(configured="GB", runtime="GB")
            result = self.module.apply_country(
                "CA", path, run_command=system.run, sleep=lambda _seconds: None
            )
            self.assertEqual((result.configured, result.boot, result.runtime), ("CA", "CA", "CA"))

    def test_existing_correct_country_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(
                directory, "root=/dev/mmcblk0p2 cfg80211.ieee80211_regdom=US\n"
            )
            before = path.stat().st_ino
            changed = self.module.reconcile_boot_country(path, "US")
            self.assertFalse(changed)
            self.assertEqual(path.stat().st_ino, before)

            system = FakeRegulatorySystem(configured="US", runtime="US")
            result = self.module.apply_country(
                "US", path, run_command=system.run, sleep=lambda _seconds: None
            )
            self.assertTrue(result.consistent)
            self.assertNotIn(
                [self.module.DEFAULT_RASPI_CONFIG, "nonint", "do_wifi_country", "US"],
                system.calls,
            )

    def test_raspi_config_does_not_inherit_daemon_sudo_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(
                directory, "root=/dev/x cfg80211.ieee80211_regdom=DE\n"
            )
            system = FakeRegulatorySystem(configured="DE", runtime="DE")
            daemon_environment = {
                "SUDO_USER": "sysupdate-api",
                "SUDO_UID": "999",
                "SUDO_GID": "984",
                "SUDO_COMMAND": "/usr/local/bin/manage-localization.sh",
            }
            with patch.dict(os.environ, daemon_environment):
                self.module.apply_country(
                    "US", path, run_command=system.run, sleep=lambda _seconds: None
                )

            raspi_environments = [
                environment
                for argv, environment in system.environments
                if argv[0] == self.module.DEFAULT_RASPI_CONFIG
            ]
            self.assertTrue(raspi_environments)
            for environment in raspi_environments:
                self.assertIsNotNone(environment)
                self.assertFalse(set(daemon_environment).intersection(environment))

    def test_correct_persistent_country_still_repairs_stale_boot_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(
                directory, "root=/dev/mmcblk0p2 cfg80211.ieee80211_regdom=DE\n"
            )
            system = FakeRegulatorySystem(configured="US", runtime="DE")
            result = self.module.apply_country(
                "US", path, run_command=system.run, sleep=lambda _seconds: None
            )
            self.assertTrue(result.consistent)
            self.assertEqual(self.module.read_boot_countries(path), ["US"])

    def test_duplicate_tokens_are_replaced_with_one_deterministic_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(
                directory,
                "console=tty1 cfg80211.ieee80211_regdom=DE rootwait "
                "cfg80211.ieee80211_regdom=US quiet\n",
            )
            self.module.reconcile_boot_country(path, "AU")
            content = path.read_text(encoding="utf-8")
            self.assertEqual(content.count("cfg80211.ieee80211_regdom="), 1)
            self.assertIn("cfg80211.ieee80211_regdom=AU", content)

    def test_no_existing_token_is_added_without_changing_unrelated_arguments(self):
        original = (
            "console=serial0,115200 console=tty1 root=PARTUUID=abc "
            "rootfstype=ext4 fsck.repair=yes rootwait quiet splash other=value\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(directory, original)
            self.module.reconcile_boot_country(path, "GB")
            content = path.read_text(encoding="utf-8")
            for token in original.split():
                self.assertIn(token, content.split())
            self.assertEqual(content.splitlines().__len__(), 1)
            self.assertEqual(self.module.read_boot_countries(path), ["GB"])

    @unittest.skipIf(os.name == "nt", "Windows does not expose POSIX file modes")
    def test_atomic_rewrite_preserves_file_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(directory, "root=/dev/x cfg80211.ieee80211_regdom=DE\n")
            os.chmod(path, 0o640)
            self.module.reconcile_boot_country(path, "US")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_each_apply_or_verification_failure_is_reported(self):
        failures = [
            ("nonint", "do_wifi_country", "US"),
            ("reg", "set", "US"),
        ]
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                path = self.write_cmdline(
                    directory, "root=/dev/x cfg80211.ieee80211_regdom=DE\n"
                )
                system = FakeRegulatorySystem(fail=failure)
                with self.assertRaises(self.module.RegulatoryError):
                    self.module.apply_country(
                        "US", path, run_command=system.run, sleep=lambda _seconds: None
                    )

    def test_raspi_config_desktop_notification_failure_uses_postconditions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(
                directory, "root=/dev/x cfg80211.ieee80211_regdom=DE\n"
            )
            system = FakeRegulatorySystem(configured="DE", runtime="DE")
            messages = []

            def fail_after_apply(argv, **kwargs):
                if tuple(argv[1:3]) == ("nonint", "do_wifi_country"):
                    system.calls.append(list(argv))
                    system.environments.append((list(argv), kwargs.get("env")))
                    system.configured = argv[3]
                    return subprocess.CompletedProcess(
                        argv, 1, "", "session D-Bus notification failed"
                    )
                return system.run(argv, **kwargs)

            result = self.module.apply_country(
                "US",
                path,
                run_command=fail_after_apply,
                sleep=lambda _seconds: None,
                log=messages.append,
            )

            self.assertTrue(result.consistent)
            self.assertEqual((result.configured, result.boot, result.runtime), ("US", "US", "US"))
            self.assertTrue(any("returned non-zero" in message for message in messages))

    def test_boot_configuration_write_failure_is_reported_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(
                directory, "root=/dev/x cfg80211.ieee80211_regdom=DE\n"
            )
            system = FakeRegulatorySystem(configured="DE", runtime="DE")

            with patch.object(
                self.module,
                "reconcile_boot_country",
                side_effect=OSError("simulated filesystem detail"),
            ):
                with self.assertRaisesRegex(
                    self.module.RegulatoryError,
                    "boot regulatory configuration could not be persisted",
                ) as raised:
                    self.module.apply_country(
                        "US", path, run_command=system.run, sleep=lambda _seconds: None
                    )

            self.assertNotIn("simulated filesystem detail", str(raised.exception))

    def test_runtime_verification_failure_does_not_report_consistency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(directory, "root=/dev/x\n")
            system = FakeRegulatorySystem()

            def ignore_runtime_set(argv, **kwargs):
                result = system.run(argv, **kwargs)
                if tuple(argv[1:3]) == ("reg", "set"):
                    system.runtime = "DE"
                return result

            with self.assertRaisesRegex(self.module.RegulatoryError, "runtime"):
                self.module.apply_country(
                    "US", path, run_command=ignore_runtime_set, sleep=lambda _seconds: None
                )

    def test_repeated_updates_do_not_accumulate_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cmdline(directory, "root=/dev/x quiet\n")
            for country in ("GB", "CA", "GB"):
                self.module.reconcile_boot_country(path, country)
            self.assertEqual(self.module.read_boot_countries(path), ["GB"])

    def test_runtime_parser_uses_global_domain_not_driver_self_managed_domain(self):
        output = "global\ncountry US: DFS-FCC\n\nphy#0\ncountry 99: DFS-UNSET\n"
        self.assertEqual(self.module.parse_runtime_country(output), "US")


if __name__ == "__main__":
    unittest.main()
