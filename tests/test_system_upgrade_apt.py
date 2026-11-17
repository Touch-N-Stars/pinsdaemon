"""Package-resolution integration tests for the PINS upgrade path.

Each test runs the real system-upgrade.sh (and, for the regression, the legacy
updater from 1e72c70) with real apt-get/dpkg against a private root. See
apt_sandbox.py. The field report: pinsdaemon 1.0.6~unstable+43241103993.1 was
kept back because its successor added avahi-utils to Depends and the installed
updater ran `apt-get upgrade` without --with-new-pkgs.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apt_sandbox import SCRIPTS, AptSandbox, sandbox_unavailable_reason

OLD = "1.0.6~unstable+43241103993.1"
NEW = "1.0.6~unstable+43949403375.1"
TRANSITIONAL = "1.0.6~unstable+43950000000.1"
KERNEL_OLD = "1:6.18.50-1+rpt1"
KERNEL_NEW = "1:6.18.60-1+rpt1"

SKIP_REASON = sandbox_unavailable_reason()


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class SandboxTestCase(unittest.TestCase):
    def setUp(self):
        self.apt = AptSandbox()
        self.addCleanup(self.apt.cleanup)

    def assertOutcome(self, result, outcome, exit_code):
        report = self.apt.report()
        self.assertEqual(report["outcome"], outcome, result.stdout + result.stderr)
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        return report

    def rig_with_pinsdaemon(self, *extra_installed):
        """A rig on the field version with the approved kernel installed and held."""
        self.apt.add_package("pinsdaemon", OLD)
        self.apt.add_package("linux-image-rpi-v8", KERNEL_OLD)
        self.apt.publish(("pinsdaemon", OLD), ("linux-image-rpi-v8", KERNEL_OLD), *extra_installed)
        self.apt.install("pinsdaemon", "linux-image-rpi-v8", *[name for name, _ in extra_installed])


class LegacyUpdaterRegressionTests(SandboxTestCase):
    def test_legacy_updater_keeps_back_pinsdaemon_that_adds_a_dependency(self):
        # Scenario 1: the field failure, reproduced with the shipped legacy script.
        self.rig_with_pinsdaemon()
        self.apt.add_package("pinsdaemon", NEW, depends="avahi-utils")
        self.apt.add_package("avahi-utils", "0.8-16")
        self.apt.publish(("pinsdaemon", NEW), ("avahi-utils", "0.8-16"), ("linux-image-rpi-v8", KERNEL_OLD))

        result = self.apt.run_legacy_upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("kept back", result.stdout)
        self.assertIn("pinsdaemon", result.stdout)
        # The misleading claim from the bug report.
        self.assertIn("System is already up to date.", result.stdout)
        self.assertEqual(self.apt.version("pinsdaemon"), OLD)
        self.assertIsNone(self.apt.version("avahi-utils"))

    def test_legacy_updater_also_keeps_back_a_new_recommends(self):
        # Why the transitional release cannot simply demote avahi-utils to Recommends.
        self.rig_with_pinsdaemon()
        self.apt.add_package("pinsdaemon", NEW, recommends="avahi-utils")
        self.apt.add_package("avahi-utils", "0.8-16")
        self.apt.publish(("pinsdaemon", NEW), ("avahi-utils", "0.8-16"), ("linux-image-rpi-v8", KERNEL_OLD))

        self.apt.run_legacy_upgrade()

        self.assertEqual(self.apt.version("pinsdaemon"), OLD)


def control_relations():
    """Depends/Recommends/Suggests of the package this repository builds."""
    fields = {}
    for line in (SCRIPTS.parent / "packaging" / "DEBIAN" / "control").read_text().splitlines():
        name, _, value = line.partition(":")
        if name in ("Depends", "Recommends", "Suggests"):
            fields[name.lower()] = value.strip()
    return fields


class LegacyRecoveryPathTests(SandboxTestCase):
    def test_legacy_updater_installs_transitional_release_and_runtime_check_adds_avahi_utils(self):
        # Scenario 14: the supported recovery path for rigs still on the old updater
        # (the rig in the field report: pins and plugins upgrade, pinsdaemon did not).
        relations = control_relations()
        # Every Depends of the last legacy-reachable release is on the rig already.
        legacy_depends = [item.split("(")[0].strip() for item in relations["depends"].split(",")]
        base = [(name, "1.0") for name in legacy_depends]
        sdk = [("pins-nitecrawlersdk", "1.0"), ("pins-wanderercoversdk", "1.0"),
               ("pins-wandereretasdk", "1.0"), ("pins-wandererrotatorsdk", "1.0")]
        for name, version in base + sdk:
            self.apt.add_package(name, version)
        self.rig_with_pinsdaemon(*base, *sdk)
        # The transitional package with this repository's real package relations.
        self.apt.add_package("pinsdaemon", TRANSITIONAL, **relations)
        self.apt.add_package("avahi-utils", "0.8-16")
        self.apt.publish(("pinsdaemon", TRANSITIONAL), ("avahi-utils", "0.8-16"),
                         ("linux-image-rpi-v8", KERNEL_OLD), *base, *sdk)

        legacy = self.apt.run_legacy_upgrade()

        self.assertEqual(legacy.returncode, 0, legacy.stdout + legacy.stderr)
        self.assertEqual(self.apt.version("pinsdaemon"), TRANSITIONAL)
        self.assertIsNone(self.apt.version("avahi-utils"))

        # The new daemon starts and runs its root-owned startup package check.
        ensure = self.apt.run("bash", str(SCRIPTS / "ensure-required-packages.sh"), check=False)

        self.assertEqual(ensure.returncode, 0, ensure.stdout + ensure.stderr)
        self.assertEqual(self.apt.version("avahi-utils"), "0.8-16")
        self.assertEqual(self.apt.version("linux-image-rpi-v8"), KERNEL_OLD)

        # The next upgrade already runs the new updater and reports up to date.
        result = self.apt.run_upgrade()
        self.assertOutcome(result, "up_to_date", 0)

    def test_runtime_check_installs_what_it_can_when_one_package_is_unavailable(self):
        self.rig_with_pinsdaemon()
        self.apt.add_package("avahi-utils", "0.8-16")
        self.apt.publish(("pinsdaemon", OLD), ("avahi-utils", "0.8-16"), ("linux-image-rpi-v8", KERNEL_OLD))

        ensure = self.apt.run("bash", str(SCRIPTS / "ensure-required-packages.sh"), check=False)

        self.assertNotEqual(ensure.returncode, 0)  # the SDK packages are not in this repository
        self.assertIn("Could not install required packages", ensure.stderr)
        self.assertEqual(self.apt.version("avahi-utils"), "0.8-16")


class RuntimePackageTriggerTests(SandboxTestCase):
    """The postinst starts ensure-required-packages.sh while the upgrade still runs."""

    def upgrade_unit_shim(self, active_polls):
        # systemctl reports a pins-sysupgrade unit as active for the first polls.
        counter = self.apt.base / "polls"
        body = f"""
