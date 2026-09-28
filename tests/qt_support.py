import os
import sys
import time
from unittest.mock import patch
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_OPENGL", "software")
os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parents[1] / "venv" / "test-mpl"))
os.environ["PRIM_RESULTS_DIR"] = str(Path(__file__).resolve().parents[1] / "venv" / "test-results")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prim_app"))
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest
from PyQt5.QtGui import QFontDatabase, QFont
APP = QApplication.instance() or QApplication([])
# Windows' offscreen platform does not enumerate its installed fonts.
if not QFontDatabase().families():
    font_path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts" / "segoeui.ttf"
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
        APP.setFont(QFont("Segoe UI", 10))
with patch("PyQt5.QtCore.QStandardPaths.writableLocation", return_value=str(Path(__file__).resolve().parents[1] / "venv" / "test-settings")):
    import utils.config


def wait_until(condition, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        APP.processEvents()
        if condition():
            return
        QTest.qWait(10)
    raise AssertionError("Timed out waiting for Qt condition")
