import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

from app import main
from app import wifi_config


class WifiConfigModeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "wifi_config.json"
        self.config_patch = patch.object(wifi_config, "CONFIG_FILE", str(self.config_path))
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.temp_dir.cleanup()

    def test_legacy_config_defaults_to_auto_mode(self):
        self.config_path.write_text(
            json.dumps({"ssid": "Home", "auto_connect": True}),
            encoding="utf-8",
        )

        config = wifi_config.load_wifi_config()

        self.assertEqual(config["desired_mode"], wifi_config.NETWORK_MODE_AUTO)

    def test_saving_mode_preserves_existing_wifi_configuration(self):
        wifi_config.save_wifi_config(
            "Home",
            True,
            "5GHz",
            client_interface="wlan0",
            hotspot_interface="wlan1",
        )

        saved = wifi_config.save_network_mode(wifi_config.NETWORK_MODE_HOTSPOT)
        reloaded = wifi_config.load_wifi_config()

        self.assertEqual(saved, reloaded)
        self.assertEqual(reloaded["desired_mode"], wifi_config.NETWORK_MODE_HOTSPOT)
        self.assertEqual(reloaded["ssid"], "Home")
        self.assertTrue(reloaded["auto_connect"])
        self.assertEqual(reloaded["client_interface"], "wlan0")
        self.assertEqual(reloaded["hotspot_interface"], "wlan1")

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            wifi_config.save_network_mode("client-only")


class WifiModeHelpersTests(unittest.TestCase):
    def test_observed_mode_distinguishes_all_runtime_states(self):
        self.assertEqual(main._observed_network_mode([]), "disconnected")
        self.assertEqual(
            main._observed_network_mode([{"role": "client"}]),
            "client",
        )
        self.assertEqual(
            main._observed_network_mode([{"role": "hotspot"}]),
            "hotspot",
        )
        self.assertEqual(
            main._observed_network_mode([{"role": "client"}, {"role": "hotspot"}]),
            "dual",
        )

    def test_configured_rig_id_is_sanitized(self):
        with patch.dict("os.environ", {"PINS_RIG_ID": "My Field Rig!"}):
            self.assertEqual(main._get_rig_id(), "my-field-rig")

    def test_persisted_rig_name_is_the_api_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            rig_name_file = Path(temp_dir) / "rig-name"
            rig_name_file.write_text("pins-ce29c\n", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {
                    "PINS_RIG_ID": "",
                    "PINS_RIG_NAME_FILE": str(rig_name_file),
                },
            ):
                self.assertEqual(main._get_rig_id(), "pins-ce29c")


class WifiModeApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_identity_contract_is_unauthenticated(self):
        with patch.dict("os.environ", {"PINS_RIG_ID": "pins-peer-test"}):
            response = await main.health()

        self.assertEqual(
            response.model_dump(),
            {
                "status": "ok",
                "service": "pinsdaemon",
                "rigId": "pins-peer-test",
                "apiVersion": 2,
            },
        )
        route = next(route for route in main.app.routes if route.path == "/health")
        self.assertEqual(route.dependant.dependencies, [])

    async def test_set_hotspot_mode_persists_intent_before_starting_job(self):
        fake_job = SimpleNamespace(
            id="job-1",
            status=main.JobStatus.STARTED,
            exit_code=None,
            created_at=1.0,
            finished_at=None,
            command="wifi-connect --hotspot",
        )

        with patch.object(main, "save_network_mode") as save_mode:
            with patch.object(
                main,
                "_start_network_mode_job",
                new=AsyncMock(return_value=fake_job),
            ) as start_job:
                result = await main.set_wifi_mode(
                    main.WifiModeRequest(desiredMode="hotspot")
                )

        save_mode.assert_called_once_with("hotspot")
        start_job.assert_awaited_once_with("hotspot")
        self.assertEqual(result.desiredMode, "hotspot")
        self.assertEqual(result.job.jobId, "job-1")

    async def test_invalid_mode_does_not_persist_or_start_job(self):
        with patch.object(main, "save_network_mode") as save_mode:
            with patch.object(main, "_start_network_mode_job", new=AsyncMock()) as start_job:
                with self.assertRaises(main.HTTPException) as raised:
                    await main.set_wifi_mode(
                        main.WifiModeRequest(desiredMode="client-only")
                    )

        self.assertEqual(raised.exception.status_code, 400)
        save_mode.assert_not_called()
        start_job.assert_not_awaited()


class WifiInterfaceFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "wifi_config.json"
        self.config_patch = patch.object(wifi_config, "CONFIG_FILE", str(self.config_path))
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.temp_dir.cleanup()

    async def test_removed_usb_adapter_collapses_both_roles_to_internal_wifi(self):
        wifi_config.save_wifi_config(
            None,
            False,
            client_interface="wlan0",
            hotspot_interface="wlan1",
        )
        adapters = [SimpleNamespace(interface="wlan0")]

        with patch.object(main, "_list_wifi_adapters", new=AsyncMock(return_value=adapters)):
            interfaces = await main._resolve_wifi_interfaces()

        self.assertEqual(interfaces, ("wlan0", "wlan0"))

    async def test_connect_persists_only_resolved_roles_before_background_job_succeeds(self):
        wifi_config.save_wifi_config(
            None,
            False,
            client_interface="wlan0",
            hotspot_interface="wlan1",
            desired_mode=wifi_config.NETWORK_MODE_HOTSPOT,
        )
        adapters = [SimpleNamespace(interface="wlan0")]
        fake_job = SimpleNamespace(
            id="job-1",
            status=main.JobStatus.STARTED,
            exit_code=None,
            created_at=1.0,
            finished_at=None,
            command="wifi-connect",
        )

        with patch.object(main, "_list_wifi_adapters", new=AsyncMock(return_value=adapters)):
            with patch.object(main.job_manager, "start_job", new=AsyncMock(return_value="job-1")) as start_job:
                with patch.object(main.job_manager, "get_job", return_value=fake_job):
                    result = await main.connect_wifi(
                        main.WifiConnectRequest(
                            ssid="Home",
                            password="secret",
                            band="5GHz",
                            client_interface="wlan0",
                            hotspot_interface=None,
                        )
                    )

        command = start_job.await_args.args[0]
        self.assertIn("--client-iface", command)
        self.assertEqual(command[command.index("--client-iface") + 1], "wlan0")
        self.assertEqual(command[command.index("--hotspot-iface") + 1], "wlan0")
        self.assertIn("--password-stdin", command)
        self.assertEqual(command[command.index("--band") + 1], "a")
        self.assertNotIn("secret", command)
        self.assertEqual(start_job.await_args.kwargs["stdin_data"], b"secret\n")
        self.assertEqual(result.jobId, "job-1")
        config = wifi_config.load_wifi_config()
        self.assertEqual(config["ssid"], None)
        self.assertFalse(config["auto_connect"])
        self.assertEqual(config["hotspot_interface"], "wlan0")
        self.assertEqual(config["desired_mode"], wifi_config.NETWORK_MODE_HOTSPOT)

    async def test_optional_second_adapter_is_used_for_hotspot_by_default(self):
        wifi_config.save_wifi_config(
            None,
            False,
            client_interface="wlan0",
            hotspot_interface="wlan0",
        )
        adapters = [SimpleNamespace(interface="wlan0"), SimpleNamespace(interface="wlan1")]

        with patch.object(main, "_list_wifi_adapters", new=AsyncMock(return_value=adapters)):
            interfaces = await main._resolve_wifi_interfaces()

        self.assertEqual(interfaces, ("wlan0", "wlan1"))

    async def test_explicit_unknown_adapter_is_still_rejected(self):
        adapters = [SimpleNamespace(interface="wlan0")]
        with patch.object(main, "_list_wifi_adapters", new=AsyncMock(return_value=adapters)):
            with self.assertRaises(main.HTTPException) as raised:
                await main._resolve_wifi_interfaces(requested_hotspot="wlan9")

        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
