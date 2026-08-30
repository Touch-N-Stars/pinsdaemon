import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import main
from app.job_manager import JobStatus


class AstapInstallJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_active_astap_job_is_returned_instead_of_starting_another(self):
        active_job = SimpleNamespace(
            id="existing-job",
            command=f"sudo -n {main.ASTAP_STAR_DATABASE_INSTALL_SCRIPT_PATH} D50",
            status=JobStatus.RUNNING,
            exit_code=None,
            created_at=123.0,
            finished_at=None,
            progress_phase="downloading",
            progress_percent=25,
            progress_bytes=250,
            progress_total_bytes=1000,
            progress_updated_at=124.0,
        )

        with (
            patch.object(main.os.path, "exists", return_value=True),
            patch.object(main, "_build_astap_star_databases", return_value=[]),
            patch.object(main.job_manager, "jobs", {active_job.id: active_job}),
            patch.object(main.job_manager, "start_job") as start_job,
        ):
            response = await main.install_astap_star_database(
                main.AstapStarDatabaseInstallRequest(databaseId="D50")
            )

        start_job.assert_not_called()
        self.assertEqual(response.jobId, "existing-job")
        self.assertEqual(response.progressPhase, "downloading")
        self.assertEqual(response.progressPercent, 25)


if __name__ == "__main__":
    unittest.main()
