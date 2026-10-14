import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app import main

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("saved_wifi_profiles", ROOT / "scripts/pins-wifi-profile.py")
wifi = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wifi
SPEC.loader.exec_module(wifi)
SPEC_AUTO = importlib.util.spec_from_file_location("saved_wifi_auto", ROOT / "scripts/wifi-automanage.py")
auto = importlib.util.module_from_spec(SPEC_AUTO)
SPEC_AUTO.loader.exec_module(auto)
UUID = str(uuid.uuid4())


def network(profile_uuid=UUID):
    return dict(uuid=profile_uuid, ssid="Home", name="Saved Home", interface=None,
                compatible=True, auto_connect=True, priority=100, last_connected=0,
                hidden=False, band="", connected=False)


class FakeNmcli:
    def __init__(self, profiles, scan=None):
        self.profiles = profiles
        self.scan = scan
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append(args)
        if "--active" in args:
            return wifi.CommandResult(0, "")
        if "UUID,TYPE" in args:
            return wifi.CommandResult(0, "\n".join(f"{u}:802-11-wireless" for u in self.profiles))
        if "--rescan" in args:
            return self.scan or wifi.CommandResult(0, "Home\nField\n")
        if "-f" in args and "uuid" in args:
            fields = self.profiles[args[-1]]
            return wifi.CommandResult(0, "\n".join(f"{k}:{v}" for k, v in fields.items()))
        return wifi.CommandResult(0)


class SavedNetworkTests(unittest.TestCase):
    def test_metadata_filters_ap_and_sorts_priority_then_recency(self):
        fields = {"802-11-wireless.mode": "infrastructure", "connection.autoconnect": "yes",
                  "connection.autoconnect-priority": "100", "connection.timestamp": "1",
                  "802-11-wireless.ssid": r"Field\: WiFi", "connection.id": "Field"}
        nm = FakeNmcli({"old": fields, "new": {**fields, "connection.timestamp": "5"},
                       "first": {**fields, "connection.autoconnect-priority": "200"},
                       "other": {**fields, "connection.interface-name": "wlan1"},
                       "ap": {**fields, "802-11-wireless.mode": "ap"}})
        listing = wifi.saved_networks(nm, "wlan0")
        self.assertEqual([n["uuid"] for n in listing], ["first", "new", "old", "other"])
        self.assertEqual(listing[0]["ssid"], "Field: WiFi")
        self.assertFalse(listing[-1]["compatible"])
        self.assertNotIn("--show-secrets", str(nm.calls))

    def test_failure_tries_next_priority_without_rewriting_settings(self):
        first = network("first")
        second = {**network("second"), "ssid": "Field", "priority": 50}
        args = SimpleNamespace(client_iface="wlan0", hotspot_iface="wlan0", profile_uuid=None, config_file="unused")
        nm = FakeNmcli({})
        with patch.object(wifi, "saved_networks", return_value=[first, second]), patch.object(
            wifi, "_profile_has_usable_credentials", return_value=True
        ), patch.object(wifi, "_activate", side_effect=[wifi.WifiFailure("INVALID_CREDENTIALS", "bad"), None]) as activate, patch.object(
            wifi, "_verify_connection_commit"
        ), patch.object(wifi, "_persist_success") as persist:
            self.assertEqual(wifi.activate_saved_networks(args, nm), "second")
        self.assertEqual([c.args[1] for c in activate.call_args_list], ["first", "second"])
        self.assertEqual(persist.call_args.kwargs["profile_uuid"], "second")
        self.assertFalse(any("modify" in call for call in nm.calls))

    def test_selection_skips_disabled_incompatible_and_invisible_but_keeps_hidden(self):
        candidates = [network("visible"), {**network("disabled"), "auto_connect": False},
                      {**network("other"), "compatible": False},
                      {**network("missing"), "ssid": "Missing"},
                      {**network("hidden"), "ssid": "Secret field", "hidden": True}]
        args = SimpleNamespace(client_iface="wlan0", hotspot_iface="wlan1", profile_uuid=None, config_file="unused")
        with patch.object(wifi, "saved_networks", return_value=candidates), patch.object(
            wifi, "_profile_has_usable_credentials", return_value=False
        ) as credentials:
            with self.assertRaises(wifi.WifiFailure):
                wifi.activate_saved_networks(args, FakeNmcli({}))
        self.assertEqual([c.args[1] for c in credentials.call_args_list], ["visible", "hidden"])

    def test_uuid_connection_preserves_disabled_autoconnect_and_band(self):
        args = SimpleNamespace(client_iface="wlan0", hotspot_iface="wlan0", profile_uuid=UUID, config_file="unused")
        saved = {**network(), "auto_connect": False, "band": "bg"}
        nm = FakeNmcli({})
        with patch.object(wifi, "saved_networks", return_value=[saved]), patch.object(
            wifi, "_profile_has_usable_credentials", return_value=True
        ), patch.object(wifi, "_activate"), patch.object(wifi, "_verify_connection_commit"), patch.object(
            wifi, "_persist_success"
        ) as persist:
            wifi.activate_saved_networks(args, nm)
        self.assertFalse(persist.call_args.kwargs["auto_connect"])
        self.assertEqual(persist.call_args.kwargs["band"], "bg")
        self.assertFalse(any("--rescan" in call for call in nm.calls))

    def test_forget_active_client_and_hotspot_are_rejected(self):
        args = SimpleNamespace(client_iface="wlan0", profile_uuid=UUID, action="forget")
        nm = FakeNmcli({})
        for listing in [[{**network(), "connected": True}], []]:
            with patch.object(wifi, "saved_networks", return_value=listing):
                with self.assertRaises(wifi.WifiFailure):
                    wifi.manage_saved_network(args, nm)
        self.assertEqual(nm.calls, [])

    def test_normal_reconnection_preserves_custom_priority(self):
        args = SimpleNamespace(config_file="unused", client_iface="wlan0", hotspot_iface="wlan0",
                               ssid="Home", band="", auto_connect=True)
        nm = FakeNmcli({})
        with patch.object(wifi, "_load_config", return_value={}), patch.object(wifi, "_profile_value", return_value="250"), patch.object(
            wifi, "_find_saved_profile", return_value=UUID
        ), patch.object(wifi, "_activate"), patch.object(wifi, "_verify_connection_commit"), patch.object(wifi, "_persist_success"):
            wifi.connect(args, "", nm)
        self.assertIn(["connection", "modify", "uuid", UUID, "connection.autoconnect-priority", "250"], nm.calls)


