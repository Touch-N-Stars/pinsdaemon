import ast
from pathlib import Path
import re
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_MODULE = REPO_ROOT / "app" / "main.py"
MANAGE_PLUGIN_SCRIPT = REPO_ROOT / "scripts" / "manage-plugin.sh"


def read_literal_assignment(name):
    module = ast.parse(MAIN_MODULE.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Assignment not found: {name}")


class PluginRegistryTests(unittest.TestCase):
    def test_api_and_installer_allowlists_match(self):
        source = MANAGE_PLUGIN_SCRIPT.read_text(encoding="utf-8")
        match = re.search(r"ALLOWED_PLUGINS=\((.*?)\)", source, re.DOTALL)

        self.assertIsNotNone(match)
        script_packages = set(re.findall(r'"(pins-plugin-[^"]+)"', match.group(1)))

        api_packages = set(read_literal_assignment("AVAILABLE_PLUGIN_PACKAGES"))

        self.assertEqual(api_packages, script_packages)

    def test_perihelion_is_available_and_mutable(self):
        package_name = "pins-plugin-perihelion"

        available_packages = set(read_literal_assignment("AVAILABLE_PLUGIN_PACKAGES"))
        protected_packages = set(read_literal_assignment("PROTECTED_PLUGIN_PACKAGES"))

        self.assertIn(package_name, available_packages)
        self.assertNotIn(package_name, protected_packages)


if __name__ == "__main__":
    unittest.main()
