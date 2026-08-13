import importlib.util
import ipaddress
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pins-wifi-viability.py"
SPEC = importlib.util.spec_from_file_location("pins_wifi_viability", SCRIPT)
viability = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = viability
SPEC.loader.exec_module(viability)


class FakeProbe:
    def __init__(
        self,
        *,
        snapshot=True,
        gateway=None,
        gateway_reachable=True,
        dhcp_server="10.42.0.1",
        peer_rig_id="pins-host",
        local_addresses=(),
    ):
        self.snapshot = (
            viability.ClientSnapshot(
                profile_uuid="client-profile",
                activation_timestamp="1234",
                addresses=(ipaddress.IPv4Interface("10.42.0.38/24"),),
            )
            if snapshot
            else None
        )
        self.gateway = ipaddress.IPv4Address(gateway) if gateway else None
        self.gateway_reachable = gateway_reachable
        self.server = ipaddress.IPv4Address(dhcp_server) if dhcp_server else None
        self.peer_rig_id = peer_rig_id
        self.local_addresses = {
            ipaddress.IPv4Address(address) for address in local_addresses
        }
        self.gateway_pings = 0
        self.health_requests = 0

    def client_snapshot(self, _interface):
        return self.snapshot

    def default_gateway(self, _interface):
        return self.gateway

    def ping_gateway(self, _interface, _gateway, _timeout):
        self.gateway_pings += 1
        return self.gateway_reachable

    def dhcp_server(self, _interface):
        return self.server

    def local_ipv4_addresses(self):
        return self.local_addresses

    def peer_health(self, _server, _interface, _timeout):
        self.health_requests += 1
        return self.peer_rig_id


class ScriptedSystemProbe(viability.SystemProbe):
    def __init__(
        self,
        *,
        mode="infrastructure",
        state="100 (connected)",
        device_uuid="client-profile",
        addresses=("10.42.0.38/24",),
        mode_query_ok=True,
        timestamp=("1234",),
    ):
        self.values = {
            "802-11-wireless.mode": [mode] if mode else [],
            "GENERAL.STATE": [state] if state else [],
            "GENERAL.CON-UUID": [device_uuid] if device_uuid else [],
            "IP4.ADDRESS": list(addresses),
            "connection.timestamp": list(timestamp),
        }
        self.mode_query_ok = mode_query_ok

    def _run(self, command, timeout=10):
        del timeout
        if command[:2] == ["nmcli", "-t"]:
            stdout = "client-profile:802-11-wireless:wlan0\n"
            return viability.subprocess.CompletedProcess(command, 0, stdout, "")
        if command[:2] == ["nmcli", "-g"]:
            if command[2] == "802-11-wireless.mode" and not self.mode_query_ok:
                return viability.subprocess.CompletedProcess(command, 10, "", "failed")
            stdout = "\n".join(self.values.get(command[2], []))
            if stdout:
                stdout += "\n"
            return viability.subprocess.CompletedProcess(command, 0, stdout, "")
        return viability.subprocess.CompletedProcess(command, 1, "", "unsupported")


