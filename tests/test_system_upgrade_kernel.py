from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_UPGRADE = REPO_ROOT / "scripts" / "system-upgrade.sh"


class SystemUpgradeKernelTests(unittest.TestCase):
    def test_kernel_6_18_39_family_is_the_pinned_target(self):
        source = SYSTEM_UPGRADE.read_text(encoding="utf-8")

        self.assertIn('PINS_TARGET_KERNEL_VERSION:-6.18.39', source)
        self.assertIn(
            'PINS_TARGET_RPI_UPDATE_HASH:-9393d5a5ba364c10219a17c07bdc63c8a6887878',
            source,
        )
        self.assertIn(
            '"$running_kernel" == "$TARGET_KERNEL_VERSION"[-.+]*',
            source,
        )
        allowed_check = source.index('"$running_kernel" == "$TARGET_KERNEL_VERSION"')
        forced_update = source.index('rpi-update "$TARGET_RPI_UPDATE_HASH"')
        self.assertLess(allowed_check, forced_update)


if __name__ == "__main__":
    unittest.main()
