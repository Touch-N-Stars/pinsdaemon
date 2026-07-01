import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-indi-package.sh"


def extract_registry_python() -> str:
    content = INSTALL_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"if ! python3 - .*?<<'PY'\n(.*?)\nPY\nthen", content, flags=re.S)
    if not match:
        raise AssertionError("Unable to locate embedded registry Python in install script")
    return match.group(1)


class IndiRegistryInstallerTests(unittest.TestCase):
    def test_xml_driver_alias_resolves_to_installed_binary_name_and_prunes_stale_alias(self):
        embedded_python = extract_registry_python()

        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            package_root = temp_dir / "pkg"
            xml_path = package_root / "usr" / "share" / "indi" / "myfocuserpro2.xml"
            bin_path = package_root / "usr" / "bin" / "indi_myfocuserpro2_focus"
            registry_path = temp_dir / "3rdparty.json"
            helper_dir = temp_dir / "bin"

            xml_path.parent.mkdir(parents=True)
            bin_path.parent.mkdir(parents=True)
            helper_dir.mkdir()

            xml_path.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<driversList>
  <devGroup group="Focusers">
    <device label="MyFocuserPro2 / Gemini EAF" driver="indi_myfocuserpro2" />
  </devGroup>
</driversList>
""",
                encoding="utf-8",
            )
            bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
            bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR)

            registry_path.write_text(
                json.dumps(
                    {
                        "camera": [],
                        "filterwheel": [],
                        "flatpanel": [],
                        "focuser": [
                            {
                                "Name": "indi_myfocuserpro2",
                                "Label": "Gemini EAF",
                                "Type": "focuser",
                            }
                        ],
                        "rotator": [],
                        "switches": [],
                        "telescope": [],
                        "weather": [],
                    }
                ),
                encoding="utf-8",
            )

            fake_dpkg_query = helper_dir / ("dpkg-query.cmd" if os.name == "nt" else "dpkg-query")
            xml_out = xml_path.as_posix()
            bin_out = bin_path.as_posix()
            if os.name == "nt":
                fake_dpkg_query.write_text(
                    f"@echo off\r\n"
                    f"if \"%1\"==\"-L\" if \"%2\"==\"indi-myfocuserpro2\" (\r\n"
                    f"  echo {xml_out}\r\n"
                    f"  echo {bin_out}\r\n"
                    f"  exit /b 0\r\n"
                    f")\r\n"
                    f"exit /b 1\r\n",
                    encoding="utf-8",
                )
            else:
                fake_dpkg_query.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = \"-L\" ] && [ \"$2\" = \"indi-myfocuserpro2\" ]; then\n"
                    f"  printf '%s\\n' '{xml_out}' '{bin_out}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "exit 1\n",
                    encoding="utf-8",
                )
                fake_dpkg_query.chmod(fake_dpkg_query.stat().st_mode | stat.S_IXUSR)

            python_file = temp_dir / "registry_update.py"
            embedded_python = embedded_python.replace(
                '["dpkg-query", "-L", pkg]',
                f'[{str(fake_dpkg_query)!r}, "-L", pkg]',
            )
            python_file.write_text(embedded_python, encoding="utf-8")

            env = os.environ.copy()
            env["PATH"] = f"{helper_dir}{os.pathsep}{env.get('PATH', '')}"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(python_file),
                    str(registry_path),
                    "indi-myfocuserpro2",
                    "focuser",
                    "Gemini EAF",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            updated = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(
                updated["focuser"],
                [
                    {
                        "Name": "indi_myfocuserpro2_focus",
                        "Label": "Gemini EAF",
                        "Type": "focuser",
                    }
                ],
            )
            self.assertIn(
                "Resolved XML driver aliases: indi_myfocuserpro2 -> indi_myfocuserpro2_focus",
                completed.stdout,
            )


if __name__ == "__main__":
    unittest.main()
