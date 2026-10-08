import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main
from app.auth import API_TOKEN

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manage_swap", ROOT / "scripts/manage-swap.py")
swap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(swap)


class SwapHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Scale byte units for fixtures rather than allocate multi-GB test files.
        units = patch.object(swap, "MIB", 1024)
        units.start()
        self.addCleanup(units.stop)
        self.write("usr/lib/systemd/system-generators/rpi-swap-generator", "")
        self.write("etc/rpi/swap.conf", "[Main]\nMechanism=auto\n")
        self.write("var/swap", "")
        self.write("proc/swaps", "Filename Type Size Used Priority\n/dev/zram0 partition 2048 0 100\n")
        with (self.root / "var/swap").open("r+b") as file:
            file.truncate(2048 * swap.MIB)

    def write(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")

    def configure(self, size):
        with patch.object(swap.shutil, "disk_usage", return_value=SimpleNamespace(free=20 * 1024**3)):
            return swap.configure_swap(size, self.root)

    def test_selected_sizes_enable_disk_swap_without_changing_live_swap(self):
        self.assertEqual(swap.swap_status(self.root)["configuredSizeMb"], 2048)
        for size in (4, 8, 2):
            result = self.configure(size)
            self.assertEqual(result["configuredSizeMb"], size * 1024)
            self.assertEqual(result["activeFileSizeMb"], 2048)
            self.assertTrue(result["pendingReboot"])
            self.assertEqual(result["mechanism"], "swapfile")
            self.assertEqual(result["activeSwapSizeMb"], 2048)
            self.assertEqual((self.root / "etc/rpi/swap.conf").read_text(), "[Main]\nMechanism=auto\n")

    def test_low_space_rejects_growth_without_writing(self):
        with patch.object(swap.shutil, "disk_usage", return_value=SimpleNamespace(free=swap.MIB)):
            with self.assertRaisesRegex(ValueError, "free storage"):
                swap.configure_swap(8, self.root)
        self.assertFalse((self.root / ("etc/rpi/swap.conf.d/" + swap.DROPIN)).exists())

    def test_pending_clears_when_new_file_size_is_observed(self):
        self.configure(4)
        with (self.root / "var/swap").open("r+b") as file:
            file.truncate(4096 * swap.MIB)
        self.write("proc/swaps", "Filename Type Size Used Priority\n/var/swap file 4096 0 -2\n")
        self.assertFalse(swap.swap_status(self.root)["pendingReboot"])

    def test_existing_eight_gb_backing_file_still_needs_mechanism_change(self):
        with (self.root / "var/swap").open("r+b") as file:
            file.truncate(8192 * swap.MIB)
        result = self.configure(8)
        self.assertEqual(result["activeFileSizeMb"], 8192)
        self.assertEqual(result["activeSwapSizeMb"], 2048)
        self.assertTrue(result["pendingReboot"])

    def test_real_swap_header_is_rounded_up_and_other_swap_is_reported(self):
        with patch.object(swap, "MIB", 1024 * 1024):
            self.write("proc/swaps", "Filename Type Size Used Priority\n/var/swap file 8388604 0 -2\n")
            self.assertEqual(swap.active_swap(self.root, "/var/swap"), (8192, 8192))
            self.write("proc/swaps", "Filename Type Size Used Priority\n/var/swap file 8388604 0 -2\n/dev/zram0 partition 2097148 0 100\n")
            self.assertEqual(swap.active_swap(self.root, "/var/swap"), (10240, 8192))

    def test_later_mechanism_override_rolls_back(self):
        self.write("etc/rpi/swap.conf.d/zz-custom.conf", "[Main]\nMechanism=zram+file\n")
        with self.assertRaisesRegex(ValueError, "mechanism"):
            self.configure(8)
        self.assertFalse((self.root / ("etc/rpi/swap.conf.d/" + swap.DROPIN)).exists())

    def test_refuses_device_or_directory_as_swap_file(self):
        self.write("etc/rpi/swap.conf", "[File]\nPath=/var\n")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.configure(4)

    def test_later_override_rolls_back(self):
        self.write("etc/rpi/swap.conf.d/zz-custom.conf", "[File]\nFixedSizeMiB=2048\n")
        with self.assertRaisesRegex(ValueError, "overrides"):
            self.configure(8)
        self.assertFalse((self.root / ("etc/rpi/swap.conf.d/" + swap.DROPIN)).exists())

    def test_dropin_precedence_and_custom_file_path(self):
        self.write("usr/lib/rpi/swap.conf.d/40-image.conf", "[File]\nFixedSizeMiB=4096\n")
        self.write("etc/rpi/swap.conf.d/40-image.conf", "[File]\nFixedSizeMiB=2048\nPath=/var/custom-swap\n")
        self.write("var/custom-swap", "")
        status = swap.swap_status(self.root)
        self.assertEqual(status["configuredSizeMb"], 2048)
        self.assertEqual(status["activeFileSizeMb"], 0)

    def test_disabled_or_zram_only_swap_is_not_silently_changed(self):
        for mechanism in ("none", "zram"):
            self.write("etc/rpi/swap.conf", f"[Main]\nMechanism={mechanism}\n")
            self.assertFalse(swap.swap_status(self.root)["supported"])
            with self.assertRaises(ValueError):
                self.configure(4)

    def test_legacy_sets_both_size_and_two_gb_cap(self):
        (self.root / "usr/lib/systemd/system-generators/rpi-swap-generator").unlink()
        self.write("sbin/dphys-swapfile", "")
        self.write("etc/dphys-swapfile", "# Keep comment\nCONF_SWAPSIZE=2048\nCONF_MAXSWAP=2048\nCONF_SWAPFILE=/var/swap\n")
        self.configure(8)
        text = (self.root / "etc/dphys-swapfile").read_text()
        self.assertIn("CONF_SWAPSIZE=8192", text)
        self.assertIn("CONF_MAXSWAP=8192", text)
        self.assertIn("# Keep comment", text)
        self.assertIn("CONF_SWAPFILE=/var/swap", text)

    def test_invalid_values_rejected_without_mutation(self):
        for size in (0, 3, 16, "4", True, 4.0):
            with self.assertRaises(ValueError):
                self.configure(size)


class SwapApiTests(unittest.IsolatedAsyncioTestCase):
    def status(self, **changes):
        fields = dict(supported=True, backend="rpi-swap", mechanism="zram+file",
                      configuredSizeMb=2048, activeFileSizeMb=2048, availableBytes=20 * 1024**3,
                      pendingReboot=False, optionsGb=[2, 4, 8], defaultSizeGb=2)
        return main.SwapStatusResponse(**(fields | changes))

    def test_model_strict_allowlist(self):
        for size in (1, 3, 16, "4", True, 4.0):
            with self.assertRaises(ValidationError):
                main.SwapUpdateRequest(sizeGb=size)
        with self.assertRaises(ValidationError):
            main.SwapUpdateRequest(sizeGb=4, path="/tmp/swap")

    async def test_reads_and_validates_helper_status(self):
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(
            return_value=(self.status().model_dump_json().encode(), b"")))
        with patch.object(main.os.path, "isfile", return_value=True):
            with patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                result = await main.get_system_swap()
        self.assertEqual(result.activeFileSizeMb, 2048)

    async def test_missing_helper_and_bad_output_are_unavailable(self):
        with patch.object(main.os.path, "isfile", return_value=False):
            with self.assertRaises(HTTPException) as error:
                await main.get_system_swap()
            self.assertEqual(error.exception.status_code, 503)
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"invalid", b"")))
        with patch.object(main.os.path, "isfile", return_value=True):
            with patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                with self.assertRaises(HTTPException) as error:
                    await main.get_system_swap()
                self.assertEqual(error.exception.status_code, 503)

    async def test_update_queues_only_an_allowlisted_helper_command(self):
        job = SimpleNamespace(id="swap-1", status=main.JobStatus.STARTED, exit_code=None,
                              created_at=1.0, finished_at=None, command="swap set 4")
        with patch.object(main, "_read_swap_status", AsyncMock(return_value=self.status())):
            with patch.object(main.job_manager, "start_job", AsyncMock(return_value="swap-1")) as start:
                with patch.object(main.job_manager, "get_job", return_value=job):
                    result = await main.update_system_swap(main.SwapUpdateRequest(sizeGb=4))
        start.assert_awaited_once_with(["sudo", "-n", main.SWAP_SCRIPT_PATH, "set", "4"])
        self.assertEqual(result.jobId, "swap-1")

    async def test_unsupported_and_low_disk_do_not_start_jobs(self):
        for status in (self.status(supported=False, unsupportedReason="Unsupported"),
                       self.status(availableBytes=1)):
            with patch.object(main, "_read_swap_status", AsyncMock(return_value=status)):
                with patch.object(main.job_manager, "start_job", AsyncMock()) as start:
                    with self.assertRaises(HTTPException) as error:
                        await main.update_system_swap(main.SwapUpdateRequest(sizeGb=8))
                    self.assertEqual(error.exception.status_code, 409)
                    start.assert_not_called()

    def test_routes_require_auth_and_validate_body(self):
        client = TestClient(main.app)  # No lifespan: no real host startup jobs.
        for method in (client.get, client.put):
            response = method("/system/swap")
            self.assertIn(response.status_code, (401, 403))
        response = client.put("/system/swap", headers={"Authorization": f"Bearer {API_TOKEN}"},
                              json={"sizeGb": 3})
        self.assertEqual(response.status_code, 422)

    def test_package_installs_root_owned_helper_and_exact_sudo_rules(self):
        workflow = (ROOT / ".github/workflows/build-deb.yml").read_text()
        self.assertIn("cp scripts/manage-swap.py build/usr/local/bin/", workflow)
        sudoers = (ROOT / "packaging/sudoers").read_text()
        for args in ("status", "set 2", "set 4", "set 8"):
            self.assertIn(f"/usr/local/bin/manage-swap.py {args}\n", sudoers)
        self.assertNotIn("manage-swap.py *", sudoers)
        self.assertIn("chown root:root /usr/local/bin/manage-swap.py",
                      (ROOT / "packaging/DEBIAN/postinst").read_text())


if __name__ == "__main__":
    unittest.main()
