"""A throwaway APT/dpkg installation for package-resolution integration tests.

Real apt-get, apt-mark, apt-cache, dpkg and dpkg-query operate on a private
root (status database, sources, lists, archives) selected through APT_CONFIG
and DPKG_ADMINDIR, with a local flat repository of generated packages. The
host's own package database is never touched. Generated packages carry no
maintainer scripts and only install files below <root>/opt.
"""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
REQUIRED_TOOLS = ("apt-get", "apt-mark", "apt-cache", "dpkg", "dpkg-deb", "dpkg-query", "dpkg-scanpackages", "flock", "stdbuf")


def sandbox_unavailable_reason():
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        return f"needs {', '.join(missing)}"
    if os.geteuid() != 0:
        return "real dpkg/apt transactions in a private root need root (CI runs them with sudo)"
    return None


class AptSandbox:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pins-apt-")
        self.base = Path(self._tmp.name)
        self.root = self.base / "root"
        self.repo = self.base / "repo"
        self.bin = self.base / "bin"
        self.work = self.base / "work"
        self.state_file = self.base / "last-upgrade-job.json"
        self.lock_file = self.base / "pins-repository.lock"
        self.calls_log = self.base / "calls.log"
        for path in (
            self.repo, self.bin, self.root / "var/lib/dpkg/info", self.root / "var/lib/dpkg/updates",
            self.root / "etc/apt/sources.list.d", self.root / "etc/apt/preferences.d",
            self.root / "etc/apt/apt.conf.d", self.root / "var/cache/apt/archives/partial",
            self.root / "var/lib/apt/lists/partial", self.root / "var/log/apt",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "var/lib/dpkg/status").touch()
        (self.root / "etc/apt/sources.list").write_text(f"deb [trusted=yes] file:{self.repo} ./\n")
        self.apt_config = self.base / "apt.conf"
        r = self.root
        self.apt_config.write_text(textwrap.dedent(f"""\
            Dir "{r}/";
            Dir::State::status "{r}/var/lib/dpkg/status";
            Dir::Etc::SourceList "{r}/etc/apt/sources.list";
            Dir::Etc::SourceParts "{r}/etc/apt/sources.list.d";
            Dir::Etc::Preferences "{r}/etc/apt/preferences";
            Dir::Etc::PreferencesParts "{r}/etc/apt/preferences.d";
            Dir::Etc::Parts "{r}/etc/apt/apt.conf.d";
            DPkg::Options {{ "--root={r}"; "--admindir={r}/var/lib/dpkg"; "--force-script-chrootless"; }};
            APT::Sandbox::User "root";
            APT::Architecture "amd64";
            Acquire::AllowInsecureRepositories "true";
            APT::Get::AllowUnauthenticated "true";
            """))
        # Commands the upgrade must not reach on a development machine.
        self._shim("systemctl", 'echo "systemctl $*" >> "$PINS_TEST_CALLS"; exit 0')
        self._shim("rpi-update", 'echo "rpi-update $*" >> "$PINS_TEST_CALLS"; exit 1')
        self._shim("systemd-run", 'echo "systemd-run $*" >> "$PINS_TEST_CALLS"; exit 1')
        self.packages = {}

    def cleanup(self):
        self._tmp.cleanup()

    def _shim(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    @property
    def env(self):
        env = dict(os.environ)
        env.update(
            APT_CONFIG=str(self.apt_config),
            DPKG_ADMINDIR=str(self.root / "var/lib/dpkg"),
            PATH=f"{self.bin}:{env.get('PATH', '/usr/bin:/bin')}",
            PINS_TEST_CALLS=str(self.calls_log),
            PINS_UPDATE_DETACHED="true",
            PINS_REPOSITORY_LOCK=str(self.lock_file),
            PINS_UPGRADE_WORK_DIR=str(self.work),
            # Pretend the running kernel is already the approved one.
            PINS_TARGET_KERNEL_VERSION=os.uname().release,
            DEBIAN_FRONTEND="noninteractive",
            LC_ALL="C",
        )
        return env

    def run(self, *args, check=True):
        result = subprocess.run(list(args), env=self.env, capture_output=True, text=True, timeout=300)
        if check and result.returncode != 0:
            raise AssertionError(f"{args} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        return result

    def add_package(self, name, version, depends=None, recommends=None, suggests=None, conflicts=None,
                    breaks=None):
        build = self.base / "build" / f"{name}_{version}"
        (build / "DEBIAN").mkdir(parents=True)
        payload = build / "opt" / name
        payload.mkdir(parents=True)
        (payload / "version").write_text(version + "\n")
        control = [f"Package: {name}", f"Version: {version}", "Architecture: all", "Maintainer: PINS tests <t@example.invalid>"]
        for field, value in (("Depends", depends), ("Recommends", recommends), ("Suggests", suggests),
                             ("Conflicts", conflicts), ("Breaks", breaks)):
            if value:
                control.append(f"{field}: {value}")
        control.append(f"Description: PINS test package {name}")
        (build / "DEBIAN" / "control").write_text("\n".join(control) + "\n")
        deb = self.base / "debs" / f"{name}_{version}_all.deb"
        deb.parent.mkdir(exist_ok=True)
        self.run("dpkg-deb", "--root-owner-group", "--build", str(build), str(deb))
        self.packages[(name, version)] = deb
        return deb

    def publish(self, *name_versions):
        """Make exactly these package versions the repository's content and refresh lists."""
        for old in self.repo.glob("*.deb"):
            old.unlink()
        for key in name_versions:
            shutil.copy(self.packages[key], self.repo)
        index = subprocess.run(
            ["dpkg-scanpackages", "-m", ".", "/dev/null"], cwd=self.repo, capture_output=True, text=True, check=True
        )
        (self.repo / "Packages").write_text(index.stdout)
        self.run("apt-get", "update")

    def install(self, *specs):
        """Install name=version specs as the starting state of a rig."""
        self.run("apt-get", "install", "-y", "--allow-downgrades", *specs)

    def hold(self, name):
        self.run("apt-mark", "hold", name)

    def held(self):
        return self.run("apt-mark", "showhold").stdout.split()

    def version(self, name):
        result = self.run("dpkg-query", "-W", "-f=${Status}\t${Version}", name, check=False)
        status, _, version = result.stdout.partition("\t")
        return version if result.returncode == 0 and status.endswith(" ok installed") else None

    def set_status(self, name, status):
        path = self.root / "var/lib/dpkg/status"
        lines = path.read_text().splitlines()
        current = None
        for index, line in enumerate(lines):
            if line.startswith("Package: "):
                current = line.split(": ", 1)[1]
            elif line.startswith("Status: ") and current == name:
                lines[index] = f"Status: {status}"
        path.write_text("\n".join(lines) + "\n")

    def run_script(self, script_path, job_id="0f1e2d3c-4b5a-4968-8776-655443322110"):
        return self.run("bash", str(script_path), "--job-id", job_id, "--state-file", str(self.state_file), check=False)

    def run_upgrade(self):
        return self.run_script(SCRIPTS / "system-upgrade.sh")

    def run_legacy_upgrade(self):
        """The updater shipped up to pinsdaemon 1e72c70 (plain apt-get upgrade)."""
        source = (FIXTURES / "legacy-system-upgrade-1e72c70.sh").read_text()
        legacy = self.base / "legacy-system-upgrade.sh"
        legacy.write_text(source.replace("/run/lock/pins-repository.lock", str(self.lock_file)))
        return self.run_script(legacy)

    def state(self):
        return json.loads(self.state_file.read_text())

    def report(self):
        return json.loads((self.work / "report.json").read_text())

    def calls(self):
        return self.calls_log.read_text() if self.calls_log.exists() else ""
