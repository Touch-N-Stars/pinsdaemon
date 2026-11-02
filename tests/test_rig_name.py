import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pins-rig-name"
DASH = shutil.which("dash")


@unittest.skipUnless(DASH, "pins-rig-name regression tests require Dash")
class RigNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.name_file = self.root / "etc" / "rig-name"
        self.env = {
            **os.environ,
            "LC_ALL": "C",
            "PINS_RIG_ID": "",
            "PINS_RIG_NAME_FILE": str(self.name_file),
        }

    def run_name(self, configured="", *args):
        result = subprocess.run(
            [DASH, str(SCRIPT), *args],
            env={**self.env, "PINS_RIG_ID": configured},
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        return result.stdout.strip()

    def save_name(self, name):
        self.name_file.parent.mkdir(parents=True, exist_ok=True)
        self.name_file.write_text(name, encoding="utf-8")

    def test_environment_override_is_accepted_under_dash(self):
        for name in ("pins", "myrig", "field-rig-42", "rig.example"):
            with self.subTest(name=name):
                self.assertEqual(self.run_name(name), name)

    def test_environment_override_takes_precedence_over_saved_name(self):
        self.save_name("saved-rig\n")
        self.assertEqual(self.run_name("override-rig"), "override-rig")
        self.assertEqual(self.name_file.read_text(), "saved-rig\n")

    def test_saved_name_survives_repeated_reads(self):
        self.save_name("pins\nignored-second-line\n")
        self.assertEqual(self.run_name(), "pins")
        self.assertEqual(self.run_name(), "pins")

    def test_names_keep_existing_normalization_and_length_limit(self):
        self.assertEqual(self.run_name(" My Field Rig! "), "my-field-rig")
        self.assertEqual(self.run_name("r" * 80), "r" * 63)
        self.save_name("Saved Field Rig!\n")
        self.assertEqual(self.run_name(), "saved-field-rig")

    def test_invalid_environment_uses_saved_name(self):
        self.save_name("saved-rig\n")
        for name in ("!!!", ".hidden", "rig..example", "."):
            with self.subTest(name=name):
                self.assertEqual(self.run_name(name), "saved-rig")

    def test_missing_or_invalid_names_use_same_hardware_fallback(self):
        fallback = self.run_name()
        self.assertRegex(fallback, re.compile(r"^pins-[a-z0-9]{5}$"))
        self.assertFalse(self.name_file.exists())
        self.save_name(".invalid\n")
        self.assertEqual(self.run_name("!!!"), fallback)

    def test_ensure_preserves_saved_name_and_is_idempotent(self):
        # Only ownership is stubbed: tests never require root or touch /etc/pins.
        self.stub_ownership()
        self.save_name("chosen-rig\n")
        for _ in range(2):
            self.assertEqual(self.run_name("", "--ensure"), "chosen-rig")
            self.assertEqual(self.name_file.read_text(), "chosen-rig\n")
            self.assertEqual(self.name_file.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(self.name_file.parent.glob(".rig-name.*")), [])

    def test_ensure_persists_environment_override(self):
        self.stub_ownership()
        self.assertEqual(self.run_name("My Rig", "--ensure"), "my-rig")
        self.assertEqual(self.name_file.read_text(), "my-rig\n")
        self.assertEqual(self.run_name(), "my-rig")

    def stub_ownership(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        chown = bin_dir / "chown"
        chown.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        chown.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]


if __name__ == "__main__":
    unittest.main()