class WifiViabilityTests(unittest.TestCase):
    def evaluate(self, probe, state, local_rig):
        return viability.validate_once(
            probe,
            interface="wlan0",
            peer_state_file=state,
            local_rig_name_file=local_rig,
            ping_timeout=2,
            health_timeout=1.0,
            probe_gateway=True,
        )

    def test_home_wifi_keeps_existing_gateway_probe_and_never_uses_peer_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "peer.json"
            local = Path(directory) / "rig-name"
            probe = FakeProbe(
                gateway="192.168.1.1",
                gateway_reachable=True,
                dhcp_server="192.168.1.1",
            )

            result = self.evaluate(probe, state, local)

        self.assertTrue(result.healthy)
        self.assertEqual(result.mode, "gateway")
        self.assertEqual(probe.gateway_pings, 1)
        self.assertEqual(probe.health_requests, 0)

    def test_gateway_path_does_not_add_networkmanager_profile_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = FakeProbe(
                snapshot=False,
                gateway="192.168.1.1",
                gateway_reachable=True,
            )
            result = self.evaluate(
                probe,
                Path(directory) / "peer.json",
                Path(directory) / "rig-name",
            )

        self.assertTrue(result.healthy)
        self.assertEqual(result.mode, "gateway")
        self.assertEqual(probe.gateway_pings, 1)
        self.assertEqual(probe.health_requests, 0)

    def test_connection_commit_accepts_gateway_without_changing_initial_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = FakeProbe(
                snapshot=False,
                gateway="192.168.1.1",
                gateway_reachable=False,
            )
            result = viability.validate_once(
                probe,
                interface="wlan0",
                peer_state_file=Path(directory) / "peer.json",
                local_rig_name_file=Path(directory) / "rig-name",
                ping_timeout=2,
                health_timeout=1.0,
                probe_gateway=False,
            )

        self.assertTrue(result.healthy)
        self.assertEqual(result.reason, "gateway_present")
        self.assertEqual(probe.gateway_pings, 0)
        self.assertEqual(probe.health_requests, 0)

    def test_unreachable_advertised_gateway_is_not_reclassified_as_pins_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "peer.json"
            state.write_text('{"rig_id":"stale"}', encoding="utf-8")
            probe = FakeProbe(gateway="192.168.1.1", gateway_reachable=False)

            result = self.evaluate(probe, state, Path(directory) / "rig-name")

            self.assertFalse(state.exists())
        self.assertFalse(result.healthy)
        self.assertEqual(result.mode, "gateway")
        self.assertEqual(result.reason, "gateway_unreachable")
        self.assertEqual(probe.health_requests, 0)

    def test_failed_route_probe_cannot_fall_through_to_peer_validation(self):
        class FailedRouteProbe(FakeProbe):
            def default_gateway(self, _interface):
                raise viability.ProbeFailure("failed")

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "peer.json"
            state.write_text('{"confirmed":true}', encoding="utf-8")
            probe = FailedRouteProbe()
            result = self.evaluate(probe, state, Path(directory) / "rig-name")

            self.assertFalse(state.exists())
        self.assertFalse(result.healthy)
        self.assertEqual(result.reason, "default_route_probe_failed")
        self.assertEqual(probe.health_requests, 0)

    def test_local_only_pins_peer_requires_same_identity_across_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "peer.json"
            local = Path(directory) / "rig-name"
            local.write_text("pins-client\n", encoding="utf-8")
            probe = FakeProbe(peer_rig_id="pins-host")

            first = self.evaluate(probe, state, local)
            second = self.evaluate(probe, state, local)
            persisted = json.loads(state.read_text(encoding="utf-8"))

        self.assertTrue(first.pending)
        self.assertEqual(first.reason, "peer_identity_pending")
        self.assertTrue(second.healthy)
        self.assertEqual(second.mode, "pins-peer")
        self.assertEqual(second.rig_id, "pins-host")
        self.assertTrue(persisted["confirmed"])
        self.assertEqual(probe.health_requests, 2)

    def test_changed_peer_identity_needs_fresh_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "peer.json"
            local = Path(directory) / "rig-name"
            probe = FakeProbe(peer_rig_id="pins-one")
            self.evaluate(probe, state, local)
            self.assertTrue(self.evaluate(probe, state, local).healthy)

            probe.peer_rig_id = "pins-two"
            changed = self.evaluate(probe, state, local)
            confirmed = self.evaluate(probe, state, local)

        self.assertTrue(changed.pending)
        self.assertTrue(confirmed.healthy)
        self.assertEqual(confirmed.rig_id, "pins-two")

    def test_stale_matching_identity_does_not_skip_a_fresh_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "peer.json"
            state.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "interface": "wlan0",
                        "profile_uuid": "client-profile",
                        "activation_timestamp": "1234",
                        "dhcp_server": "10.42.0.1",
                        "rig_id": "pins-host",
                        "confirmed": True,
                        "observed_at": 1,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(viability.time, "time", return_value=100):
                result = self.evaluate(
                    FakeProbe(peer_rig_id="pins-host"),
                    state,
                    Path(directory) / "rig-name",
                )

        self.assertTrue(result.pending)
        self.assertEqual(result.reason, "peer_identity_pending")

    def test_missing_active_infrastructure_client_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "peer.json"
            probe = FakeProbe(snapshot=False)
            result = self.evaluate(probe, state, Path(directory) / "rig-name")

        self.assertFalse(result.healthy)
        self.assertEqual(result.reason, "active_infrastructure_client_required")
        self.assertEqual(probe.health_requests, 0)

    def test_dhcp_server_must_be_known_and_on_link(self):
        for server, reason in (
            (None, "dhcp_server_unknown"),
            ("192.168.50.1", "dhcp_server_not_on_link"),
            ("10.42.0.38", "dhcp_server_is_local_address:10.42.0.38"),
        ):
            with self.subTest(server=server), tempfile.TemporaryDirectory() as directory:
                probe = FakeProbe(dhcp_server=server)
                result = self.evaluate(
                    probe,
                    Path(directory) / "peer.json",
                    Path(directory) / "rig-name",
                )
                self.assertFalse(result.healthy)
                self.assertEqual(result.reason, reason)
                self.assertEqual(probe.health_requests, 0)

    def test_peer_health_loss_clears_identity_and_allows_normal_fallback_counting(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "peer.json"
            local = Path(directory) / "rig-name"
            probe = FakeProbe(peer_rig_id="pins-host")
            self.evaluate(probe, state, local)
            self.assertTrue(self.evaluate(probe, state, local).healthy)

            probe.peer_rig_id = None
            failed = self.evaluate(probe, state, local)

            self.assertFalse(state.exists())
        self.assertFalse(failed.healthy)
        self.assertEqual(failed.reason, "pins_health_unavailable")

    def test_own_rig_identity_is_never_accepted_as_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "rig-name"
            local.write_text("pins-same\n", encoding="utf-8")
            probe = FakeProbe(peer_rig_id="pins-same")
            result = self.evaluate(probe, Path(directory) / "peer.json", local)

        self.assertFalse(result.healthy)
        self.assertEqual(result.reason, "peer_identity_matches_local_rig")

    def test_active_local_hotspot_address_is_never_probed_as_a_remote_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = FakeProbe(local_addresses=("10.42.0.1",))
            result = self.evaluate(
                probe,
                Path(directory) / "peer.json",
                Path(directory) / "rig-name",
            )

        self.assertFalse(result.healthy)
        self.assertEqual(result.reason, "dhcp_server_is_local_address:10.42.0.1")
        self.assertEqual(probe.health_requests, 0)

    def test_failed_local_address_inventory_cannot_probe_a_possible_own_ap(self):
        class FailedAddressProbe(FakeProbe):
            def local_ipv4_addresses(self):
                raise viability.ProbeFailure("failed")

        with tempfile.TemporaryDirectory() as directory:
            probe = FailedAddressProbe()
            result = self.evaluate(
                probe,
                Path(directory) / "peer.json",
                Path(directory) / "rig-name",
            )

        self.assertFalse(result.healthy)
        self.assertEqual(result.reason, "local_address_probe_failed")
        self.assertEqual(probe.health_requests, 0)

    def test_health_payload_requires_exact_pinsdaemon_identity_contract(self):
        valid = {
            "status": "ok",
            "service": "pinsdaemon",
            "rigId": "pins-host",
            "apiVersion": 2,
        }
        self.assertEqual(viability._valid_health_identity(valid), "pins-host")
        for update in (
            {"status": "error"},
            {"service": "other"},
            {"rigId": "bad identity"},
            {"apiVersion": 1},
            {"apiVersion": True},
        ):
            payload = dict(valid)
            payload.update(update)
            self.assertIsNone(viability._valid_health_identity(payload))

    def test_dhcp_server_parser_accepts_networkmanager_option_format(self):
        server = viability._parse_dhcp_server(
            ["dhcp_lease_time = 3600", "dhcp_server_identifier = 10.42.0.1"]
        )
        self.assertEqual(str(server), "10.42.0.1")

    def test_system_probe_requires_infrastructure_connected_uuid_and_ipv4(self):
        valid = ScriptedSystemProbe().client_snapshot("wlan0")
        self.assertIsNotNone(valid)
        self.assertEqual(valid.profile_uuid, "client-profile")
        self.assertIsNotNone(
            ScriptedSystemProbe(mode="").client_snapshot("wlan0"),
            "NetworkManager defines blank mode as infrastructure",
        )

        for probe in (
            ScriptedSystemProbe(mode="ap"),
            ScriptedSystemProbe(mode="", mode_query_ok=False),
            ScriptedSystemProbe(state="30 (disconnected)"),
            ScriptedSystemProbe(device_uuid="different-profile"),
            ScriptedSystemProbe(addresses=()),
            ScriptedSystemProbe(timestamp=()),
            ScriptedSystemProbe(timestamp=("unknown",)),
            ScriptedSystemProbe(timestamp=("0",)),
        ):
            with self.subTest(values=probe.values):
                self.assertIsNone(probe.client_snapshot("wlan0"))


if __name__ == "__main__":
    unittest.main()
