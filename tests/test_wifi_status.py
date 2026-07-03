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
                "HomeWifi:802-11-wireless:wlan0",
            ]
        )

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProcess(output)

        with patch.object(main, "_get_configured_wifi_interfaces", return_value=("wlan0", "wlan1")):
            with patch.object(main.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                connection_name, interface = await main._read_active_wifi_client_connection()

        self.assertEqual(connection_name, "HomeWifi")
        self.assertEqual(interface, "wlan0")

    async def test_read_nmcli_ipv4_address_strips_cidr_suffix(self):
        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProcess("192.168.1.42/24\n")

        with patch.object(main.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            ip_address = await main._read_nmcli_ipv4_address("wlan0")

        self.assertEqual(ip_address, "192.168.1.42")


if __name__ == "__main__":
    unittest.main()
