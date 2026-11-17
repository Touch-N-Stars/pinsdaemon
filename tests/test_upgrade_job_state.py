"""Upgrade job status across daemon restarts and stale persisted records.

The upgrade runs in a detached systemd unit and records its own result in
UPGRADE_LAST_JOB_FILE, because pinsdaemon itself is often restarted by the
upgrade it is tracking. These tests pin down what clients see in that case.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import main
from app.job_manager import Job, JobManager, JobStatus


REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_UPGRADE = REPO_ROOT / "scripts" / "system-upgrade.sh"

JOB_ID = "bda1a98b-8e15-4b2e-9fc8-c0a5352b0c6d"
OTHER_JOB_ID = "0f1e2d3c-4b5a-4968-8776-655443322110"

UNIT_ACTIVE = {"LoadState": "loaded", "ActiveState": "active", "Result": "success", "ExecMainStatus": "0"}
UNIT_GONE = {"LoadState": "not-found", "ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0"}
UNIT_FAILED = {"LoadState": "loaded", "ActiveState": "failed", "Result": "exit-code", "ExecMainStatus": "100"}
UNIT_SUCCEEDED = {"LoadState": "loaded", "ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0"}


def stored_record(status, job_id=JOB_ID, exit_code=None, finished_at=None):
    return {
        "jobId": job_id,
        "status": status,
        "exitCode": exit_code,
        "startedAt": 1794481035.7,
        "finishedAt": finished_at,
        "command": f"sudo -n /usr/local/bin/system-upgrade.sh --job-id {job_id}",
    }


class UpgradeStateTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "last-upgrade-job.json")
        patches = [
            patch.object(main, "UPGRADE_LAST_JOB_FILE", self.state_file),
            # A fresh daemon after a restart: no runtime jobs in memory.
            patch.object(main.job_manager, "jobs", {}),
            patch("app.main.append_local_log"),
            patch("app.job_manager.append_local_log"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def write_state(self, record):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(record, f)

    def read_state(self):
        with open(self.state_file, encoding="utf-8") as f:
            return json.load(f)

    def unit(self, properties):
        mock = AsyncMock(return_value=properties)
        p = patch.object(main, "_read_upgrade_unit_properties", mock)
        p.start()
        self.addCleanup(p.stop)
        return mock


class PersistedStatusAfterRestartTests(UpgradeStateTestCase):
    async def test_success_recorded_by_detached_script_survives_daemon_restart(self):
        # The field case: the daemon restarted mid-upgrade, the script finished
        # and wrote success. Polling the job id must return that result.
        self.write_state(stored_record("success", exit_code=0, finished_at=1794481166.0))
        unit = self.unit(UNIT_GONE)

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.SUCCESS)
        self.assertEqual(response.exitCode, 0)
        unit.assert_not_awaited()  # terminal records are never second-guessed

    async def test_failure_recorded_by_detached_script_keeps_exit_code(self):
        self.write_state(stored_record("failed", exit_code=100, finished_at=1794481166.0))
        self.unit(UNIT_GONE)

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.FAILED)
        self.assertEqual(response.exitCode, 100)

    async def test_running_record_stays_running_while_unit_is_active(self):
        self.write_state(stored_record("running"))
        self.unit(UNIT_ACTIVE)

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.RUNNING)
        self.assertEqual(self.read_state()["status"], "running")

    async def test_unknown_job_id_is_404_even_when_another_upgrade_is_stored(self):
        self.write_state(stored_record("success", exit_code=0, finished_at=1.0))

        with self.assertRaises(HTTPException) as raised:
            await main.get_job_status(OTHER_JOB_ID)

        self.assertEqual(raised.exception.status_code, 404)


class StaleRunningRecordTests(UpgradeStateTestCase):
    async def test_vanished_unit_without_result_settles_to_unknown(self):
        # Reboot or power loss mid-upgrade: the record says running forever.
        self.write_state(stored_record("running"))
        self.unit(UNIT_GONE)

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.UNKNOWN)
        self.assertIsNone(response.exitCode)
        self.assertEqual(response.errorCode, main.UPGRADE_OUTCOME_UNKNOWN_CODE)
        self.assertIsNotNone(response.finishedAt)
        persisted = self.read_state()
        self.assertEqual(persisted["status"], "unknown")
        self.assertEqual(persisted["errorCode"], main.UPGRADE_OUTCOME_UNKNOWN_CODE)

    async def test_failed_unit_settles_to_failed_with_its_exit_status(self):
        self.write_state(stored_record("running"))
        self.unit(UNIT_FAILED)

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.FAILED)
        self.assertEqual(response.exitCode, 100)
        self.assertEqual(response.errorCode, main.UPGRADE_UNIT_FAILED_CODE)

    async def test_unit_that_exited_zero_settles_to_success(self):
        self.write_state(stored_record("started"))
        self.unit(UNIT_SUCCEEDED)

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.SUCCESS)
        self.assertEqual(response.exitCode, 0)

    async def test_unreadable_unit_state_does_not_invent_a_result(self):
        self.write_state(stored_record("running"))
        self.unit(None)

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.RUNNING)
        self.assertEqual(self.read_state()["status"], "running")

    async def test_result_written_while_querying_systemd_wins(self):
        self.write_state(stored_record("running"))

        async def unit_exits_and_script_records_success(job_id):
            self.write_state(stored_record("success", exit_code=0, finished_at=time.time()))
            return UNIT_GONE

        with patch.object(main, "_read_upgrade_unit_properties", side_effect=unit_exits_and_script_records_success):
            response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.SUCCESS)

    async def test_job_monitored_by_this_daemon_is_not_settled_from_systemd(self):
        self.write_state(stored_record("running"))
        main.job_manager.jobs[JOB_ID] = Job(id=JOB_ID, command="sudo -n x", status=JobStatus.RUNNING)
        unit = self.unit(UNIT_GONE)

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.RUNNING)
        unit.assert_not_awaited()

    async def test_terminal_record_wins_over_unfinished_runtime_job(self):
        self.write_state(stored_record("failed", exit_code=100, finished_at=time.time()))
        main.job_manager.jobs[JOB_ID] = Job(id=JOB_ID, command="sudo -n x", status=JobStatus.RUNNING)

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.FAILED)
        self.assertEqual(response.exitCode, 100)

    async def test_latest_job_reports_the_settled_record(self):
        self.write_state(stored_record("running"))
        self.unit(UNIT_GONE)

        response = await main.get_latest_job_status()

        self.assertEqual(response.jobId, JOB_ID)
        self.assertEqual(response.status, JobStatus.UNKNOWN)


class DuplicateUpgradeTests(UpgradeStateTestCase):
    async def test_second_upgrade_is_rejected_while_runtime_upgrade_runs(self):
        main.job_manager.jobs[JOB_ID] = Job(
            id=JOB_ID,
            command=f"sudo -n {main.SCRIPT_PATH} --job-id {JOB_ID} --state-file x",
            status=JobStatus.RUNNING,
        )
        start = AsyncMock()
        with patch.object(main.job_manager, "start_job", start):
            with self.assertRaises(HTTPException) as raised:
                await main.trigger_upgrade(main.UpgradeRequest())

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["jobId"], JOB_ID)
        start.assert_not_awaited()

    async def test_second_upgrade_is_rejected_while_detached_unit_runs_after_restart(self):
        self.write_state(stored_record("running"))
        self.unit(UNIT_ACTIVE)
        with self.assertRaises(HTTPException) as raised:
            await main.trigger_upgrade(main.UpgradeRequest())

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["jobId"], JOB_ID)

    async def test_stale_record_does_not_block_a_new_upgrade(self):
        self.write_state(stored_record("running"))
        self.unit(UNIT_GONE)

        async def fake_start(cmd, job_id, on_finished):
            main.job_manager.jobs[job_id] = Job(id=job_id, command=" ".join(cmd))
            return job_id

        with patch.object(main.job_manager, "start_job", side_effect=fake_start) as start:
            response = await main.trigger_upgrade(main.UpgradeRequest())

        start.assert_awaited_once()
        self.assertEqual(start.await_args.kwargs["on_finished"], main._persist_finished_upgrade_job)
        self.assertEqual(self.read_state()["jobId"], response.jobId)
        self.assertEqual(self.read_state()["status"], "started")


class FinishHookTests(UpgradeStateTestCase):
    def test_hook_records_daemon_result_when_script_left_record_unfinished(self):
        self.write_state(stored_record("running"))
        job = Job(id=JOB_ID, command="x", status=JobStatus.FAILED, exit_code=1, finished_at=time.time())

        main._persist_finished_upgrade_job(job)

        self.assertEqual(self.read_state()["status"], "failed")
        self.assertEqual(self.read_state()["exitCode"], 1)

    def test_hook_never_overrides_the_scripts_own_result(self):
        self.write_state(stored_record("success", exit_code=0, finished_at=1.0))
        job = Job(id=JOB_ID, command="x", status=JobStatus.FAILED, exit_code=1, finished_at=time.time())

        main._persist_finished_upgrade_job(job)

        self.assertEqual(self.read_state()["status"], "success")

    def test_hook_ignores_a_record_of_another_job(self):
        self.write_state(stored_record("running", job_id=OTHER_JOB_ID))
        job = Job(id=JOB_ID, command="x", status=JobStatus.FAILED, exit_code=1, finished_at=time.time())

        main._persist_finished_upgrade_job(job)

        self.assertEqual(self.read_state()["status"], "running")


class JobManagerFinishCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_runs_once_with_the_final_status(self):
        seen = []
        manager = JobManager()
        with patch("app.job_manager.append_local_log"):
            job_id = await manager.start_job(
                [sys.executable, "-c", "import sys; sys.exit(3)"],
                on_finished=lambda job: seen.append((job.status, job.exit_code)),
            )
            for _ in range(200):
                if manager.get_job(job_id).finished_at is not None and seen:
                    break
                await asyncio.sleep(0.01)

        self.assertEqual(seen, [(JobStatus.FAILED, 3)])

    async def test_failing_callback_does_not_break_the_job(self):
        def broken(job):
            raise RuntimeError("disk full")

        manager = JobManager()
        with patch("app.job_manager.append_local_log"):
            job_id = await manager.start_job([sys.executable, "-c", "pass"], on_finished=broken)
            for _ in range(200):
                if manager.get_job(job_id).finished_at is not None:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)

        self.assertEqual(manager.get_job(job_id).status, JobStatus.SUCCESS)


INCOMPLETE_REPORT = {
    "outcome": "incomplete",
    "errorCode": "UPGRADE_PINS_PACKAGE_PENDING",
    "message": "Upgrade incomplete: PINS packages are still pending (pinsdaemon 1 -> 2).",
    "exitCode": 20,
    "pendingPinsPackages": [{"name": "pinsdaemon", "installed": "1", "candidate": "2"}],
}


def incomplete_record(job_id=JOB_ID):
    record = stored_record("failed", job_id=job_id, exit_code=20, finished_at=1794481166.0)
    record.update(
        errorCode=INCOMPLETE_REPORT["errorCode"],
        errorMessage=INCOMPLETE_REPORT["message"],
        upgradeReport=INCOMPLETE_REPORT,
    )
    return record


class UpgradeOutcomeReportingTests(UpgradeStateTestCase):
    async def test_incomplete_outcome_survives_a_daemon_restart(self):
        # Self-update: the daemon restarted mid-upgrade and the script recorded
        # that a PINS package is still pending. Clients see why, not "success".
        self.write_state(incomplete_record())
        self.unit(UNIT_GONE)

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.status, JobStatus.FAILED)
        self.assertEqual(response.exitCode, 20)
        self.assertEqual(response.errorCode, "UPGRADE_PINS_PACKAGE_PENDING")
        self.assertIn("pinsdaemon", response.errorMessage)
        self.assertEqual(response.upgradeReport["outcome"], "incomplete")

    async def test_scripts_report_wins_over_the_daemons_own_finished_job(self):
        # No restart: the unit monitor finished the job from the exit status only.
        self.write_state(incomplete_record())
        main.job_manager.jobs[JOB_ID] = Job(
            id=JOB_ID, command="sudo -n x", status=JobStatus.FAILED, exit_code=20, finished_at=time.time()
        )

        response = await main.get_job_status(JOB_ID)

        self.assertEqual(response.errorCode, "UPGRADE_PINS_PACKAGE_PENDING")
        self.assertEqual(response.upgradeReport["pendingPinsPackages"][0]["name"], "pinsdaemon")

    async def test_success_with_kept_back_packages_carries_its_report(self):
        record = stored_record("success", exit_code=0, finished_at=1.0)
        record["upgradeReport"] = {"outcome": "partial", "keptBackPackages": ["libcamera-ipa"]}
        self.write_state(record)

        response = await main.get_latest_job_status()

        self.assertEqual(response.status, JobStatus.SUCCESS)
        self.assertEqual(response.upgradeReport["keptBackPackages"], ["libcamera-ipa"])
        self.assertIsNone(response.errorCode)


class _FakeProcess:
    def __init__(self, stdout=b"", returncode=0):
        self.returncode = None
        self._final = returncode
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self._stdout = stdout

    async def communicate(self):
        self.returncode = self._final
        return self._stdout, b""

    async def wait(self):
        self.returncode = self._final
        return self._final

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class DetachedUnitEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, journal, exec_main_status):
        def spawn(*cmd, **kwargs):
            if "journalctl" in cmd:
                return _FakeProcess(journal)
            if "is-active" in cmd:
                return _FakeProcess(b"inactive\n")
            return _FakeProcess(f"{exec_main_status}\nsuccess\n".encode())

        job = Job(id=JOB_ID, command="x")
        with patch("asyncio.create_subprocess_exec", side_effect=spawn), \
                patch("asyncio.sleep", AsyncMock()), \
                patch("app.job_manager.append_local_log"):
            await JobManager()._monitor_detached_unit(job, "pins-sysupgrade-x.service")
        return job

    async def test_up_to_date_marker_cannot_hide_a_pending_pins_package(self):
        # The legacy script printed "System is already up to date." while
        # pinsdaemon was kept back; a known non-zero exit must stay a failure.
        job = await self.evaluate(b"System is already up to date.\n", 20)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.exit_code, 20)

    async def test_clean_exit_is_success(self):
        job = await self.evaluate(b"System upgrade completed successfully.\n", 0)

        self.assertEqual(job.status, JobStatus.SUCCESS)


class UnitPropertyParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_job_ids_that_are_not_uuids(self):
        with patch("asyncio.create_subprocess_exec") as spawn:
            self.assertIsNone(await main._read_upgrade_unit_properties("x; reboot"))
        spawn.assert_not_called()


@unittest.skipUnless(
    os.name == "posix" and shutil.which("bash") and shutil.which("python3"),
    "needs bash and python3",
)
class SystemUpgradeScriptStateTests(unittest.TestCase):
    def test_dry_run_records_running_then_success_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "last-upgrade-job.json")
            result = subprocess.run(
                ["bash", str(SYSTEM_UPGRADE), "--dry-run", "--job-id", JOB_ID, "--state-file", state_file],
                capture_output=True,
                text=True,
                timeout=60,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with open(state_file, encoding="utf-8") as f:
                record = json.load(f)
            self.assertEqual(record["jobId"], JOB_ID)
            self.assertEqual(record["status"], "success")
            self.assertEqual(record["exitCode"], 0)
            self.assertIsNotNone(record["finishedAt"])
            self.assertEqual(sorted(os.listdir(tmp)), ["last-upgrade-job.json"])  # no temp files left


if __name__ == "__main__":
    unittest.main()
