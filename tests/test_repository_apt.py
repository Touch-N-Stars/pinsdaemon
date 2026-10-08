"""Exercise real APT resolution against isolated, generated package indexes."""
import importlib.util
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "linux" and all(shutil.which(tool) for tool in
                    ("apt-get", "apt-cache", "dpkg-deb", "dpkg-scanpackages")), "Requires Linux APT tooling")
class RepositoryAptTests(unittest.TestCase):
    def test_install_update_lower_unstable_and_return_to_stable(self):
        spec = importlib.util.spec_from_file_location("repository_apt", ROOT / "scripts/manage-repository.py")
        repository = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(repository)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            architecture = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
            for directory in ("etc/apt/sources.list.d", "etc/apt/preferences.d", "var/lib/apt/lists/partial", "var/cache/apt/archives/partial", "var/lib/dpkg"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            (root / "var/lib/dpkg/status").write_text("")
            (root / "etc/apt/sources.list").write_text("")

            def build_suite(suite, version):
                repo = root / "repo" / suite
                (repo / "pool").mkdir(parents=True, exist_ok=True)
                package = root / "build" / suite
                (package / "DEBIAN").mkdir(parents=True, exist_ok=True)
                (package / "DEBIAN/control").write_text(
                    f"Package: pins\nVersion: {version}\nArchitecture: {architecture}\nMaintainer: Test <test@example.invalid>\nDescription: Repository selection fixture\n")
                subprocess.run(["dpkg-deb", "--build", str(package), str(repo / "pool/pins.deb")], check=True, capture_output=True)
                index = repo / f"dists/{suite}/main/binary-{architecture}"
                index.mkdir(parents=True, exist_ok=True)
                packages = subprocess.check_output(["dpkg-scanpackages", "pool", "/dev/null"], cwd=repo, stderr=subprocess.DEVNULL)
                (index / "Packages").write_bytes(packages)
                (index.parent.parent / "Release").write_text(
                    f"Origin: Touch-N-Stars\nCodename: {suite}\nArchitectures: {architecture}\nComponents: main\n"
                    f"SHA256:\n {hashlib.sha256(packages).hexdigest()} {len(packages)} main/binary-{architecture}/Packages\n")

            def apt(command, *args):
                result = subprocess.run([command, "-o", f"Dir={root}", "-o", f"Dir::State::status={root}/var/lib/dpkg/status",
                    "-o", "Debug::NoLocking=true", "-o", "APT::Get::List-Cleanup=false", *args],
                    text=True, capture_output=True, env=os.environ | {"LC_ALL": "C"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            # Generate production preferences using the production helper. Only
            # replace source URIs for the isolated fixture, never host APT files.
            (root / "etc/apt/sources.list.d/pins.list").write_text(
                f"deb [arch={architecture}] https://repo.touch-n-stars.eu/reprepro trixie main\n")
            # Existing images have a broad origin pin at 1001. Specific channel
            # rules must still select unstable when its version is lower.
            (root / "etc/apt/preferences.d/origin.pref").write_text(
                'Package: *\nPin: origin ""\nPin-Priority: 1001\n')
            build_suite("trixie", "5.0")
            build_suite("unstable", "4.0~unstable")

            def select(channel):
                repository.configure_repository(channel, root, refresh=False)
                for path in (root / "etc/apt/sources.list.d").glob("*.list"):
                    text = path.read_text()
                    suite = "unstable" if " unstable " in text else "trixie"
                    path.write_text(f"deb [trusted=yes] file:{root}/repo/{suite} {suite} main\n")
                apt("apt-get", "update")

            def restore_sources():
                (root / "etc/apt/sources.list.d/pins.list").write_text(
                    f"deb [arch={architecture}] https://repo.touch-n-stars.eu/reprepro trixie main\n")
                (root / "etc/apt/sources.list.d/pins-channel.list").unlink(missing_ok=True)

            def installed(version):
                (root / "var/lib/dpkg/status").write_text(
                    f"Package: pins\nStatus: install ok installed\nPriority: optional\nSection: misc\nVersion: {version}\nArchitecture: {architecture}\nMaintainer: Test <test@example.invalid>\nDescription: Installed fixture\n\n")

            select("trixie")
            self.assertIn("Candidate: 5.0", apt("apt-cache", "policy", "pins"))
            self.assertIn("Inst pins (5.0", apt("apt-get", "-s", "install", "pins"))
            installed("5.0")
            restore_sources()
            select("unstable")
            self.assertIn("Candidate: 4.0~unstable", apt("apt-cache", "policy", "pins"))
            self.assertIn("Inst pins [5.0] (4.0~unstable", apt("apt-get", "-s", "--allow-downgrades", "upgrade"))
            installed("4.0~unstable")
            build_suite("unstable", "6.0~unstable")
            apt("apt-get", "update")
            self.assertIn("Candidate: 6.0~unstable", apt("apt-cache", "policy", "pins"))
            self.assertIn("Inst pins [4.0~unstable] (6.0~unstable", apt("apt-get", "-s", "--allow-downgrades", "upgrade"))
            installed("6.0~unstable")
            restore_sources()
            select("trixie")
            self.assertIn("Candidate: 5.0", apt("apt-cache", "policy", "pins"))
            self.assertIn("Inst pins [6.0~unstable] (5.0", apt("apt-get", "-s", "--allow-downgrades", "upgrade"))


if __name__ == "__main__":
    unittest.main()
