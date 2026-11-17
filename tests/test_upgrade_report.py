"""Upgrade plan checks and outcome classification from mocked APT output."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "pins-upgrade-report.py"

spec = importlib.util.spec_from_file_location("pins_upgrade_report", HELPER)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

KERNEL = {"linux-image-rpi-v8", "linux-headers-rpi-v8"}

# The field state: classic apt-get wording, kernel holds, camera stack kept back.
FIELD_PLAN = """\
Reading package lists...
Building dependency tree...
Calculating upgrade...
The following packages have been kept back:
  libcamera-ipa librpicam-app1 linux-base-rpi-v8
  linux-headers-rpi-v8 linux-image-rpi-v8 rpicam-apps-core
The following NEW packages will be installed:
  avahi-utils
The following packages will be upgraded:
  pinsdaemon
1 upgraded, 1 newly installed, 0 to remove and 6 not upgraded.
Inst avahi-utils (0.8-16 Debian:13.1/stable [arm64])
Inst pinsdaemon [1.0.6~unstable+43241103993.1] (1.0.6~unstable+43949403375.1 Touch-N-Stars:unstable [arm64])
Conf avahi-utils (0.8-16 Debian:13.1/stable [arm64])
Conf pinsdaemon (1.0.6~unstable+43949403375.1 Touch-N-Stars:unstable [arm64])
"""

FIELD_AFTER = """\
The following packages have been kept back:
  libcamera-ipa librpicam-app1 linux-base-rpi-v8
  linux-headers-rpi-v8 linux-image-rpi-v8 rpicam-apps-core
0 upgraded, 0 newly installed, 0 to remove and 6 not upgraded.
"""

APT3_PLAN = """\
Not upgrading:
  libcamera-ipa  linux-image-rpi-v8

Summary:
  Upgrading: 0, Installing: 0, Removing: 0, Not Upgrading: 2
"""

POLICY_PENDING = """\
pins:
  Installed: 2.3.0
  Candidate: 2.3.0
pinsdaemon:
  Installed: 1.0.6~unstable+43241103993.1
  Candidate: 1.0.6~unstable+43949403375.1
  Version table:
     1.0.6~unstable+43949403375.1 1002
        500 https://repo.touch-n-stars.eu/reprepro unstable/main arm64 Packages
pins-plugin-foo:
  Installed: 1.0
  Candidate: (none)
"""

POLICY_CURRENT = """\
pinsdaemon:
  Installed: 1.0.6~unstable+43949403375.1
  Candidate: 1.0.6~unstable+43949403375.1
"""

UNRESOLVABLE = """\
Some packages could not be installed. This may mean that you have
requested an impossible situation.
The following packages have unmet dependencies:
 pinsdaemon : Depends: avahi-utils but it is not installable
