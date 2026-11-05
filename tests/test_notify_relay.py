import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, notify_relay
from app.auth import API_TOKEN
from app.notify_relay import (
    NotifyRelay,
    merge_config_update,
    nina_notification_fields,
    normalize_config,
    parse_signalr_frames,
    public_config,
)

HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


def make_relay(tmpdir, **kwargs):
    return NotifyRelay(config_path=os.path.join(tmpdir, "notify.json"), **kwargs)


class ConfigTests(unittest.TestCase):
    def test_defaults_for_missing_or_invalid_input(self):
        self.assertEqual(normalize_config(None), notify_relay.DEFAULT_CONFIG)
        self.assertEqual(normalize_config("nope"), notify_relay.DEFAULT_CONFIG)

    def test_clamps_and_rejects_invalid_values(self):
        config = normalize_config({
            "enabled": "yes",
            "temperatureThresholdC": 500,
            "diskPath": "relative/path",
            "ntfy": {"url": "ftp://example.org/topic", "minSeverity": "loud", "token": "a\nb"},
        })
        self.assertTrue(config["enabled"])
        self.assertEqual(config["temperatureThresholdC"], 110.0)
        self.assertEqual(config["diskPath"], "/")
        self.assertEqual(config["ntfy"]["url"], "")
        self.assertEqual(config["ntfy"]["minSeverity"], "warning")
        self.assertEqual(config["ntfy"]["token"], "")

    def test_public_config_hides_token(self):
        config = normalize_config({"ntfy": {"url": "https://ntfy.sh/rig", "token": "secret"}})
        public = public_config(config)
        self.assertNotIn("token", public["ntfy"])
        self.assertTrue(public["ntfy"]["hasToken"])
        self.assertEqual(config["ntfy"]["token"], "secret")

    def test_partial_update_keeps_stored_token(self):
        current = normalize_config({"ntfy": {"url": "https://ntfy.sh/rig", "token": "secret"}})
        merged = merge_config_update(current, {"ntfy": {"enabled": True}})
        self.assertEqual(merged["ntfy"]["token"], "secret")
        self.assertTrue(merged["ntfy"]["enabled"])
        cleared = merge_config_update(merged, {"ntfy": {"token": ""}})
        self.assertEqual(cleared["ntfy"]["token"], "")

    def test_save_and_load_round_trip_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "notify.json")
            config = normalize_config({"includeSystem": False})
            notify_relay.save_config(config, path)
            self.assertEqual(notify_relay.load_config(path), config)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class SignalRParsingTests(unittest.TestCase):
    def test_splits_frames_on_record_separator(self):
        payload = '{}\x1e{"type":6}\x1e{"type":1,"target":"ReceiveNotification","arguments":[{}]}\x1e'
        self.assertEqual([f.get("type") for f in parse_signalr_frames(payload)], [None, 6, 1])

    def test_ignores_garbage(self):
        self.assertEqual(parse_signalr_frames("not json\x1e\x1e"), [])

    def test_maps_nina_notification_types(self):
        self.assertEqual(
            nina_notification_fields({"type": 3, "title": "Camera", "message": " Lost connection "}),
            {"severity": "error", "title": "Camera", "message": "Lost connection"},
        )
        self.assertEqual(nina_notification_fields({"Type": "Warning", "Message": "x"})["severity"], "warning")
        self.assertIsNone(nina_notification_fields({"type": 0, "message": ""}))
        self.assertIsNone(
            nina_notification_fields({"type": 0, "message": "Not a 10u mount. 10u utilities disabled."})
        )


