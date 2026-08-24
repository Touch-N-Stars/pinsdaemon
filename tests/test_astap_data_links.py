import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ensure-astap-data-links.sh"


@unittest.skipIf(os.name == "nt", "functional symlink semantics are validated on Linux CI")
class AstapDataLinkTests(unittest.TestCase):
    @staticmethod
    def shell_path(path: Path) -> str:
        if os.name != "nt":
            return str(path)
        resolved = path.resolve()
        drive = resolved.drive.rstrip(":").lower()
        return f"/mnt/{drive}/{resolved.as_posix()[3:]}"

    def run_helper(self, source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                self.shell_path(SCRIPT),
                self.shell_path(source),
                self.shell_path(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_creates_links_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "opt-astap"
            destination = root / "astap-data"
            source.mkdir()
            (source / "d50_catalog").mkdir()
            (source / "index.dat").write_text("catalog", encoding="utf-8")

            self.run_helper(source, destination)
            self.run_helper(source, destination)

            self.assertTrue((destination / "d50_catalog").is_symlink())
            self.assertTrue((destination / "index.dat").is_symlink())
            self.assertEqual(os.readlink(destination / "d50_catalog"), str(source / "d50_catalog"))
            self.assertEqual(os.readlink(destination / "index.dat"), str(source / "index.dat"))

    def test_repairs_stale_link_but_preserves_real_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "opt-astap"
            destination = root / "astap-data"
            source.mkdir()
            destination.mkdir()
            (source / "catalog").mkdir()
            (source / "local.dat").write_text("source", encoding="utf-8")
            (destination / "catalog").symlink_to(root / "missing")
            (destination / "local.dat").write_text("keep", encoding="utf-8")

            self.run_helper(source, destination)

            self.assertEqual(os.readlink(destination / "catalog"), str(source / "catalog"))
            self.assertFalse((destination / "local.dat").is_symlink())
            self.assertEqual((destination / "local.dat").read_text(encoding="utf-8"), "keep")

    def test_missing_source_is_a_safe_noop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "missing"
            destination = root / "astap-data"

            result = self.run_helper(source, destination)

            self.assertTrue(destination.is_dir())
            self.assertIn("nothing to link", result.stdout)


class AstapDataLinkPackagingTests(unittest.TestCase):
    def test_helper_is_packaged_and_runs_before_daemon(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "build-deb.yml").read_text(encoding="utf-8")
        service = (REPO_ROOT / "systemd" / "sysupdate-api.service").read_text(encoding="utf-8")

        self.assertIn("cp scripts/ensure-astap-data-links.sh build/usr/local/bin/", workflow)
        self.assertIn("ExecStartPre=+/usr/local/bin/ensure-astap-data-links.sh", service)

    def test_database_installer_refreshes_links_immediately(self):
        installer = (REPO_ROOT / "scripts" / "install-astap-star-database.sh").read_text(encoding="utf-8")

        self.assertIn('LINK_SCRIPT="/usr/local/bin/ensure-astap-data-links.sh"', installer)
        self.assertIn('"$LINK_SCRIPT"', installer)


if __name__ == "__main__":
    unittest.main()
