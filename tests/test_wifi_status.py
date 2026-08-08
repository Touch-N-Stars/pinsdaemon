import unittest
from unittest.mock import patch

from app import main


class FakeProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self._stdout = stdout.encode()
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


class WifiStatusHelpersTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_active_wifi_client_connection_prefers_configured_client_and_skips_hotspot(self):
        output = "\n".join(
            [
                "pins-123:802-11-wireless:wlan1",
                "OtherWifi:802-11-wireless:wlan2",
                "pins-client-605c408f2ebf168c:802-11-wireless:wlan0",
            ]
        )

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProcess(output)

        with patch.object(main, "_get_configured_wifi_interfaces", return_value=("wlan0", "wlan1")):
            with patch.object(main.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                connection_name, interface = await main._read_active_wifi_client_connection()

        self.assertEqual(connection_name, "pins-client-605c408f2ebf168c")
        self.assertEqual(interface, "wlan0")

    def test_managed_client_profile_name_is_not_classified_as_hotspot(self):
        self.assertFalse(main._is_hotspot_connection_name("pins-client-605c408f2ebf168c"))
        self.assertTrue(main._is_hotspot_connection_name("pins-ce29c"))

    async def test_read_nmcli_ipv4_address_strips_cidr_suffix(self):
        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProcess("192.168.1.42/24\n")

        with patch.object(main.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            ip_address = await main._read_nmcli_ipv4_address("wlan0")

        self.assertEqual(ip_address, "192.168.1.42")

    async def test_read_active_wifi_connections_includes_client_and_hotspot_roles(self):
        output = "\n".join(
            [
                "pins-123:802-11-wireless:wlan1",
                "pins-client-605c408f2ebf168c:802-11-wireless:wlan0",
            ]
        )

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProcess(output)

        with patch.object(main, "_get_configured_wifi_interfaces", return_value=("wlan0", "wlan1")):
            with patch.object(main.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                connections = await main._read_active_wifi_connections()

        self.assertEqual(
            connections,
            [
                {
                    "connectionName": "pins-client-605c408f2ebf168c",
                    "interface": "wlan0",
                    "role": "client",
                    "preferred": "true",
                },
                {
                    "connectionName": "pins-123",
                    "interface": "wlan1",
                    "role": "hotspot",
                    "preferred": None,
                },
            ],
        )

    async def test_read_wifi_connection_metrics_parses_signal_channel_frequency_and_band(self):
        output = "InfraWifi:80:1:2412 MHz\n*:HomeWifi:67:36:5180 MHz\n"

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProcess(output)

        with patch.object(main.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            metrics = await main._read_wifi_connection_metrics("wlan0")

        self.assertEqual(
            metrics,
            {
                "ssid": "HomeWifi",
                "signalStrength": 67,
                "quality": "67/100",
                "channel": 36,
                "frequency": 5180.0,
                "band": "5GHz",
            },
        )


if __name__ == "__main__":
    unittest.main()
