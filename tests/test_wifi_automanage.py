import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "wifi-automanage.py"
SPEC = importlib.util.spec_from_file_location("wifi_automanage", MODULE_PATH)
wifi_automanage = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(wifi_automanage)


class WifiAutomanageTests(unittest.TestCase):
    def test_normalize_band_accepts_api_and_nmcli_values(self):
        self.assertEqual(wifi_automanage.normalize_band("2.4GHz"), "bg")
        self.assertEqual(wifi_automanage.normalize_band("5GHz"), "a")
        self.assertEqual(wifi_automanage.normalize_band("bg"), "bg")
        self.assertEqual(wifi_automanage.normalize_band("a"), "a")
        self.assertEqual(wifi_automanage.normalize_band(None), "")
        self.assertEqual(wifi_automanage.normalize_band("auto"), "")

    def test_auto_mode_attempts_saved_profile_without_ap_mode_prescan(self):
        config = {
            "desired_mode": "auto",
            "ssid": "Home WiFi",
            "auto_connect": True,
            "band": "5GHz",
            "client_interface": "wlan0",
            "hotspot_interface": "wlan0",
        }

        with patch.object(wifi_automanage, "load_config", return_value=config), patch.object(
            wifi_automanage, "resolve_wifi_interfaces", return_value=("wlan0", "wlan0")
        ), patch.object(wifi_automanage, "connect_to_wifi", return_value=True) as connect, patch.object(
            wifi_automanage, "start_hotspot"
        ) as start_hotspot:
            with self.assertRaises(SystemExit) as exit_context:
                wifi_automanage.main()

        self.assertEqual(exit_context.exception.code, 0)
        connect.assert_called_once_with("Home WiFi", "5GHz", "wlan0", "wlan0")
        start_hotspot.assert_not_called()

    def test_failed_saved_profile_activation_falls_back_to_hotspot(self):
        config = {
            "desired_mode": "auto",
            "ssid": "Unavailable WiFi",
            "auto_connect": True,
            "band": None,
        }

        with patch.object(wifi_automanage, "load_config", return_value=config), patch.object(
            wifi_automanage, "resolve_wifi_interfaces", return_value=("wlan0", "wlan0")
        ), patch.object(wifi_automanage, "connect_to_wifi", return_value=False) as connect, patch.object(
            wifi_automanage, "start_hotspot"
        ) as start_hotspot:
            wifi_automanage.main()

        connect.assert_called_once_with("Unavailable WiFi", None, "wlan0", "wlan0")
        start_hotspot.assert_called_once_with("wlan0", "wlan0")

    def test_existing_verified_hotspot_is_not_recreated(self):
        with patch.object(wifi_automanage, "hotspot_is_ready", return_value=True), patch.object(
            wifi_automanage.subprocess, "run"
        ) as run:
            wifi_automanage.start_hotspot("wlan0", "wlan0")

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