class RelayBufferTests(unittest.TestCase):
    def test_sequence_numbers_and_since(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            relay = make_relay(tmpdir)
            for index in range(3):
                relay.publish("info", "nina", f"m{index}")
            self.assertEqual(relay.last_seq, 3)
            self.assertEqual([e.message for e in relay.events_since(1)], ["m1", "m2"])

    def test_buffer_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            relay = make_relay(tmpdir, buffer_size=2)
            for index in range(5):
                relay.publish("info", "nina", f"m{index}")
            self.assertEqual(relay.oldest_seq(), 4)
            self.assertEqual([e.seq for e in relay.events_since(0)], [4, 5])

    def test_disabled_relay_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            relay = make_relay(tmpdir)
            relay.config["enabled"] = False
            self.assertIsNone(relay.publish("error", "nina", "x"))
            self.assertEqual(relay.last_seq, 0)

    def test_hub_frames(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            relay = make_relay(tmpdir)
            self.assertEqual(relay.handle_hub_frame({"type": 6}), '{"type": 6}\x1e')
            relay.handle_hub_frame({
                "type": 1,
                "target": "ReceiveNotification",
                "arguments": [{"type": 2, "title": "Guider", "message": "Star lost"}],
            })
            event = relay.events_since(0)[0]
            self.assertEqual((event.source, event.severity, event.message), ("nina", "warning", "Star lost"))
            relay.config["includeNina"] = False
            relay.handle_hub_frame({"type": 1, "target": "ReceiveNotification", "arguments": [{"message": "x"}]})
            self.assertEqual(relay.last_seq, 1)


class NtfyTests(unittest.TestCase):
    def test_request_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            relay = make_relay(tmpdir)
            event = notify_relay.RelayEvent(1, "error", "nina", "Kamera ü", "Fehler", 0.0)
            request = relay.ntfy_request({"url": "https://ntfy.sh/rig", "token": "tk"}, event)
            self.assertEqual(request.full_url, "https://ntfy.sh/rig")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.data, "Fehler".encode())
            self.assertEqual(request.get_header("Priority"), "5")
            self.assertEqual(request.get_header("Authorization"), "Bearer tk")
            self.assertEqual(request.get_header("Title"), "Kamera ü")  # latin-1 safe

    def test_non_latin_title_is_rfc2047_encoded(self):
        self.assertTrue(notify_relay._rfc2047("望遠鏡").startswith("=?UTF-8?B?"))


class SystemCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_under_voltage_reported_once_and_on_recovery(self):
        state = {"underVoltage": True, "supplyVoltage": 4.71}

        async def power():
            return dict(state)

        with tempfile.TemporaryDirectory() as tmpdir:
            relay = make_relay(tmpdir, power_probe=power)
            relay.config["diskFreeThresholdGb"] = 0
            await relay.check_system_once()
            await relay.check_system_once()
            state["underVoltage"] = False
            await relay.check_system_once()
            codes = [e.code for e in relay.events_since(0)]
            self.assertEqual(codes, ["system.underVoltage", "system.underVoltageCleared"])
            self.assertIn("4.71 V", relay.events_since(0)[0].message)

    async def test_temperature_uses_hysteresis(self):
        readings = iter([82.0, 79.0, 83.0, 74.0, 81.0])

        async def temperature():
            return next(readings)

        with tempfile.TemporaryDirectory() as tmpdir:
            relay = make_relay(tmpdir, temperature_probe=temperature)
            relay.config["diskFreeThresholdGb"] = 0
            for _ in range(5):
                await relay.check_system_once()
            # 82 alerts; 79 and 83 stay inside the 5 °C band; 74 re-arms; 81 alerts again.
            self.assertEqual(len(relay.events_since(0)), 2)

    async def test_low_disk(self):
        usage = type("Usage", (), {"free": 1 * 1024 ** 3})
        with tempfile.TemporaryDirectory() as tmpdir:
            relay = make_relay(tmpdir)
            with patch.object(notify_relay.shutil, "disk_usage", return_value=usage):
                await relay.check_system_once()
                await relay.check_system_once()
            self.assertEqual([e.code for e in relay.events_since(0)], ["system.lowDisk"])

    async def test_failing_probes_are_ignored(self):
        async def broken():
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmpdir:
            relay = make_relay(tmpdir, power_probe=broken, temperature_probe=broken)
            relay.config["diskFreeThresholdGb"] = 0
            await relay.check_system_once()
            self.assertEqual(relay.last_seq, 0)


class HubListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_relays_notifications_from_a_signalr_hub(self):
        from websockets.asyncio.server import serve

        received_handshake = asyncio.get_running_loop().create_future()

        async def hub(ws):
            handshake = await ws.recv()
            received_handshake.set_result(handshake)
            await ws.send("{}\x1e")
            await ws.send(
                json.dumps({
                    "type": 1,
                    "target": "ReceiveNotification",
                    "arguments": [{"type": 3, "title": "Mount", "message": "Park failed"}],
                }) + "\x1e"
            )
            await ws.send('{"type":6}\x1e')
            self.assertEqual(await ws.recv(), '{"type": 6}\x1e')
            await asyncio.sleep(1)

        async with serve(hub, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as tmpdir:
                relay = make_relay(tmpdir, hub_url=f"ws://127.0.0.1:{port}/hubs/notifications")
                relay.config["includeSystem"] = False
                relay.start()
                try:
                    self.assertEqual(json.loads((await received_handshake).rstrip("\x1e")),
                                     {"protocol": "json", "version": 1})
                    for _ in range(50):
                        if relay.last_seq:
                            break
                        await asyncio.sleep(0.02)
                    self.assertTrue(relay.hub_connected)
                    event = relay.events_since(0)[0]
                    self.assertEqual((event.severity, event.title, event.message),
                                     ("error", "Mount", "Park failed"))
                finally:
                    await relay.stop()


class NotificationApiTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.relay = make_relay(self.tmpdir.name)
        self.patch = patch.object(main, "notify_relay", self.relay)
        self.patch.start()
        self.client = TestClient(main.app)  # No lifespan: the relay tasks are not started.

    def tearDown(self):
        self.patch.stop()
        self.tmpdir.cleanup()

    def test_requires_token(self):
        self.assertIn(self.client.get("/notifications").status_code, (401, 403))

    def test_lists_events_since(self):
        self.relay.publish("warning", "nina", "one")
        self.relay.publish("error", "nina", "two")
        body = self.client.get("/notifications?since=1", headers=HEADERS).json()
        self.assertEqual(body["bootId"], self.relay.boot_id)
        self.assertEqual(body["lastSeq"], 2)
        self.assertFalse(body["truncated"])
        self.assertEqual([e["message"] for e in body["events"]], ["two"])

    def test_reports_truncation(self):
        relay = make_relay(self.tmpdir.name, buffer_size=2)
        for index in range(4):
            relay.publish("info", "nina", str(index))
        with patch.object(main, "notify_relay", relay):
            body = self.client.get("/notifications?since=1", headers=HEADERS).json()
        self.assertTrue(body["truncated"])

    def test_settings_round_trip_never_returns_token(self):
        response = self.client.put(
            "/notifications/settings",
            headers=HEADERS,
            json={"includeSystem": False, "ntfy": {"enabled": True, "url": "https://ntfy.sh/rig", "token": "tk"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["includeSystem"])
        self.assertTrue(body["ntfy"]["hasToken"])
        self.assertNotIn("token", body["ntfy"])
        stored = notify_relay.load_config(self.relay.config_path)
        self.assertEqual(stored["ntfy"]["token"], "tk")
        self.assertEqual(self.client.get("/notifications/settings", headers=HEADERS).json(), body)

    def test_settings_validation(self):
        bad_url = self.client.put("/notifications/settings", headers=HEADERS,
                                  json={"ntfy": {"url": "ntfy.sh/rig"}})
        self.assertEqual(bad_url.status_code, 422)
        bad_temp = self.client.put("/notifications/settings", headers=HEADERS,
                                   json={"temperatureThresholdC": 5})
        self.assertEqual(bad_temp.status_code, 422)

    def test_test_notification(self):
        response = self.client.post("/notifications/test", headers=HEADERS, json={"severity": "warning"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["severity"], "warning")
        self.assertEqual(self.relay.last_seq, 1)

    def test_stream_rejects_bad_token(self):
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/notifications/stream?token=wrong") as ws:
                ws.receive_json()

    def test_stream_replays_then_streams_live(self):
        self.relay.publish("info", "nina", "old")
        self.relay.publish("error", "nina", "missed")
        with self.client.websocket_connect(
            f"/notifications/stream?token={API_TOKEN}&since=1&bootId={self.relay.boot_id}"
        ) as ws:
            hello = ws.receive_json()
            self.assertEqual((hello["kind"], hello["lastSeq"]), ("hello", 2))
            replay = ws.receive_json()
            self.assertEqual(replay["event"]["message"], "missed")
            self.client.post("/notifications/test", headers=HEADERS, json={"message": "live"})
            live = ws.receive_json()
            self.assertEqual((live["kind"], live["event"]["seq"], live["event"]["message"]),
                             ("event", 3, "live"))

    def test_stream_replays_everything_after_daemon_restart(self):
        self.relay.publish("info", "nina", "first")
        with self.client.websocket_connect(
            f"/notifications/stream?token={API_TOKEN}&since=99&bootId=stale"
        ) as ws:
            ws.receive_json()
            self.assertEqual(ws.receive_json()["event"]["message"], "first")


if __name__ == "__main__":
    unittest.main()
