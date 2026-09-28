import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class OptionalCameraStartupTests(unittest.TestCase):
    def test_window_opens_without_any_camera_sdk(self):
        code = '''
import importlib.abc
import sys
class BlockCameraSDKs(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'imagingcontrol4', 'pymmcore'}:
            raise ImportError('deliberately unavailable for startup test')
sys.meta_path.insert(0, BlockCameraSDKs())
sys.path.insert(0, 'tests')
from qt_support import APP
from main_window import MainWindow
window = MainWindow()
assert not window.camera_registry.backends
assert window.device_combo.count() == 2
assert window.device_combo.itemText(1) == 'Micro-Manager Camera Setup…'
assert not window.start_recording_action.isEnabled()
window.close()
APP.processEvents()
print('OPTIONAL_SDK_STARTUP_OK')
'''
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PRIMA_CONFIG_DIR=folder, PRIM_RESULTS_DIR=folder)
            result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[1], env=env,
                                    text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OPTIONAL_SDK_STARTUP_OK", result.stdout)
