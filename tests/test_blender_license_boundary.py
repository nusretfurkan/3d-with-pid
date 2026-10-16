"""Licence import boundary: ordinary entry points never load the GPL Blender modules.

The Blender API integration files are licensed GPL-3.0-or-later (see
``docs/LICENSING.md``). Only the offline builder (``skid.build``, also used by
``skid.cli``) starts Blender, as a separate program; the interactive service
does not start Blender at all. Importing ``skid.cli``, ``skid.build`` or the
interactive service in plain Python must therefore load neither
``bpy``/``mathutils`` nor any of those files. The pure
``skid.adapters.blender_support.plan``/``geometry`` modules are allowed.

Each check runs in a fresh interpreter so modules loaded by other tests in this
process cannot hide or fake a result. This file only names the modules as
strings; it imports none of them itself.
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BLENDER_API_ROOTS = ("bpy", "mathutils")
GPL_MODULES = (
    "skid.adapters.blender",
    "skid.adapters.blender_runner",
    "skid.adapters.blender_support.scene",
    "skid.adapters.blender_support.preview",
    "skid.adapters.blender_support.run_build",
)
ENTRY_MODULES = (
    "skid.cli",
    "skid.build",
    "skid.interactive",
    "skid.interactive.__main__",
    "skid.interactive.worker",
)


def loaded_after_import(*modules):
    """Import ``modules`` in a fresh interpreter and return the sorted names in ``sys.modules``."""
    code = (
        "import importlib, json, sys\n"
        f"for name in {list(modules)!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise AssertionError(f"importing {modules} failed:\n{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def boundary_violations(loaded):
    return sorted(m for m in loaded if m.split(".")[0] in BLENDER_API_ROOTS or m in GPL_MODULES)


class BlenderLicenceBoundaryTests(unittest.TestCase):
    def test_entry_points_load_no_blender_api_or_gpl_module(self):
        for module in ENTRY_MODULES:
            with self.subTest(module=module):
                loaded = loaded_after_import(module)
                self.assertIn(module, loaded)
                self.assertEqual(boundary_violations(loaded), [])

    def test_build_keeps_blender_adapter_through_the_pure_plan_module(self):
        code = (
            "import json\n"
            "from skid import build\n"
            "from skid.adapters.blender_support import plan\n"
            "print(json.dumps({'supported': {k: list(v) for k, v in build.SUPPORTED_ADAPTERS.items()},\n"
            "                  'plan': [plan.ADAPTER, plan.ADAPTER_VERSION],\n"
            "                  'artifacts': build.ARTIFACTS['blender'], 'bootstrap': build.BOOTSTRAP}))\n"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(info["plan"], ["blender", "0.1.0"])
        self.assertEqual(info["supported"]["blender"], ["0.1.0"])
        self.assertEqual(info["artifacts"], ["model.blend", "blender.manifest.json"])
        self.assertEqual(info["bootstrap"], os.path.join(ROOT, "skid", "adapters", "blender_runner.py"))
        self.assertTrue(os.path.isfile(info["bootstrap"]))
        loaded = loaded_after_import("skid.build")
        self.assertIn("skid.adapters.blender_support.plan", loaded)

    def test_probe_detects_a_gpl_module_when_one_is_loaded(self):
        # Positive control for the probe itself: the GPL bridge, imported directly, is reported.
        self.assertEqual(boundary_violations(loaded_after_import("skid.adapters.blender")),
                         ["skid.adapters.blender"])


if __name__ == "__main__":
    unittest.main()