class BackgroundRetryTests(unittest.TestCase):
    def test_retry_cooldown_and_single_radio_stations(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            auto, "RETRY_STATE_FILE", str(Path(directory) / "attempt")
        ), patch.object(auto, "hotspot_is_ready", return_value=True), patch.object(
            auto, "run_command", return_value=subprocess.CompletedProcess([], 0, "Station aa:bb", "")
        ) as run:
            auto.remember_attempt(1000)
            self.assertFalse(auto.background_retry_allowed("wlan0", "wlan0", 1100))
            run.assert_not_called()
            self.assertFalse(auto.background_retry_allowed("wlan0", "wlan0", 1121))
            run.return_value.stdout = ""
            self.assertTrue(auto.background_retry_allowed("wlan0", "wlan0", 1121))
            run.return_value.returncode = 1
            self.assertFalse(auto.background_retry_allowed("wlan0", "wlan0", 1121))

    def test_two_radio_retry_does_not_inspect_ap_stations(self):
        with patch.object(auto, "RETRY_STATE_FILE", "nonexistent"), patch.object(auto, "run_command") as run:
            self.assertTrue(auto.background_retry_allowed("wlan0", "wlan1", 1000))
            run.assert_not_called()

    def test_healthy_active_client_is_preserved_even_if_legacy_ssid_differs(self):
        with patch.object(auto, "load_config", return_value={"ssid": "Old", "auto_connect": True}), patch.object(
            auto, "resolve_wifi_interfaces", return_value=("wlan0", "wlan0")
        ), patch.object(auto, "client_is_healthy", return_value=True), patch.object(auto, "saved_client_networks") as listing, patch.object(
            auto, "start_hotspot"
        ) as hotspot:
            auto.main()
        listing.assert_not_called()
        hotspot.assert_not_called()


class SavedNetworkApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def authorize(self):
        main.app.dependency_overrides[main.verify_token] = lambda: None

    def test_all_saved_network_routes_require_authentication(self):
        for method, path, body in [("get", "/wifi/networks", None),
                                   ("put", f"/wifi/networks/{UUID}", {"priority": 1}),
                                   ("delete", f"/wifi/networks/{UUID}", None),
                                   ("post", f"/wifi/networks/{UUID}/connect", None)]:
            response = self.client.request(method, path, json=body)
            self.assertIn(response.status_code, [401, 403])

    def test_invalid_uuid_priority_and_empty_updates_never_launch_jobs(self):
        self.authorize()
        with patch.object(main, "_start_saved_wifi_job", new=AsyncMock()) as start, patch.object(
            main, "_saved_wifi_network", new=AsyncMock(return_value=network())
        ):
            for value in [1000, -1000, 1.5, True, "100"]:
                self.assertEqual(self.client.put(f"/wifi/networks/{UUID}", json={"priority": value}).status_code, 422)
            self.assertEqual(self.client.put("/wifi/networks/not-a-uuid", json={"priority": 1}).status_code, 422)
            self.assertEqual(self.client.put(f"/wifi/networks/{UUID}", json={}).status_code, 400)
            start.assert_not_called()

    def test_listing_exposes_metadata_without_secrets(self):
        self.authorize()
        with patch.object(main, "_resolve_wifi_interfaces", new=AsyncMock(return_value=("wlan0", "wlan0"))), patch.object(
            main, "_run_wifi_profile_helper", new=AsyncMock(return_value=json.dumps({"networks": [{**network(), "password": "redacted"}]}))
        ):
            response = self.client.get("/wifi/networks")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("password", response.text)

    def test_update_builds_validated_privileged_job(self):
        self.authorize()
        job = dict(jobId="job", status="started", exitCode=None, startedAt=1, finishedAt=None, command="update")
        with patch.object(main, "_saved_wifi_network", new=AsyncMock(return_value=network())), patch.object(
            main, "_resolve_wifi_interfaces", new=AsyncMock(return_value=("wlan0", "wlan0"))
        ), patch.object(main, "_start_saved_wifi_job", new=AsyncMock(return_value=job)) as start:
            response = self.client.put(f"/wifi/networks/{UUID}", json={"priority": 0, "auto_connect": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[0][-4:], ["--priority", "0", "--auto-connect", "no"])

    def test_active_network_forget_is_conflict(self):
        self.authorize()
        with patch.object(main, "_saved_wifi_network", new=AsyncMock(return_value={**network(), "connected": True})):
            self.assertEqual(self.client.delete(f"/wifi/networks/{UUID}").status_code, 409)


if __name__ == "__main__":
    unittest.main()
