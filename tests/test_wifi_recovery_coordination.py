from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = REPO_ROOT / "scripts" / "pins-wifi-watchdog.sh"
DISPATCHER = REPO_ROOT / "scripts" / "90-pins-wifi-recovery"
WIFI_CONNECT = REPO_ROOT / "scripts" / "wifi-connect.sh"


class WifiRecoveryCoordinationTests(unittest.TestCase):
    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_all_network_mutation_paths_use_the_same_flock(self):
        scripts = {
            path.name: self.read(path)
            for path in (WATCHDOG, DISPATCHER, WIFI_CONNECT)
        }

        lock_paths = {}
        for name, source in scripts.items():
            match = re.search(
                r'^COORDINATION_LOCK_FILE="([^"]+)"$', source, re.MULTILINE
            )
            self.assertIsNotNone(match, f"{name} must declare the shared lock")
            lock_paths[name] = match.group(1)
            self.assertIn("flock ", source, f"{name} must acquire the shared lock")

        self.assertEqual(
            set(lock_paths.values()),
            {"/run/pins-wifi-coordination.lock"},
        )

    def test_recovery_callers_keep_lock_held_during_wifi_connect(self):
        for path in (WATCHDOG, DISPATCHER):
            source = self.read(path)
            calls = [
                line
                for line in source.splitlines()
                if '"$WIFI_CONNECT_SCRIPT" --hotspot' in line
            ]
            self.assertTrue(calls, f"{path.name} must invoke hotspot fallback")
            for call in calls:
                self.assertIn("PINS_WIFI_COORDINATION_LOCK_HELD=1", call)

        wifi_connect = self.read(WIFI_CONNECT)
        self.assertIn('exec 9>"$COORDINATION_LOCK_FILE"', wifi_connect)
        self.assertIn(
            'flock -w "$COORDINATION_LOCK_WAIT_SECONDS" 9', wifi_connect
        )

    def test_watchdog_logs_outcomes_and_retries_failed_hotspot_immediately(self):
        source = self.read(WATCHDOG)
        self.assertIn("Gateway connectivity restored", source)
        self.assertIn("Fallback hotspot enabled successfully", source)
        self.assertIn("Failed to enable fallback hotspot", source)
        self.assertIn('set_failures "$MAX_FAILURES"', source)
        self.assertIn('DESIRED_MODE="${IFACES[2]:-auto}"', source)
        self.assertIn('if [[ "$DESIRED_MODE" == "hotspot" ]]', source)

    def test_dispatcher_logs_outcomes_and_targets_configured_interface(self):
        source = self.read(DISPATCHER)
        self.assertIn("Wi-Fi client connectivity restored", source)
        self.assertIn("Fallback hotspot enabled successfully", source)
        self.assertIn("Failed to enable fallback hotspot", source)
        self.assertIn(
            'nmcli connection up "$TARGET_SSID" ifname "$CLIENT_IFACE"',
            source,
        )
        self.assertIn('DESIRED_MODE="${IFACES[2]:-auto}"', source)
        self.assertIn('if [[ "$DESIRED_MODE" == "hotspot" ]]', source)

    def test_hotspot_has_fixed_address_and_activation_postcondition(self):
        source = self.read(WIFI_CONNECT)
        self.assertIn("10.42.0.1/24", source)
        self.assertIn("hotspot_postcondition_met", source)
        self.assertIn('ipv4.method shared ipv4.addresses "$HOTSPOT_IPV4_CIDR"', source)
        self.assertIn("Hotspot activation did not reach the required AP/IP postcondition", source)

    def test_package_installs_persistent_journal_configuration(self):
        workflow = self.read(REPO_ROOT / ".github" / "workflows" / "build-deb.yml")
        postinst = self.read(REPO_ROOT / "packaging" / "DEBIAN" / "postinst")
        config = self.read(REPO_ROOT / "packaging" / "journald-persistent.conf")
        control = self.read(REPO_ROOT / "packaging" / "DEBIAN" / "control")
        avahi_service = self.read(REPO_ROOT / "packaging" / "pinsdaemon.service")

        self.assertEqual(config.strip(), "[Journal]\nStorage=persistent")
        self.assertIn(
            "build/etc/systemd/journald.conf.d/90-pins-persistent.conf",
            workflow,
        )
        self.assertIn("mkdir -p /var/log/journal", postinst)
        self.assertIn("journalctl --flush", postinst)
        self.assertRegex(
            control,
            re.compile(r"^Depends:.*\butil-linux\b", re.MULTILINE),
        )
        self.assertRegex(
            control,
            re.compile(r"^Depends:.*\bavahi-daemon\b", re.MULTILINE),
        )
        self.assertIn("<type>_pinsdaemon._tcp</type>", avahi_service)
        self.assertIn("<port>8000</port>", avahi_service)
        self.assertIn("<name replace-wildcards=\"yes\">%h</name>", avahi_service)
        self.assertIn("<txt-record>backendPort=5000</txt-record>", avahi_service)

    def test_hotspot_and_mdns_share_the_persisted_rig_name(self):
        workflow = self.read(REPO_ROOT / ".github" / "workflows" / "build-deb.yml")
        postinst = self.read(REPO_ROOT / "packaging" / "DEBIAN" / "postinst")
        wifi_connect = self.read(WIFI_CONNECT)
        rig_name = self.read(REPO_ROOT / "scripts" / "pins-rig-name")

        self.assertIn("cp scripts/pins-rig-name build/usr/local/bin/", workflow)
        self.assertIn("bash -n scripts/*.sh scripts/pins-rig-name", workflow)
        self.assertIn('HOTSPOT_SSID="$("$RIG_NAME_COMMAND")"', wifi_connect)
        self.assertIn("/usr/local/bin/pins-rig-name --ensure", postinst)
        self.assertIn('hostnamectl set-hostname "$PINS_RIG_NAME"', postinst)
        self.assertIn("/etc/pins/rig-name", rig_name)

    def test_legacy_private_directory_locks_are_removed(self):
        watchdog = self.read(WATCHDOG)
        dispatcher = self.read(DISPATCHER)
        wifi_connect = self.read(WIFI_CONNECT)
        self.assertNotIn("pins-wifi-watchdog.lock", watchdog)
        self.assertNotIn("pins-wifi-connect.lock", watchdog)
        self.assertNotIn("pins-wifi-connect.lock", dispatcher)
        self.assertNotIn("pins-wifi-connect.lock", wifi_connect)
        self.assertNotIn('mkdir "$LOCK_DIR"', watchdog)
        self.assertNotIn('mkdir "$LOCK_DIR"', dispatcher)


if __name__ == "__main__":
    unittest.main()
