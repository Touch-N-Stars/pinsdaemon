import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import main


class LocalizationParsingTests(unittest.TestCase):
    def test_reads_current_shell_assignments_without_quotes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "config"
            config.write_text('LANG="de_DE.UTF-8"\nXKBLAYOUT=de\n', encoding="utf-8")

            self.assertEqual(main._read_shell_assignment(str(config), "LANG"), "de_DE.UTF-8")
            self.assertEqual(main._read_shell_assignment(str(config), "XKBLAYOUT"), "de")

    def test_supported_locales_include_only_utf8_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            supported = Path(temp_dir) / "SUPPORTED"
            supported.write_text(
                "# comment\nde_DE.UTF-8 UTF-8\nde_DE ISO-8859-1\nen_GB.UTF-8 UTF-8\n",
                encoding="utf-8",
            )

            self.assertEqual(
                main._read_supported_locales(str(supported)),
                ["de_DE.UTF-8", "en_GB.UTF-8"],
            )

    def test_wifi_country_options_preserve_code_and_label(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            countries = Path(temp_dir) / "iso3166.tab"
            countries.write_text("DE\tGermany\nUS\tUnited States\n", encoding="utf-8")

            result = main._read_wifi_country_options(str(countries))

            self.assertEqual([(item.code, item.name) for item in result], [
                ("DE", "Germany"),
                ("US", "United States"),
            ])

    def test_keyboard_layout_options_include_human_readable_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            rules = Path(temp_dir) / "base.lst"
            rules.write_text(
                "! model\n  pc105 Generic\n! layout\n"
                "  us English (US)\n  gb English (UK)\n  de German\n"
                "! variant\n  intl us: English (US, intl.)\n",
                encoding="utf-8",
            )

            result = main._read_keyboard_layout_options(str(rules), {"us", "gb"})

            self.assertEqual(
                [(item.code, item.name) for item in result],
                [("gb", "English (UK)"), ("us", "English (US)")],
            )


class LocalizationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_values_from_the_system(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            locale_file = Path(temp_dir) / "locale"
            keyboard_file = Path(temp_dir) / "keyboard"
            locale_file.write_text('LANG="en_GB.UTF-8"\n', encoding="utf-8")
            keyboard_file.write_text('XKBLAYOUT="de"\n', encoding="utf-8")

            async def capture(*command, **_kwargs):
                if command[0] == "timedatectl":
                    return "Europe/Berlin"
                return "DE"

            with patch.object(main, "DEFAULT_LOCALE_PATH", str(locale_file)):
                with patch.object(main, "DEFAULT_KEYBOARD_PATH", str(keyboard_file)):
                    with patch.object(main, "_capture_localization_command", side_effect=capture):
                        result = await main._read_system_localization()

        self.assertEqual(
            result.model_dump(),
            {
                "locale": "en_GB.UTF-8",
                "wifiCountry": "DE",
                "timezone": "Europe/Berlin",
                "keyboardLayout": "de",
            },
        )

    async def test_update_validates_options_and_starts_allowlisted_job(self):
        fake_job = SimpleNamespace(
            id="job-1",
            status=main.JobStatus.STARTED,
            exit_code=None,
            created_at=1.0,
            finished_at=None,
            command="manage-localization",
            error_code=None,
            error_message=None,
        )
        options = main.SystemLocalizationOptionsResponse(
            locales=["en_GB.UTF-8"],
            wifiCountries=[main.WifiCountryOption(code="DE", name="Germany")],
            timezones=["Europe/Berlin"],
            keyboardLayouts=["de"],
        )

        with tempfile.NamedTemporaryFile() as script:
            with patch.object(main, "LOCALIZATION_SCRIPT_PATH", script.name):
                with patch.object(main, "_read_localization_options", new=AsyncMock(return_value=options)):
                    with patch.object(
                        main.job_manager,
                        "start_job",
                        new=AsyncMock(return_value="job-1"),
                    ) as start_job:
                        with patch.object(main.job_manager, "get_job", return_value=fake_job):
                            result = await main.update_system_localization(
                                main.SystemLocalizationUpdateRequest(
                                    locale="en_GB.UTF-8",
                                    wifiCountry="de",
                                    timezone="Europe/Berlin",
                                    keyboardLayout="de",
                                )
                            )

        command = start_job.await_args.args[0]
        self.assertEqual(command[:3], ["sudo", "-n", script.name])
        self.assertEqual(
            command[3:],
            [
                "--locale",
                "en_GB.UTF-8",
                "--wifi-country",
                "DE",
                "--timezone",
                "Europe/Berlin",
                "--keyboard-layout",
                "de",
            ],
        )
        self.assertEqual(result.jobId, "job-1")

    async def test_update_rejects_an_unsupported_value_before_starting_a_job(self):
        options = main.SystemLocalizationOptionsResponse(
            locales=["en_GB.UTF-8"],
            wifiCountries=[],
            timezones=[],
            keyboardLayouts=[],
        )
        with patch.object(main, "_read_localization_options", new=AsyncMock(return_value=options)):
            with patch.object(main.job_manager, "start_job", new=AsyncMock()) as start_job:
                with self.assertRaises(main.HTTPException) as raised:
                    await main.update_system_localization(
                        main.SystemLocalizationUpdateRequest(locale="not-a-locale")
                    )

        self.assertEqual(raised.exception.status_code, 400)
        start_job.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
