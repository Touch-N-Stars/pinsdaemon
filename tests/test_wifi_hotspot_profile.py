"""Exercise AP profile construction without touching host networking."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wifi-connect.sh"


@unittest.skipIf(os.name == "nt", "Requires Linux bash and /proc UUID source")
class HotspotProfileTests(unittest.TestCase):
    def build_profile(self, band="", channel="", add_fails=False):
        source = SCRIPT.read_text()
        function = source[source.index("enable_hotspot() {"):].split("\n}\n", 1)[0] + "\n}\n"
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / "calls"
            harness = r'''
PARALLEL_WIFI_MODE=false
HOTSPOT_IFACE=wlan0
HOTSPOT_IPV4_CIDR=10.42.0.1/24
HOTSPOT_IPV4_ADDRESS=10.42.0.1
HOTSPOT_AUTOCONNECT_PRIORITY=0
RIG_NAME_COMMAND=/nonexistent
ensure_local_only_hotspot_dhcp() { :; }
list_hotspot_profile_uuids() { :; }
get_hotspot_password() { echo test-password; }
get_hotspot_band() { printf '%s' "$TEST_BAND"; }
get_hotspot_channel() { printf '%s' "$TEST_CHANNEL"; }
hotspot_postcondition_met() { return 0; }
iw() { :; }
nmcli() {
    if [ "$1 $2" = "connection add" ]; then
        printf '%s\0' "$@" > "$CALLS"
        return "$ADD_FAILS"
    fi
    return 0
}
'''
            result = subprocess.run(
                ["bash", "-c", harness + function + "\nenable_hotspot\n"],
                env={**os.environ, "TEST_BAND": band, "TEST_CHANNEL": channel,
                     "CALLS": str(calls), "ADD_FAILS": str(int(add_fails))},
                capture_output=True, text=True, timeout=10,
            )
            args = calls.read_bytes().decode().split("\0")[:-1]
        return result, args

    def test_default_is_channel_six_and_wpa2_ccmp_before_activation(self):
        result, args = self.build_profile()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for key, value in (("802-11-wireless.band", "bg"), ("802-11-wireless.channel", "6"),
                           ("wifi-sec.proto", "rsn"), ("wifi-sec.pairwise", "ccmp"),
                           ("wifi-sec.group", "ccmp"), ("wifi-sec.pmf", "disable"),
                           ("wifi-sec.psk", "test-password")):
            self.assertEqual(args[args.index(key) + 1], value)

    def test_explicit_band_and_channel_are_preserved(self):
        for band, channel in (("a", "36"), ("bg", "11"), ("", "13")):
            with self.subTest(band=band, channel=channel):
                result, args = self.build_profile(band, channel)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(args[args.index("802-11-wireless.channel") + 1], channel)
                if band:
                    self.assertEqual(args[args.index("802-11-wireless.band") + 1], band)
                else:
                    self.assertNotIn("802-11-wireless.band", args)

    def test_five_ghz_without_channel_does_not_inherit_channel_six(self):
        result, args = self.build_profile("a")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("802-11-wireless.channel", args)

    def test_profile_creation_failure_is_reported(self):
        result, _ = self.build_profile(add_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Hotspot enabled successfully", result.stdout)
