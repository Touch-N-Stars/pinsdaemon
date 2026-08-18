import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import main


class PowerStatusParsingTests(unittest.TestCase):
    def test_parses_throttled_hex_value(self):
        self.assertEqual(main._parse_throttled_value("throttled=0x50005"), 0x50005)

    def test_rejects_unexpected_throttled_output(self):
        with self.assertRaises(ValueError):
            main._parse_throttled_value("not supported")

    def test_parses_pi_5_supply_voltage(self):
        self.assertEqual(
            main._parse_supply_voltage("EXT5V_V volt(24)=5.09066000V"),
            5.09066,
        )

    def test_missing_supply_voltage_is_supported(self):
        self.assertIsNone(main._parse_supply_voltage("error=1"))


class PowerStatusApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_decodes_current_and_historical_flags(self):
        async def capture(*arguments):
            if arguments == ("get_throttled",):
                return "throttled=0x50005"
            return "EXT5V_V volt(24)=4.81230000V"

        with patch.object(main, "_capture_vcgencmd", side_effect=capture):
            result = await main._read_power_status()

        self.assertEqual(
            result.model_dump(),
            {
                "supplyVoltage": 4.8123,
                "underVoltage": True,
                "underVoltageOccurred": True,
                "throttled": True,
                "throttlingOccurred": True,
                "armFrequencyCapped": False,
                "armFrequencyCappingOccurred": False,
                "softTemperatureLimit": False,
                "softTemperatureLimitOccurred": False,
                "rawValue": "0x50005",
                "source": "vcgencmd",
            },
        )

    async def test_reports_unavailable_firmware_status(self):
        with patch.object(main, "_capture_vcgencmd", new=AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as raised:
                await main._read_power_status()

        self.assertEqual(raised.exception.status_code, 500)

    async def test_endpoint_reuses_cached_status(self):
        status = main.PowerStatusResponse(
            underVoltage=False,
            underVoltageOccurred=False,
            throttled=False,
            throttlingOccurred=False,
            armFrequencyCapped=False,
            armFrequencyCappingOccurred=False,
            softTemperatureLimit=False,
            softTemperatureLimitOccurred=False,
            rawValue="0x0",
        )
        main._power_status_cache = (0.0, None)

        with patch.object(main, "_read_power_status", new=AsyncMock(return_value=status)) as read:
            first = await main.get_system_power_status()
            second = await main.get_system_power_status()

        self.assertIs(first, status)
        self.assertIs(second, status)
        read.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