E: Unable to correct problems, you have held broken packages.
"""


class SimulationParsingTests(unittest.TestCase):
    def test_field_plan_separates_upgrades_new_packages_and_kept_back(self):
        plan = report.parse_simulation(FIELD_PLAN)

        self.assertEqual([p["name"] for p in plan["upgrade"]], ["pinsdaemon"])
        self.assertEqual(plan["upgrade"][0]["from"], "1.0.6~unstable+43241103993.1")
        self.assertEqual(plan["upgrade"][0]["to"], "1.0.6~unstable+43949403375.1")
        self.assertEqual([p["name"] for p in plan["install"]], ["avahi-utils"])
        self.assertEqual(plan["remove"], [])
        self.assertEqual(plan["keptBack"], sorted([
            "libcamera-ipa", "librpicam-app1", "linux-base-rpi-v8",
            "linux-headers-rpi-v8", "linux-image-rpi-v8", "rpicam-apps-core",
        ]))

    def test_apt3_not_upgrading_wording(self):
        self.assertEqual(report.parse_simulation(APT3_PLAN)["keptBack"], ["libcamera-ipa", "linux-image-rpi-v8"])

    def test_architecture_qualified_names(self):
        plan = report.parse_simulation("Inst libfoo:arm64 [1] (2 x [arm64])\nRemv libbar:arm64 [1]\n")
        self.assertEqual(plan["upgrade"][0]["name"], "libfoo")
        self.assertEqual(plan["remove"][0]["name"], "libbar")

    def test_policy_and_pending_pins_packages(self):
        pending = report.pins_pending(report.parse_policy(POLICY_PENDING))
        self.assertEqual(pending, [{
            "name": "pinsdaemon",
            "installed": "1.0.6~unstable+43241103993.1",
            "candidate": "1.0.6~unstable+43949403375.1",
        }])

    def test_held_packages_count_as_installed(self):
        installed = report.parse_installed("pinsdaemon\t1.0\thi \nfoo\t1\trc \navahi-utils\t0.8\tii \n")
        self.assertEqual(installed, {"pinsdaemon": "1.0", "avahi-utils": "0.8"})

    def test_pins_package_patterns(self):
        for name in ("pins", "pinsdaemon", "pins-plugin-x", "pins-nitecrawlersdk"):
            self.assertTrue(report.is_pins_package(name), name)
        for name in ("pinsx", "avahi-utils", "linux-image-rpi-v8"):
            self.assertFalse(report.is_pins_package(name), name)


class PlanCheckTests(unittest.TestCase):
    def test_new_dependency_plan_is_safe(self):
        self.assertEqual(report.check_plan(FIELD_PLAN, 0, KERNEL), (report.EXIT_OK, "ok"))

    def test_plan_with_removals_is_refused(self):
        code, message = report.check_plan(FIELD_PLAN + "Remv legacy-tool [1.0]\n", 0, KERNEL)
        self.assertEqual(code, report.EXIT_REMOVAL_REQUIRED)
        self.assertIn("legacy-tool", message)

    def test_plan_touching_kernel_is_refused(self):
        code, message = report.check_plan("Inst linux-image-rpi-v8 [1] (2 x [arm64])\n", 0, KERNEL)
        self.assertEqual(code, report.EXIT_PROTECTED_PACKAGE_CHANGE)
        self.assertIn("linux-image-rpi-v8", message)

    def test_unresolvable_plan_reports_the_dependency(self):
        code, message = report.check_plan(UNRESOLVABLE, 100, KERNEL)
        self.assertEqual(code, report.EXIT_DEPENDENCY_PROBLEM)
        self.assertIn("avahi-utils but it is not installable", message)


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.files(held="linux-headers-rpi-v8\nlinux-image-rpi-v8\n", upgrade_rc="0", dpkg_audit="")

    def files(self, **contents):
        names = {"upgrade_rc": "upgrade.rc", "targeted_rc": "targeted.rc", "dpkg_audit": "dpkg-audit.txt",
                 "targeted_plan": "targeted-plan.txt", "targeted_check": "targeted-check.txt"}
        for key, value in contents.items():
            name = names.get(key, f"{key}.txt")
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(value)

    def evaluate(self):
        return report.evaluate(self.dir, KERNEL)

    def test_field_after_fix_is_upgraded_with_camera_stack_kept_back(self):
        self.files(plan=FIELD_PLAN, verify=FIELD_AFTER, policy=POLICY_CURRENT)

        result = self.evaluate()

        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(result["approvedKernelHolds"], ["linux-headers-rpi-v8", "linux-image-rpi-v8"])
        self.assertEqual(result["keptBackPackages"],
                         ["libcamera-ipa", "librpicam-app1", "linux-base-rpi-v8", "rpicam-apps-core"])
        self.assertNotIn("up to date", result["message"])

    def test_only_approved_kernel_holds_left_is_a_full_success(self):
        after = "The following packages have been kept back:\n  linux-image-rpi-v8\n"
        self.files(plan=FIELD_PLAN, verify=after, policy=POLICY_CURRENT)

        result = self.evaluate()

        self.assertEqual(result["outcome"], "upgraded")
        self.assertEqual(result["keptBackPackages"], [])

    def test_nothing_to_do_is_up_to_date(self):
        self.files(plan="0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
                   verify="", policy=POLICY_CURRENT)

        result = self.evaluate()

        self.assertEqual(result["outcome"], "up_to_date")
        self.assertEqual(result["message"], "System is already up to date.")

    def test_field_bug_state_is_incomplete_not_up_to_date(self):
        # What the legacy updater saw: nothing installed, pinsdaemon still pending.
        kept = FIELD_AFTER.replace("libcamera-ipa ", "libcamera-ipa pinsdaemon ")
        self.files(plan=kept, verify=kept, policy=POLICY_PENDING)

        result = self.evaluate()

        self.assertEqual(result["outcome"], "incomplete")
        self.assertEqual(result["exitCode"], report.EXIT_PINS_PENDING)
        self.assertEqual(result["errorCode"], "UPGRADE_PINS_PACKAGE_PENDING")
        self.assertIn("pinsdaemon", result["message"])
        self.assertNotIn("pinsdaemon", result["keptBackPackages"])

    def test_incomplete_includes_the_dependency_reason(self):
        self.files(plan=FIELD_AFTER, verify=FIELD_AFTER, policy=POLICY_PENDING,
                   targeted_plan=UNRESOLVABLE, targeted_check="APT cannot resolve the upgrade: x")

        result = self.evaluate()

        self.assertTrue(any("not installable" in line for line in result["diagnostics"]), result["diagnostics"])
        self.assertEqual(result["diagnostics"][0], "APT cannot resolve the upgrade: x")

    def test_failed_apt_transaction(self):
        self.files(plan=FIELD_PLAN, verify=FIELD_PLAN, policy=POLICY_PENDING, upgrade_rc="100")

        result = self.evaluate()

        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["exitCode"], report.EXIT_APT_FAILED)
        self.assertIn("100", result["message"])

    def test_interrupted_dpkg_state_is_reported(self):
        self.files(plan=FIELD_PLAN, verify="", policy=POLICY_CURRENT,
                   dpkg_audit="The following packages are only half configured:\n pinsdaemon\n")

        result = self.evaluate()

        self.assertEqual(result["outcome"], "interrupted")
        self.assertEqual(result["errorCode"], "UPGRADE_DPKG_INTERRUPTED")
        self.assertIn("dpkg --configure -a", result["message"])


class CommandLineTests(unittest.TestCase):
    def run_helper(self, *args):
        return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, timeout=30)

    def test_blocked_plan_writes_a_report_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, "plan.txt")
            out = os.path.join(tmp, "report.json")
            Path(plan).write_text(UNRESOLVABLE)

            result = self.run_helper("check-plan", plan, "--rc", "100", "--report", out, "--protected", *KERNEL)

            self.assertEqual(result.returncode, report.EXIT_DEPENDENCY_PROBLEM)
            data = json.loads(Path(out).read_text())
            self.assertEqual(data["outcome"], "blocked")
            self.assertEqual(data["errorCode"], "UPGRADE_DEPENDENCY_PROBLEM")

    def test_evaluate_prints_structured_result_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "plan.txt").write_text(FIELD_PLAN)
            Path(tmp, "verify.txt").write_text(FIELD_AFTER)
            Path(tmp, "policy.txt").write_text(POLICY_PENDING)
            Path(tmp, "upgrade.rc").write_text("0\n")

            result = self.run_helper("evaluate", tmp, "--protected", *KERNEL)

            self.assertEqual(result.returncode, 20)
            self.assertIn("PINS_UPGRADE_RESULT outcome=incomplete code=UPGRADE_PINS_PACKAGE_PENDING", result.stdout)
            self.assertEqual(json.loads(Path(tmp, "report.json").read_text())["outcome"], "incomplete")


class PackagingDependencyPolicyTests(unittest.TestCase):
    """New hard relations in pinsdaemon strand rigs that still run a legacy updater."""

    # Depends of the last release before 197c493, i.e. what every legacy
    # updater can satisfy. Extend only with packages guaranteed to be installed
    # on every PINS image; install anything else from ensure-required-packages.sh.
    LEGACY_REACHABLE_DEPENDS = {
        "python3", "python3-venv", "sudo", "bash", "util-linux", "network-manager", "wireless-tools",
        "unzip", "usbutils", "rfkill", "iw", "iputils-ping", "iputils-arping", "avahi-daemon",
        "raspi-config", "locales", "keyboard-configuration",
    }

    def control_field(self, name):
        control = (REPO_ROOT / "packaging" / "DEBIAN" / "control").read_text()
        for line in control.splitlines():
            if line.startswith(f"{name}:"):
                return {
                    alternative.split("(")[0].strip()
                    for item in line.split(":", 1)[1].split(",")
                    for alternative in item.split("|")
                    if alternative.strip()
                }
        return set()

    def test_no_new_depends_or_recommends(self):
        added = (self.control_field("Depends") | self.control_field("Pre-Depends")
                 | self.control_field("Recommends")) - self.LEGACY_REACHABLE_DEPENDS
        self.assertEqual(added, set(), (
            "apt-get upgrade without --with-new-pkgs keeps back a pinsdaemon that adds Depends or "
            "Recommends, so legacy rigs could never install it. Add the package to "
            "scripts/ensure-required-packages.sh instead."
        ))

    def test_runtime_packages_cover_avahi_utils(self):
        self.assertIn('"avahi-utils"', (REPO_ROOT / "scripts" / "ensure-required-packages.sh").read_text())

    def test_postinst_starts_the_runtime_package_helper_in_its_own_unit(self):
        # avahi-utils is installed right after this package, not on a later daemon
        # start, and a daemon restart cannot interrupt that APT transaction.
        unit = (REPO_ROOT / "systemd" / "pins-ensure-required-packages.service").read_text()
        self.assertIn("ExecStart=/usr/local/bin/ensure-required-packages.sh", unit)
        self.assertIn("Type=oneshot", unit)
        postinst = (REPO_ROOT / "packaging" / "DEBIAN" / "postinst").read_text()
        start = postinst.index("systemctl start --no-block pins-ensure-required-packages.service")
        self.assertLess(postinst.index("systemctl daemon-reload"), start)
        commands = [line for line in postinst.splitlines() if not line.lstrip().startswith("#")]
        # dpkg holds the APT lock while postinst runs, so it must never call APT itself.
        self.assertFalse([line for line in commands if "apt-get" in line or "apt install" in line])
        workflow = (REPO_ROOT / ".github" / "workflows" / "build-deb.yml").read_text()
        self.assertIn("cp systemd/pins-ensure-required-packages.service build/etc/systemd/system/", workflow)

    def test_build_ships_the_report_helper(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "build-deb.yml").read_text()
        self.assertIn("cp scripts/pins-upgrade-report.py build/usr/local/bin/", workflow)
        self.assertIn("./usr/local/bin/pins-upgrade-report.py", workflow)


if __name__ == "__main__":
    unittest.main()
