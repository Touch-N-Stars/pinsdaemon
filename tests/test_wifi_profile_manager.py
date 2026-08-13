import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pins-wifi-profile.py"
SPEC = importlib.util.spec_from_file_location("pins_wifi_profile", SCRIPT)
wifi = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = wifi
SPEC.loader.exec_module(wifi)


class FakeNmcli:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def run(self, args, timeout=60):
        self.calls.append(list(args))
        if self.results:
            return self.results.pop(0)
        return wifi.CommandResult(0, "", "")


class WifiProfileIdentityTests(unittest.TestCase):
    def test_managed_profile_identity_is_deterministic_and_not_the_ssid(self):
        first = wifi.profile_identity("Field Network")
        self.assertEqual(first, wifi.profile_identity("Field Network"))
        self.assertNotEqual(first[0], "Field Network")
        self.assertNotEqual(first, wifi.profile_identity("Other Network"))

    def test_terse_parser_handles_colons_and_backslashes_in_ssid(self):
        self.assertEqual(wifi._split_terse(r"Field\:Net:WPA2"), ["Field:Net", "WPA2"])
        self.assertEqual(wifi._split_terse(r"Field\\Net:--"), [r"Field\Net", "--"])

    def test_missing_secret_is_classified_without_returning_the_secret(self):
        failure = wifi._classify_activation(
            wifi.CommandResult(4, "", "Secrets were required, but not provided"),
            False,
        )
        self.assertEqual(failure.code, "MISSING_CREDENTIALS")
        self.assertNotIn("password", failure.safe_message.lower())

    def test_wrong_password_is_classified_as_invalid_credentials(self):
        failure = wifi._classify_activation(
            wifi.CommandResult(4, "", "802.1x authentication failed: secret rejected"),
            True,
        )
        self.assertEqual(failure.code, "INVALID_CREDENTIALS")

        nm_secret_wording = wifi._classify_activation(
            wifi.CommandResult(4, "", "Secrets were required, but not provided"),
            True,
        )
        self.assertEqual(nm_secret_wording.code, "INVALID_CREDENTIALS")

    def test_network_not_found_is_classified(self):
        failure = wifi._classify_activation(
            wifi.CommandResult(10, "", "No network with SSID was found"),
            True,
        )
        self.assertEqual(failure.code, "NETWORK_NOT_FOUND")

    def test_ip_configuration_failure_is_classified(self):
        failure = wifi._classify_activation(
            wifi.CommandResult(4, "", "IP configuration could not be reserved"),
            True,
        )
        self.assertEqual(failure.code, "IP_CONFIGURATION_FAILED")


