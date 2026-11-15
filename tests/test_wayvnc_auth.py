import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ensure-wayvnc-auth.sh"

HARDENED_CONTROL = "await self.send_command('client-disconnect', {'id': client['id']})\n"
LEGACY_CONTROL = "await self.send_command('attach', {'display': display})\n"
NOAUTH_CONFIG = "address=0.0.0.0\nport=5901\nenable_auth=false\n"


@unittest.skipIf(os.name == "nt", "the helper is validated on Linux")
class EnsureWayvncAuthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = self.root / "config"
        self.control = self.root / "wayvnc-control.py"
        self.rsa_key = self.root / "rsa_key.pem"

    def tearDown(self):
        self._tmp.cleanup()

    def run_helper(self) -> subprocess.CompletedProcess[str]:
        env = dict(
            os.environ,
            WAYVNC_CONFIG=str(self.config),
            WAYVNC_CONTROL_SCRIPT=str(self.control),
            WAYVNC_RSA_KEY=str(self.rsa_key),
            WAYVNC_RESTART="0",
        )
        return subprocess.run(
            ["bash", str(SCRIPT)], check=True, capture_output=True, text=True, env=env
        )

    def test_legacy_wayvnc_keeps_noauth_config(self):
        self.config.write_text(NOAUTH_CONFIG)
        self.control.write_text(LEGACY_CONTROL)

        result = self.run_helper()

        self.assertEqual(self.config.read_text(), NOAUTH_CONFIG)
        self.assertIn("leaving config unchanged", result.stdout)

    def test_hardened_wayvnc_enables_pam_auth_without_tls(self):
        self.config.write_text(NOAUTH_CONFIG)
        self.control.write_text(HARDENED_CONTROL)
        self.rsa_key.write_text("key")

        self.run_helper()

        lines = self.config.read_text().splitlines()
        self.assertIn("address=0.0.0.0", lines)
        self.assertIn("port=5901", lines)
        self.assertIn("enable_auth=true", lines)
        self.assertIn("enable_pam=true", lines)
        self.assertIn("relax_encryption=true", lines)
        self.assertIn(f"rsa_private_key_file={self.rsa_key}", lines)
        # noVNC cannot negotiate VeNCrypt X509 (TLS); it must not be offered.
        self.assertFalse(any(line.startswith("certificate_file") for line in lines))
        self.assertEqual(
            (self.root / "config.pins-noauth.bak").read_text(), NOAUTH_CONFIG
        )

    def test_is_idempotent(self):
        self.config.write_text(NOAUTH_CONFIG)
        self.control.write_text(HARDENED_CONTROL)

        self.run_helper()
        first = self.config.read_text()
        result = self.run_helper()

        self.assertEqual(self.config.read_text(), first)
        self.assertIn("already up to date", result.stdout)

    def test_custom_authenticated_config_is_left_alone(self):
        custom = "enable_auth=true\nusername=me\npassword=secret\n"
        self.config.write_text(custom)
        self.control.write_text(HARDENED_CONTROL)

        result = self.run_helper()

        self.assertEqual(self.config.read_text(), custom)
        self.assertIn("custom authenticated config", result.stdout)

    def test_missing_config_is_a_noop(self):
        self.control.write_text(HARDENED_CONTROL)

        result = self.run_helper()

        self.assertFalse(self.config.exists())
        self.assertIn("nothing to do", result.stdout)


class EnsureWayvncAuthPackagingTests(unittest.TestCase):
    def test_helper_is_packaged_and_wired_in(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "build-deb.yml").read_text(encoding="utf-8")
        service = (REPO_ROOT / "systemd" / "sysupdate-api.service").read_text(encoding="utf-8")
        upgrade = (REPO_ROOT / "scripts" / "system-upgrade.sh").read_text(encoding="utf-8")

        self.assertIn("cp scripts/ensure-wayvnc-auth.sh build/usr/local/bin/", workflow)
        self.assertIn("ExecStartPre=-+/usr/local/bin/ensure-wayvnc-auth.sh", service)
        self.assertIn('WAYVNC_AUTH_SCRIPT="/usr/local/bin/ensure-wayvnc-auth.sh"', upgrade)