echo "systemctl $*" >> "$PINS_TEST_CALLS"
case "$*" in
  *list-units*pins-sysupgrade-*)
    n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "{counter}"
    if [ "$n" -le {active_polls} ]; then
      echo "pins-sysupgrade-x.service loaded active running upgrade"
      # The upgrade is still running: nothing may have been installed yet.
      dpkg-query -W -f='${{Status}}' avahi-utils 2>/dev/null | grep -q installed && echo EARLY >> "$PINS_TEST_CALLS"
    fi ;;
esac
exit 0"""
        shim = self.apt.bin / "systemctl"
        shim.write_text("#!/bin/sh\n" + body + "\n")
        shim.chmod(0o755)

    def run_helper(self, **env):
        merged = dict(self.apt.env, PINS_ENSURE_POLL_SECONDS="0", **env)
        return subprocess.run(["bash", str(SCRIPTS / "ensure-required-packages.sh")], env=merged,
                              capture_output=True, text=True, timeout=300)

    def rig_missing_avahi_utils(self):
        sdk = [("pins-nitecrawlersdk", "1.0"), ("pins-wanderercoversdk", "1.0"),
               ("pins-wandereretasdk", "1.0"), ("pins-wandererrotatorsdk", "1.0")]
        for name, version in sdk:
            self.apt.add_package(name, version)
        self.rig_with_pinsdaemon(*sdk)
        self.apt.add_package("avahi-utils", "0.8-16")
        self.apt.publish(("pinsdaemon", OLD), ("avahi-utils", "0.8-16"), ("linux-image-rpi-v8", KERNEL_OLD), *sdk)

    def test_waits_for_the_running_upgrade_then_installs_avahi_utils(self):
        self.rig_missing_avahi_utils()
        self.upgrade_unit_shim(active_polls=3)

        result = self.run_helper()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Waiting for the running system upgrade to finish", result.stdout)
        self.assertEqual(self.apt.version("avahi-utils"), "0.8-16")
        self.assertEqual(self.apt.calls().count("list-units"), 4)
        self.assertNotIn("EARLY", self.apt.calls())

    def test_gives_up_instead_of_waiting_forever(self):
        self.rig_missing_avahi_utils()
        self.upgrade_unit_shim(active_polls=1000)

        result = self.run_helper(PINS_ENSURE_UPGRADE_WAIT_SECONDS="0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("retrying on the next daemon start", result.stderr)
        self.assertIsNone(self.apt.version("avahi-utils"))

    def test_waits_for_the_pins_package_lock(self):
        self.rig_missing_avahi_utils()
        holder = subprocess.Popen(["flock", str(self.apt.lock_file), "sleep", "3"])
        self.addCleanup(holder.wait)
        import time
        time.sleep(0.5)  # the lock is held, as by a legacy updater
        started = time.monotonic()

        result = self.run_helper(PINS_ENSURE_LOCK_WAIT_SECONDS="60")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 2)
        self.assertEqual(self.apt.version("avahi-utils"), "0.8-16")


class NewDependencyTests(SandboxTestCase):
    def test_new_updater_installs_pinsdaemon_with_its_new_dependency(self):
        # Scenarios 2, 3, 4, 5: new dependency, --with-new-pkgs, installed together,
        # pinsdaemon not held while the kernel is.
        self.rig_with_pinsdaemon()
        self.apt.add_package("pinsdaemon", NEW, depends="avahi-utils")
        self.apt.add_package("avahi-utils", "0.8-16")
        self.apt.add_package("linux-image-rpi-v8", KERNEL_NEW)
        self.apt.publish(("pinsdaemon", NEW), ("avahi-utils", "0.8-16"), ("linux-image-rpi-v8", KERNEL_NEW))

        result = self.apt.run_upgrade()

        report = self.assertOutcome(result, "upgraded", 0)
        self.assertEqual(self.apt.version("pinsdaemon"), NEW)
        self.assertEqual(self.apt.version("avahi-utils"), "0.8-16")
        self.assertEqual(report["newPackages"], ["avahi-utils"])
        # Approved kernel hold: still held, still the old kernel, not a failure.
        self.assertEqual(self.apt.version("linux-image-rpi-v8"), KERNEL_OLD)
        self.assertIn("linux-image-rpi-v8", self.apt.held())
        self.assertEqual(report["approvedKernelHolds"], ["linux-image-rpi-v8"])
        self.assertEqual(report["keptBackPackages"], [])
        self.assertNotIn("System is already up to date.", result.stdout)
        self.assertIn("System upgrade completed successfully.", result.stdout)
        self.assertIn("systemctl restart pins", self.apt.calls())
        state = self.apt.state()
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["upgradeReport"]["outcome"], "upgraded")

    def test_upgrade_without_new_dependencies(self):
        # Scenario 6.
        self.rig_with_pinsdaemon()
        self.apt.add_package("pinsdaemon", NEW)
        self.apt.publish(("pinsdaemon", NEW), ("linux-image-rpi-v8", KERNEL_OLD))

        result = self.apt.run_upgrade()

        report = self.assertOutcome(result, "upgraded", 0)
        self.assertEqual(self.apt.version("pinsdaemon"), NEW)
        self.assertEqual(report["newPackages"], [])

    def test_multiple_new_dependencies_are_installed(self):
        # Scenario 7.
        self.rig_with_pinsdaemon()
        self.apt.add_package("pinsdaemon", NEW, depends="avahi-utils, libfoo1 (>= 2)")
        self.apt.add_package("avahi-utils", "0.8-16", depends="libavahi-client3")
        self.apt.add_package("libavahi-client3", "0.8-16")
        self.apt.add_package("libfoo1", "2.0")
        self.apt.publish(("pinsdaemon", NEW), ("avahi-utils", "0.8-16"), ("libavahi-client3", "0.8-16"),
                         ("libfoo1", "2.0"), ("linux-image-rpi-v8", KERNEL_OLD))

        result = self.apt.run_upgrade()

        report = self.assertOutcome(result, "upgraded", 0)
        self.assertEqual(self.apt.version("pinsdaemon"), NEW)
        self.assertEqual(sorted(report["newPackages"]), ["avahi-utils", "libavahi-client3", "libfoo1"])


class UnresolvableUpgradeTests(SandboxTestCase):
    def test_missing_dependency_reports_incomplete_with_reason(self):
        # Scenarios 8 and 13: unresolvable dependency, incomplete transaction.
        self.rig_with_pinsdaemon()
        self.apt.add_package("pinsdaemon", NEW, depends="avahi-utils")
        self.apt.publish(("pinsdaemon", NEW), ("linux-image-rpi-v8", KERNEL_OLD))  # avahi-utils missing

        result = self.apt.run_upgrade()

        report = self.assertOutcome(result, "incomplete", 20)
        self.assertEqual(self.apt.version("pinsdaemon"), OLD)
        self.assertEqual(report["errorCode"], "UPGRADE_PINS_PACKAGE_PENDING")
        self.assertEqual(report["pendingPinsPackages"],
                         [{"name": "pinsdaemon", "installed": OLD, "candidate": NEW}])
        self.assertTrue(any("avahi-utils" in line for line in report["diagnostics"]), report["diagnostics"])
        self.assertNotIn("System is already up to date.", result.stdout)
        self.assertNotIn("System upgrade completed successfully.", result.stdout)
        state = self.apt.state()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["exitCode"], 20)
        self.assertEqual(state["errorCode"], "UPGRADE_PINS_PACKAGE_PENDING")
        self.assertIn("pinsdaemon", state["errorMessage"])

    def test_conflict_requiring_a_removal_is_never_resolved_by_removing(self):
        # Scenarios 8 and 9: a conflict APT could only solve by removing a package.
        self.apt.add_package("legacy-tool", "1.0")
        self.rig_with_pinsdaemon(("legacy-tool", "1.0"))
        self.apt.add_package("pinsdaemon", NEW, conflicts="legacy-tool")
        self.apt.publish(("pinsdaemon", NEW), ("legacy-tool", "1.0"), ("linux-image-rpi-v8", KERNEL_OLD))

        result = self.apt.run_upgrade()

        report = self.assertOutcome(result, "incomplete", 20)
        self.assertEqual(self.apt.version("legacy-tool"), "1.0")
        self.assertEqual(self.apt.version("pinsdaemon"), OLD)
        self.assertTrue(report["diagnostics"], result.stdout)

    def test_held_pins_package_while_other_packages_upgrade(self):
        # Scenario 11.
        self.apt.add_package("indi-bin", "2.0")
        self.rig_with_pinsdaemon(("indi-bin", "2.0"))
        self.apt.hold("pinsdaemon")
        self.apt.add_package("pinsdaemon", NEW)
        self.apt.add_package("indi-bin", "2.1")
        self.apt.publish(("pinsdaemon", NEW), ("indi-bin", "2.1"), ("linux-image-rpi-v8", KERNEL_OLD))

        result = self.apt.run_upgrade()

        report = self.assertOutcome(result, "incomplete", 20)
        self.assertEqual(self.apt.version("indi-bin"), "2.1")
        self.assertEqual(self.apt.version("pinsdaemon"), OLD)
        self.assertTrue(any("on hold" in line for line in report["diagnostics"]), report["diagnostics"])

    def test_unrelated_package_kept_back_is_reported_without_claiming_completion(self):
        # libcamera-ipa in the field: its update needs a package the kernel hold excludes.
        self.apt.add_package("libcamera-ipa", "0.5.0")
        self.rig_with_pinsdaemon(("libcamera-ipa", "0.5.0"))
        self.apt.add_package("pinsdaemon", NEW)
        self.apt.add_package("libcamera-ipa", "0.6.0", depends=f"linux-image-rpi-v8 (>= {KERNEL_NEW})")
        self.apt.add_package("linux-image-rpi-v8", KERNEL_NEW)
        self.apt.publish(("pinsdaemon", NEW), ("libcamera-ipa", "0.6.0"), ("linux-image-rpi-v8", KERNEL_NEW))

        result = self.apt.run_upgrade()

        report = self.assertOutcome(result, "partial", 0)
        self.assertEqual(self.apt.version("pinsdaemon"), NEW)
        self.assertEqual(report["keptBackPackages"], ["libcamera-ipa"])
        self.assertEqual(self.apt.version("linux-image-rpi-v8"), KERNEL_OLD)
        self.assertNotIn("System upgrade completed successfully.", result.stdout)
        self.assertNotIn("System is already up to date.", result.stdout)
        self.assertIn("kept back", report["message"])


class NoUpdateAndRecoveryTests(SandboxTestCase):
    def test_no_updates_reports_up_to_date(self):
        # Scenario 10.
        self.rig_with_pinsdaemon()

        result = self.apt.run_upgrade()

        self.assertOutcome(result, "up_to_date", 0)
        self.assertIn("System is already up to date.", result.stdout)
        self.assertNotIn("systemctl restart pins", self.apt.calls())
        self.assertEqual(self.apt.state()["status"], "success")

    def test_interrupted_installation_is_completed_before_upgrading(self):
        self.rig_with_pinsdaemon()
        self.apt.set_status("pinsdaemon", "install ok unpacked")
        self.apt.add_package("pinsdaemon", NEW)
        self.apt.publish(("pinsdaemon", NEW), ("linux-image-rpi-v8", KERNEL_OLD))

        result = self.apt.run_upgrade()

        self.assertIn("Interrupted package installation detected", result.stdout)
        self.assertOutcome(result, "upgraded", 0)
        self.assertEqual(self.apt.version("pinsdaemon"), NEW)

    def test_update_script_never_runs_full_or_dist_upgrade(self):
        self.rig_with_pinsdaemon()
        self.apt.add_package("pinsdaemon", NEW, depends="avahi-utils")
        self.apt.add_package("avahi-utils", "0.8-16")
        self.apt.publish(("pinsdaemon", NEW), ("avahi-utils", "0.8-16"), ("linux-image-rpi-v8", KERNEL_OLD))
        # Record every apt-get invocation through a pass-through shim.
        real = subprocess.run(["sh", "-c", "command -v apt-get"], capture_output=True, text=True).stdout.strip()
        shim = self.apt.bin / "apt-get"
        shim.write_text(f'#!/bin/sh\necho "apt-get $*" >> "$PINS_TEST_CALLS"\nexec {real} "$@"\n')
        shim.chmod(0o755)

        result = self.apt.run_upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [line for line in self.apt.calls().splitlines() if line.startswith("apt-get")]
        self.assertTrue(any(" upgrade -y --with-new-pkgs --allow-downgrades --no-remove" in c for c in calls), calls)
        for call in calls:
            self.assertNotIn("full-upgrade", call)
            self.assertNotIn("dist-upgrade", call)
            self.assertNotIn("--allow-change-held-packages", call)
            self.assertNotIn("--force", call)
        self.assertTrue(os.path.exists(self.apt.work / "upgrade.log"))


if __name__ == "__main__":
    unittest.main()