class ConnectionViabilityTests(unittest.TestCase):
    def test_helper_invocation_is_bounded_and_contains_no_secret(self):
        completed = subprocess.CompletedProcess(
            [],
            0,
            "PINS_WIFI_VIABILITY status=healthy mode=gateway reason=gateway_present\n",
            "",
        )
        with patch.object(wifi.subprocess, "run", return_value=completed) as run:
            result, output = wifi._run_viability_check("wlan0")

        argv = run.call_args.args[0]
        self.assertEqual(argv[0], wifi.VIABILITY_COMMAND)
        self.assertIn("--connection-commit", argv)
        self.assertIn("--confirm-peer", argv)
        self.assertEqual(argv[argv.index("--interface") + 1], "wlan0")
        self.assertEqual(argv[argv.index("--peer-state-file") + 1], wifi.PEER_STATE_FILE)
        self.assertNotIn("token", " ".join(argv).lower())
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(output, completed.stdout.strip())

    def test_gateway_or_verified_peer_is_accepted(self):
        nm = FakeNmcli()
        for mode, reason in (
            ("gateway", "gateway_present"),
            ("pins-peer", "peer_identity_confirmed"),
        ):
            with self.subTest(mode=mode), patch.object(
                wifi,
                "_run_viability_check",
                return_value=(
                    subprocess.CompletedProcess([], 0),
                    f"PINS_WIFI_VIABILITY status=healthy mode={mode} reason={reason}",
                ),
            ):
                wifi._verify_connection_commit(nm, "wlan0", "wlan1")

    def test_unverified_gatewayless_client_is_rejected(self):
        nm = FakeNmcli()
        with patch.object(
            wifi,
            "_run_viability_check",
            return_value=(
                subprocess.CompletedProcess([], 1),
                "PINS_WIFI_VIABILITY status=unhealthy mode=pins-peer reason=pins_health_unavailable",
            ),
        ):
            with self.assertRaises(wifi.WifiFailure) as raised:
                wifi._verify_connection_commit(nm, "wlan0", "wlan1")
        self.assertEqual(raised.exception.code, "GATEWAY_UNREACHABLE")

    def test_invalid_success_output_fails_closed(self):
        with patch.object(
            wifi,
            "_run_viability_check",
            return_value=(
                subprocess.CompletedProcess([], 0),
                "PINS_WIFI_VIABILITY status=healthy mode=unknown reason=unexpected",
            ),
        ):
            with self.assertRaises(wifi.WifiFailure) as raised:
                wifi._verify_connection_commit(FakeNmcli(), "wlan0", "wlan1")
        self.assertEqual(raised.exception.code, "UNKNOWN")

    def test_parallel_pins_address_conflict_suspends_managed_ap_and_retries(self):
        nm = FakeNmcli()
        failed = (
            subprocess.CompletedProcess([], 1),
            "PINS_WIFI_VIABILITY status=unhealthy mode=pins-peer "
            "reason=dhcp_server_is_local_address:10.42.0.1",
        )
        healthy = (
            subprocess.CompletedProcess([], 0),
            "PINS_WIFI_VIABILITY status=healthy mode=pins-peer "
            "reason=peer_identity_confirmed",
        )
        with patch.object(
            wifi, "_run_viability_check", side_effect=[failed, healthy]
        ) as check, patch.object(
            wifi, "_deactivate_parallel_pins_hotspot", return_value=True
        ) as deactivate:
            wifi._verify_connection_commit(nm, "wlan0", "wlan1")

        self.assertEqual(check.call_count, 2)
        deactivate.assert_called_once_with(nm, "wlan0", "wlan1", "10.42.0.1")

    def test_only_managed_ap_owning_conflicting_address_is_suspended(self):
        address = wifi.CommandResult(0, "10.42.0.1/24\n")
        active = wifi.CommandResult(0, "ap-uuid:802-11-wireless:wlan1\n")
        nm = FakeNmcli([address, active])
        with patch.object(wifi, "_profile_value", side_effect=["ap", "Hotspot"]):
            self.assertTrue(
                wifi._deactivate_parallel_pins_hotspot(
                    nm, "wlan0", "wlan1", "10.42.0.1"
                )
            )
        self.assertIn(
            ["connection", "modify", "uuid", "ap-uuid", "connection.autoconnect", "no"],
            nm.calls,
        )
        self.assertIn(["connection", "down", "uuid", "ap-uuid"], nm.calls)

        other_interface_address = FakeNmcli(
            [wifi.CommandResult(0, "192.168.1.10/24\n")]
        )
        self.assertFalse(
            wifi._deactivate_parallel_pins_hotspot(
                other_interface_address, "wlan0", "wlan1", "10.42.0.1"
            )
        )
        self.assertFalse(
            any(call[:2] == ["connection", "down"] for call in other_interface_address.calls)
        )


class SavedProfileTests(unittest.TestCase):
    def test_saved_profile_with_no_secret_fails_before_activation(self):
        nm = FakeNmcli()
        with patch.object(wifi, "_profile_exists", return_value=True), patch.object(
            wifi, "_wifi_profile_uuids", return_value=[]
        ), patch.object(
            wifi, "_profile_value", side_effect=lambda _nm, _uuid, field, **_kwargs: {
                "802-11-wireless.ssid": "Home",
                "802-11-wireless.mode": "infrastructure",
            }.get(field, "")
        ), patch.object(wifi, "_profile_has_usable_credentials", return_value=False):
            with self.assertRaises(wifi.WifiFailure) as raised:
                wifi._find_saved_profile(nm, "Home", "profile-uuid")
        self.assertEqual(raised.exception.code, "MISSING_CREDENTIALS")
        self.assertIn(
            ["connection", "modify", "uuid", "profile-uuid", "connection.autoconnect", "no"],
            nm.calls,
        )

    def test_profile_display_name_may_differ_from_ssid(self):
        nm = FakeNmcli()
        with patch.object(wifi, "_profile_exists", return_value=False), patch.object(
            wifi, "_wifi_profile_uuids", return_value=["uuid-with-unrelated-display-name"]
        ), patch.object(
            wifi, "_profile_value", side_effect=lambda _nm, _uuid, field, **_kwargs: {
                "802-11-wireless.ssid": "Home",
                "802-11-wireless.mode": "infrastructure",
            }.get(field, "")
        ), patch.object(wifi, "_profile_has_usable_credentials", return_value=True):
            self.assertEqual(
                wifi._find_saved_profile(nm, "Home", None),
                "uuid-with-unrelated-display-name",
            )

    def test_duplicate_usable_profiles_require_reconfiguration(self):
        nm = FakeNmcli()
        with patch.object(wifi, "_profile_exists", return_value=False), patch.object(
            wifi, "_wifi_profile_uuids", return_value=["one", "two"]
        ), patch.object(
            wifi, "_profile_value", side_effect=lambda _nm, _uuid, field, **_kwargs: {
                "802-11-wireless.ssid": "Home",
                "802-11-wireless.mode": "infrastructure",
            }.get(field, "")
        ), patch.object(wifi, "_profile_has_usable_credentials", return_value=True):
            with self.assertRaises(wifi.WifiFailure) as raised:
                wifi._find_saved_profile(nm, "Home", None)
        self.assertEqual(raised.exception.code, "PROFILE_NOT_FOUND")

    def test_hidden_network_can_reconnect_through_saved_profile(self):
        args = SimpleNamespace(
            config_file="unused", client_iface="wlan0", hotspot_iface="wlan0",
            ssid="Hidden", band="", auto_connect=True,
        )
        nm = FakeNmcli([wifi.CommandResult(0)])
        with patch.object(wifi, "_load_config", return_value={"client_profile_uuid": "saved"}), patch.object(
            wifi, "_find_saved_profile", return_value="saved"
        ), patch.object(wifi, "_activate") as activate, patch.object(
            wifi, "_verify_connection_commit"
        ), patch.object(wifi, "_persist_success"):
            self.assertEqual(wifi.connect(args, "", nm), "saved")
        activate.assert_called_once_with(nm, "saved", "wlan0", False)
        self.assertIn(
            ["connection", "modify", "uuid", "saved", "connection.autoconnect", "yes"],
            nm.calls,
        )

    def test_manual_saved_profile_connection_disables_networkmanager_autoconnect(self):
        args = SimpleNamespace(
            config_file="unused", client_iface="wlan0", hotspot_iface="wlan0",
            ssid="Home", band="", auto_connect=False,
        )
        nm = FakeNmcli([wifi.CommandResult(0)])
        with patch.object(wifi, "_load_config", return_value={"client_profile_uuid": "saved"}), patch.object(
            wifi, "_find_saved_profile", return_value="saved"
        ), patch.object(wifi, "_activate"), patch.object(
            wifi, "_verify_connection_commit"
        ), patch.object(wifi, "_persist_success"):
            self.assertEqual(wifi.connect(args, "", nm), "saved")
        self.assertIn(
            ["connection", "modify", "uuid", "saved", "connection.autoconnect", "no"],
            nm.calls,
        )


