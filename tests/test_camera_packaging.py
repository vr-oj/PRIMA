import importlib.util
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS, ModuleType
import unittest
from unittest.mock import patch


class CameraPackagingTests(unittest.TestCase):
    def build_spec(self, bridges):
        hooks = ModuleType("PyInstaller.utils.hooks")
        hooks.collect_all = lambda name: ([], [], [name])
        modules = {"PyInstaller": ModuleType("PyInstaller"), "PyInstaller.utils": ModuleType("PyInstaller.utils"),
                   "PyInstaller.utils.hooks": hooks}
        analysis = {}
        def analyze(*args, **kwargs):
            analysis.update(kwargs)
            return NS(pure=[], scripts=[], binaries=[], datas=[], zipfiles=[])
        with patch.dict(sys.modules, modules), \
                patch.object(importlib.util, "find_spec", side_effect=lambda name: NS() if name in bridges else None):
            runpy.run_path(str(Path(__file__).parents[1] / "PRIMAcquisition.spec"), init_globals={
                "Analysis": analyze, "PYZ": lambda *a, **kw: None,
                "EXE": lambda *a, **kw: None, "COLLECT": lambda *a, **kw: None})
        return analysis

    def test_no_sdk_build_is_possible(self):
        self.assertEqual(set(self.build_spec([])["excludes"]), {"imagingcontrol4", "pymmcore"})

    def test_both_bridges_and_early_helper_hook_are_packaged(self):
        analysis = self.build_spec(["imagingcontrol4", "pymmcore"])
        self.assertIn("imagingcontrol4", analysis["hiddenimports"])
        self.assertIn("pymmcore", analysis["hiddenimports"])
        self.assertIn(os.path.join("prim_app", "hooks", "mm_worker_bootstrap.py"), analysis["runtime_hooks"])
