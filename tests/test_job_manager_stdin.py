import asyncio
import sys
import unittest

from app.job_manager import JobManager, JobStatus


class JobManagerStdinTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_stdin_is_not_exposed_and_wifi_result_is_structured(self):
        manager = JobManager()
        job_id = await manager.start_job(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.stdin.readline(); "
                    "print('PINS_WIFI_RESULT code=MISSING_CREDENTIALS "
                    "message=Saved_profile_has_no_secret'); sys.exit(1)"
                ),
            ],
            display_command="wifi-connect --password-stdin",
            stdin_data=b"not-for-logs\n",
        )

        for _ in range(100):
            job = manager.get_job(job_id)
            if job and job.finished_at is not None:
                break
            await asyncio.sleep(0.01)

        self.assertIsNotNone(job)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, "MISSING_CREDENTIALS")
        self.assertEqual(job.error_message, "Saved profile has no secret")
        self.assertNotIn("not-for-logs", job.command)
        self.assertFalse(any("not-for-logs" in line for line in job.logs))


if __name__ == "__main__":
    unittest.main()