class TransactionTests(unittest.TestCase):
    def test_failed_new_secure_connection_does_not_persist_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wifi.json"
            original = {"ssid": "Old", "auto_connect": True, "client_profile_uuid": "old"}
            path.write_text(json.dumps(original), encoding="utf-8")
            args = SimpleNamespace(
                config_file=str(path), client_iface="wlan0", hotspot_iface="wlan0",
                ssid="New", band="", auto_connect=True,
            )
            nm = FakeNmcli([wifi.CommandResult(0)])
            with patch.object(wifi, "_scan_security", return_value="WPA2"), patch.object(
                wifi, "_create_profile"
            ), patch.object(
                wifi, "_activate", side_effect=wifi.WifiFailure("INVALID_CREDENTIALS", "failed")
            ), patch.object(wifi, "_delete_profile"):
                with self.assertRaises(wifi.WifiFailure):
                    wifi.connect(args, "wrong-password", nm)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_successful_secure_connection_persists_only_profile_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wifi.json"
            path.write_text("{}", encoding="utf-8")
            args = SimpleNamespace(
                config_file=str(path), client_iface="wlan0", hotspot_iface="wlan1",
                ssid="Home", band="bg", auto_connect=True,
            )
            nm = FakeNmcli([wifi.CommandResult(0)])
            with patch.object(wifi, "_scan_security", return_value="WPA2"), patch.object(
                wifi, "_create_profile"
            ), patch.object(wifi, "_activate"), patch.object(
                wifi, "_verify_connection_commit"
            ) as verify, patch.object(wifi, "_delete_profile"), patch.object(
                wifi, "_wifi_profile_uuids", return_value=[]
            ):
                profile_uuid = wifi.connect(args, "valid-password", nm)
            self.assertEqual(verify.call_count, 2)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["client_profile_uuid"], profile_uuid)
            self.assertEqual(saved["hotspot_interface"], "wlan1")
            self.assertNotIn("password", saved)
            self.assertNotIn("psk", saved)

    def test_lan_only_connection_needs_ipv4_but_not_an_internet_probe(self):
        nm = FakeNmcli([
            wifi.CommandResult(0),
            wifi.CommandResult(0, "profile-uuid\n192.168.1.20/24\n"),
        ])
        wifi._activate(nm, "profile-uuid", "wlan0", False)
        flattened = [item for call in nm.calls for item in call]
        self.assertNotIn("ping", flattened)
        self.assertNotIn("8.8.8.8", flattened)

    def test_missing_interface_is_explicit(self):
        args = SimpleNamespace(
            config_file="unused", client_iface="wlan9", hotspot_iface="wlan0",
            ssid="Home", band="", auto_connect=True,
        )
        nm = FakeNmcli([wifi.CommandResult(10)])
        with patch.object(wifi, "_load_config", return_value={}):
            with self.assertRaises(wifi.WifiFailure) as raised:
                wifi.connect(args, "password123", nm)
        self.assertEqual(raised.exception.code, "INTERFACE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
