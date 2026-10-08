import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app import main
from app.auth import API_TOKEN

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manage_repository", ROOT / "scripts/manage-repository.py")
repository = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repository)


class RepositoryHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stable = "deb [arch=arm64 signed-by=/usr/share/keyrings/pins.gpg] https://repo.touch-n-stars.eu/reprepro trixie main\n"
        self.source = self.write("etc/apt/sources.list.d/pins.list", self.stable)
        self.other = self.write("etc/apt/sources.list.d/debian.sources", "Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie trixie-updates\nComponents: main\n")

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def switch(self, channel):
        return repository.configure_repository(channel, self.root, refresh=False)

    def test_switch_both_ways_preserves_signatures_and_os_sources(self):
        other = self.other.read_bytes()
        status = self.switch("unstable")
        self.assertEqual(status["channel"], "unstable")
        self.assertEqual(status["suites"], ["unstable"])
        self.assertEqual(self.source.read_text(), self.stable.replace("trixie", "unstable"))
        overlay = self.root / "etc/apt/sources.list.d/pins-channel.list"
        self.assertFalse(overlay.exists())
        status = self.switch("trixie")
        self.assertEqual(status["suites"], ["trixie"])
        self.assertFalse(overlay.exists())
        self.assertEqual(self.other.read_bytes(), other)
        pin = (self.root / "etc/apt/preferences.d/pins-channel.pref").read_text()
        self.assertIn("Package: pins pinsdaemon pins-plugin-*", pin)
        self.assertIn("o=Touch-N-Stars,n=trixie", pin)
        self.assertIn("Pin-Priority: -1", pin)

    def test_repeated_switch_does_not_duplicate_sources(self):
        self.switch("unstable")
        self.switch("unstable")
        self.assertEqual(len(repository.inspect_sources(self.root)), 1)

    def test_migrates_existing_unstable_list_and_keeps_disabled_comments(self):
        unstable = self.stable.replace("trixie", "unstable")
        self.source.write_text("#" + unstable + unstable)
        self.switch("trixie")
        self.assertEqual(self.source.read_text(), "#" + unstable + self.stable)

    def test_deb822_preserves_key_and_multiline_suites(self):
        self.source.unlink()
        text = "Types: deb\nURIs: https://repo.touch-n-stars.eu/reprepro/\nSuites: trixie\n unstable\nComponents: main\nSigned-By: /usr/share/keyrings/pins.gpg\n"
        path = self.write("etc/apt/sources.list.d/pins.sources", text)
        self.switch("unstable")
        self.assertIn("Suites: unstable\nComponents", path.read_text())
        overlay = self.root / "etc/apt/sources.list.d/pins-channel.sources"
        self.assertFalse(overlay.exists())
        self.assertIn("Signed-By: /usr/share/keyrings/pins.gpg", path.read_text())
        self.switch("trixie")
        self.assertFalse(overlay.exists())

    def test_removes_legacy_overlay_in_either_channel(self):
        for channel in ("unstable", "trixie"):
            overlay = self.write("etc/apt/sources.list.d/pins-channel.list", self.stable.replace("trixie", "unstable"))
            self.source.write_text(self.stable)
            self.assertEqual(self.switch(channel)["suites"], [channel])
            self.assertFalse(overlay.exists())
            self.assertEqual(self.source.read_text(), self.stable.replace("trixie", channel))

    def test_managed_only_source_can_switch_back(self):
        self.source.unlink()
        overlay = self.write("etc/apt/sources.list.d/pins-channel.list", self.stable.replace("trixie", "unstable"))
        self.assertEqual(self.switch("trixie")["suites"], ["trixie"])
        self.assertEqual(overlay.read_text(), self.stable)

    def test_missing_source_and_invalid_channel_cannot_write(self):
        self.source.unlink()
        for channel in ("unstable", "testing", "unstable; touch /tmp/pwn"):
            with self.assertRaises(ValueError):
                self.switch(channel)
        self.assertFalse((self.root / "etc/apt/preferences.d/pins-channel.pref").exists())

    def test_disabled_source_is_ignored(self):
        self.source.unlink()
        self.write("etc/apt/sources.list.d/pins.sources", "Types: deb\nURIs: https://repo.touch-n-stars.eu/reprepro\nSuites: unstable\nEnabled: no\n")
        self.assertFalse(repository.repository_status(self.root)["configured"])

    def test_mixed_deb822_uris_refused_without_mutation(self):
        self.source.unlink()
        path = self.write("etc/apt/sources.list.d/mixed.sources", "Types: deb\nURIs: https://repo.touch-n-stars.eu/reprepro https://deb.debian.org/debian\nSuites: trixie\n")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.switch("unstable")
        self.assertEqual(path.read_bytes(), before)

    def test_failed_signed_apt_refresh_restores_all_files(self):
        self.switch("unstable")
        paths = repository.source_paths(self.root) + [self.root / "etc/apt/preferences.d/pins-channel.pref"]
        before = {path: path.read_bytes() for path in paths}
        with patch.object(repository.subprocess, "run", side_effect=[subprocess.CalledProcessError(100, "apt-get"), None]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                repository.configure_repository("trixie", self.root)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(run.call_count, 2)
        self.assertIn("APT::Update::Error-Mode=any", run.call_args_list[0].args[0])

    def test_failed_refresh_restores_legacy_overlay(self):
        overlay = self.write("etc/apt/sources.list.d/pins-channel.list", self.stable.replace("trixie", "unstable"))
        before = {path: path.read_bytes() for path in repository.source_paths(self.root)}
        with patch.object(repository.subprocess, "run", side_effect=[subprocess.CalledProcessError(100, "apt-get"), None]):
            with self.assertRaises(subprocess.CalledProcessError):
                repository.configure_repository("unstable", self.root)
        self.assertEqual({path: path.read_bytes() for path in repository.source_paths(self.root)}, before)
        self.assertTrue(overlay.exists())

    def test_successful_refresh_runs_apt_and_reports_channel(self):
        with patch.object(repository.subprocess, "run") as run:
            result = repository.configure_repository("unstable", self.root)
        self.assertEqual(result["channel"], "unstable")
        run.assert_called_once_with(["apt-get", "-o", "APT::Update::Error-Mode=any", "update"], check=True)


class RepositoryApiTests(unittest.IsolatedAsyncioTestCase):
    def status(self, channel="unstable"):
        return main.RepositoryStatusResponse(channel=channel, configured=True,
                    suites=["trixie", "unstable"] if channel == "unstable" else ["trixie"],
                    repositoryUrl=repository.REPOSITORY_URL, options=list(repository.CHANNELS))

    def test_auth_and_strict_channel_validation(self):
        client = TestClient(main.app)
        for method in (client.get, client.post):
            self.assertIn(method("/repository").status_code, (401, 403))
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        for body in ({"channel": "testing"}, {"channel": True}, {"channel": "unstable", "url": "http://evil"}):
            self.assertEqual(client.post("/repository", headers=headers, json=body).status_code, 422)

    async def test_api_schedules_allowlisted_command(self):
        job = SimpleNamespace(id="repo-1", status="started", exit_code=None, created_at=1, finished_at=None, command="switch")
        with patch.object(main, "_read_repository_status", AsyncMock(return_value=self.status())):
            with patch.object(main.job_manager, "start_job", AsyncMock(return_value=job.id)) as start:
                with patch.object(main.job_manager, "get_job", return_value=job):
                    response = await main.change_repository(main.RepositoryChangeRequest(channel="trixie"))
        start.assert_awaited_once_with(["sudo", "-n", main.REPOSITORY_SCRIPT_PATH, "set", "trixie"])
        self.assertEqual(response.jobId, "repo-1")

    async def test_unstable_metadata_excludes_stable_only_plugins(self):
        unstable = "Package: pins\nVersion: 4~unstable\n"
        with patch.object(main.os.path, "isfile", return_value=True):
            with patch.object(main, "_read_repository_status", AsyncMock(return_value=self.status())):
                with patch.object(main, "_fetch_packages_index", return_value=unstable) as fetch:
                    versions = main._parse_packages_versions(await main._fetch_active_repository_packages())
        self.assertEqual(versions, {"pins": "4~unstable"})
        fetch.assert_called_once_with(f"{repository.REPOSITORY_URL}/dists/unstable/main/binary-arm64/Packages")

    async def test_return_to_stable_is_reported_as_available_update(self):
        with patch.object(main, "_fetch_active_repository_packages", AsyncMock(return_value="Package: pins\nVersion: 2\n")):
            with patch.object(main, "_get_installed_package_versions", AsyncMock(return_value={"pins": "3~unstable"})):
                result = await main.check_updates()
        self.assertTrue(result.hasUpdates)
        self.assertTrue(result.packages[0].updateAvailable)

    async def test_missing_helper_status_is_unavailable(self):
        with patch.object(main.os.path, "isfile", return_value=False):
            with self.assertRaises(main.HTTPException) as error:
                await main.get_repository()
        self.assertEqual(error.exception.status_code, 503)

    def test_packaging_root_ownership_and_exact_sudo_rules(self):
        workflow = (ROOT / ".github/workflows/build-deb.yml").read_text()
        self.assertIn("cp scripts/manage-repository.py build/usr/local/bin/", workflow)
        rules = (ROOT / "packaging/sudoers").read_text()
        for args in ("status", "set trixie", "set unstable"):
            self.assertIn(f"/usr/local/bin/manage-repository.py {args}\n", rules)
        self.assertNotIn("manage-repository.py *", rules)
        self.assertIn("chown root:root /usr/local/bin/manage-repository.py", (ROOT / "packaging/DEBIAN/postinst").read_text())


if __name__ == "__main__":
    unittest.main()
