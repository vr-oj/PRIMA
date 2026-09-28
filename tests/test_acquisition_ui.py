import tempfile
import csv
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from qt_support import APP, wait_until
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QWidget
from main_window import MainWindow
from ui.control_panels.camera_control_panel import CameraControlPanel
from utils.recording_csv import iter_csv_data_lines


class View(QWidget):
    def _on_frame_ready(self, image, metadata):
        pass
    def clear_image(self):
        pass


class FakeSerial(QObject):
    data_ready = pyqtSignal(int, float, float)
    command_sent = pyqtSignal(str)
    finished = pyqtSignal()
    port = "TEST"
    def __init__(self):
        super().__init__()
        self.commands = []
        self.running = True
        self.auto_ack = True
        self.reject_commands = set()
    def send_command(self, command):
        self.commands.append(command)
        if command in self.reject_commands:
            return False
        if self.auto_ack:
            QTimer.singleShot(0, lambda: self.command_sent.emit(command))
        return True
    def set_idle_timeout_enabled(self, enabled):
        pass
    def isRunning(self):
        return self.running
    def stop(self):
        self.running = False


class FakeCamera(QObject):
    recording_frame_ready = pyqtSignal(QImage, object)
    timing_ready = pyqtSignal(str, object)
    timing_failed = pyqtSignal(str, str)
    preview_ready = pyqtSignal()
    finished = pyqtSignal()
    def __init__(self):
        super().__init__()
        self.requests = []
        self.running = True
    def request_recording(self, run_id, interval_s):
        self.requests.append((run_id, interval_s))
    def request_setting(self, name, value):
        self.requests.append((name, value))
    def request_preview(self):
        QTimer.singleShot(0, self.preview_ready.emit)
    def isRunning(self):
        return self.running
    def stop(self):
        self.running = False


class AcquisitionUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, value in (("main_window.QtCameraWidget", View),
                              ("main_window.MainWindow._populate_device_list", lambda self: None),
                              ("main_window.MainWindow._refresh_serial_port_list", lambda self: None),
                              ("main_window.MainWindow.showMaximized", lambda self: None),
                              ("recording_manager.MIN_FREE_SPACE_GB", 0),
                              ("main_window.get_next_fill_folder", lambda: self.temp.name)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = MainWindow()
        self.serial, self.camera = FakeSerial(), FakeCamera()
        w = self.window
        w._serial_thread, w.camera_thread = self.serial, self.camera
        w._serial_active = w._camera_ready = True
        self.serial.data_ready.connect(w._handle_new_serial_data)
        self.serial.command_sent.connect(w._on_command_sent)
        self.camera.timing_ready.connect(w._on_camera_armed)
        self.camera.timing_failed.connect(w._on_camera_arm_failed)
        self.camera.preview_ready.connect(w._on_preview_ready)
        w._refresh_recording_button_states()

    def tearDown(self):
        w = self.window
        if w._recording_state in ("preparing", "recording"):
            w._on_stop_recording()
        wait_until(lambda: w._recorder_thread is None)
        if w._completion_dialog:
            w._completion_dialog.accept()
        w.close()
        APP.processEvents()

    def prepare(self):
        before = len(self.camera.requests)
        self.window._on_start_recording()
        wait_until(lambda: len(self.camera.requests) > before)

    def arm(self):
        run_id = self.window._run_id
        self.camera.timing_ready.emit(run_id, {"trigger_source": "Line1", "camera_operating_fps": 75,
                                                "exposure_us": 10000})
        wait_until(lambda: self.window._recording_state == "recording")
        return run_id

    def test_no_start_until_outputs_and_camera_are_ready(self):
        self.prepare()
        self.assertEqual(self.serial.commands, [])
        self.assertTrue(list(Path(self.temp.name).glob("*pressure.partial.csv")))
        self.assertTrue(list(Path(self.temp.name).glob("*video.partial.tif")))
        self.arm()
        self.assertEqual(self.serial.commands, ["<Z, 100, 1>", "<G, 100, 1>"])
        self.assertFalse(self.window.camera_control_panel.sampling_spin.isEnabled())
        self.assertFalse(self.window.top_ctrl.zero_btn.isEnabled())

    def test_arm_failure_never_sends_g(self):
        self.prepare()
        self.camera.timing_failed.emit(self.window._run_id, "Exposure too long")
        wait_until(lambda: self.window._completion_dialog is not None)
        self.assertEqual(self.serial.commands, [])
        self.assertIn("Incomplete", self.window._completion_dialog.heading.text())

    def test_micro_manager_without_rate_or_native_ids_records_after_arming(self):
        self.prepare()
        self.assertEqual(self.serial.commands, [])
        run_id = self.window._run_id
        self.camera.timing_ready.emit(run_id, {
            "camera_backend": "micromanager", "timing_mode": "external_trigger",
            "trigger_source": "Configured external input", "camera_operating_fps": None,
            "exposure_us": 10000, "hardware_metadata_required": False,
            "metadata_limitations": "Hardware frame IDs are not exposed by this adapter."})
        wait_until(lambda: self.window._recording_state == "recording")
        self.assertEqual(self.serial.commands, ["<Z, 100, 1>", "<G, 100, 1>"])
        image = QImage(5, 3, QImage.Format_Grayscale8)
        image.fill(20)
        self.serial.data_ready.emit(1, .1, 10)
        self.camera.recording_frame_ready.emit(image, {"run_id": run_id,
            "camera_frame_id": None, "camera_timestamp_raw": None})
        wait_until(lambda: self.window._recorder_worker._frames_written == 1)
        self.window._on_stop_recording()
        wait_until(lambda: self.window._completion_dialog is not None)
        self.assertEqual(self.window._completion_dialog.heading.text(), "Recording saved")
        self.assertEqual(self.serial.commands[-1], "<S, 100, 1>")

    def test_cancel_ignores_late_camera_ready(self):
        self.prepare()
        run_id = self.window._run_id
        self.window._on_stop_recording()
        self.camera.timing_ready.emit(run_id, {"trigger_source": "Line1", "camera_operating_fps": 75, "exposure_us": 10000})
        wait_until(lambda: self.window._completion_dialog is not None)
        self.assertEqual(self.serial.commands, [])

    def test_completion_after_file_close_and_repeated_stop_sends_one_s(self):
        self.prepare()
        run_id = self.arm()
        self.serial.data_ready.emit(1, 0, 10)
        image = QImage(5, 3, QImage.Format_Grayscale8)
        image.fill(25)
        self.camera.recording_frame_ready.emit(image, {"run_id": run_id, "camera_frame_id": 100,
                                                       "camera_timestamp_raw": 20000})
        wait_until(lambda: self.window._recorder_worker._frames_written == 1)
        self.window._on_stop_recording()
        self.window._on_stop_recording()
        self.assertIsNone(self.window._completion_dialog)
        wait_until(lambda: self.window._completion_dialog is not None)
        self.assertEqual(self.serial.commands, ["<Z, 100, 1>", "<G, 100, 1>", "<S, 100, 1>"])
        self.assertEqual(self.window._completion_dialog.heading.text(), "Recording saved")
        self.assertTrue(Path(self.window._last_recording_paths["tiff"]).exists())
        self.assertIsNone(self.window._recorder_thread)

    def test_disconnected_status_does_not_enable_recording(self):
        self.window._handle_serial_status_change("Disconnected")
        self.assertFalse(self.window.start_recording_action.isEnabled())
        self.assertFalse(self.window.top_ctrl.zero_btn.isEnabled())

    def test_two_runs_restore_preview_and_use_distinct_outputs(self):
        paths = []
        for i in range(2):
            self.prepare()
            run_id = self.arm()
            self.serial.data_ready.emit(1, 0, 10)
            image = QImage(5, 3, QImage.Format_Grayscale8)
            image.fill(20)
            self.camera.recording_frame_ready.emit(image, {"run_id": run_id,
                "camera_frame_id": i + 100, "camera_timestamp_raw": (i + 1) * 100000})
            wait_until(lambda: self.window._recorder_worker._frames_written == 1)
            self.window._on_stop_recording()
            wait_until(lambda: self.window._completion_dialog is not None)
            self.assertEqual(self.window._completion_dialog.heading.text(), "Recording saved")
            paths.append(self.window._last_recording_paths["tiff"])
            self.window._completion_dialog.accept()
            self.assertTrue(self.window._camera_ready)
        self.assertNotEqual(*paths)
        self.assertEqual(self.serial.commands, ["<Z, 100, 1>", "<G, 100, 1>", "<S, 100, 1>"] * 2)

    def test_csv_only_needs_no_camera_and_preserves_pressure(self):
        w = self.window
        w._camera_ready = False
        w.camera_control_panel.capture_setting_combo.setCurrentIndex(0)
        w._on_start_recording()
        wait_until(lambda: w._recording_state == "recording")
        self.serial.data_ready.emit(0, 0, 10)
        wait_until(lambda: w._recorder_worker._samples_written == 1)
        w._on_stop_recording()
        wait_until(lambda: w._completion_dialog is not None)
        self.assertEqual(w._completion_dialog.heading.text(), "Recording saved")
        self.assertEqual(self.camera.requests, [])
        self.assertIsNone(w._last_recording_paths["tiff"])
        self.assertEqual(self.serial.commands, ["<Z, 100, 0>", "<G, 100, 0>", "<S, 100, 0>"])

    def test_start_waits_for_zero_write_and_cancel_prevents_g(self):
        self.serial.auto_ack = False
        self.prepare()
        self.camera.timing_ready.emit(self.window._run_id, {
            "trigger_source": "Line1", "camera_operating_fps": 75, "exposure_us": 10000})
        wait_until(lambda: self.serial.commands == ["<Z, 100, 1>"])
        self.assertEqual(self.window._recording_state, "preparing")
        self.window._on_stop_recording()
        self.serial.command_sent.emit("<Z, 100, 1>")
        wait_until(lambda: self.window._completion_dialog is not None)
        self.assertEqual(self.serial.commands, ["<Z, 100, 1>"])

    def test_failed_zero_never_starts_pump(self):
        self.serial.reject_commands.add("<Z, 100, 1>")
        self.prepare()
        self.camera.timing_ready.emit(self.window._run_id, {
            "trigger_source": "Line1", "camera_operating_fps": 75, "exposure_us": 10000})
        wait_until(lambda: self.window._completion_dialog is not None)
        self.assertEqual(self.serial.commands, ["<Z, 100, 1>"])
        self.assertIn("Incomplete", self.window._completion_dialog.heading.text())

    def test_plot_retained_after_stop_cleared_before_next_run_and_csv_time_unchanged(self):
        w = self.window
        plot = w.pressure_plot_widget
        w._serial_counter = 461  # The device is zeroed on each start.
        for run in range(2):
            self.prepare()
            self.assertEqual(plot.times, [])
            self.assertEqual(plot.pressures, [])
            self.assertEqual(list(plot.line.get_xdata()), [])
            self.assertEqual(plot.ax.get_xlim()[0], 0)
            self.assertIsNone(plot.manual_xlim)
            self.assertTrue(plot.scrollbar.isHidden())
            self.assertFalse(plot.hover_annotation.get_visible())
            run_id = self.arm()
            for number, stamp in enumerate((0.0, 0.1), 1):
                self.serial.data_ready.emit(number, stamp, 10 + run + number)
                image = QImage(5, 3, QImage.Format_Grayscale8)
                image.fill(20)
                self.camera.recording_frame_ready.emit(image, {
                    "run_id": run_id, "camera_frame_id": 100 + number,
                    "camera_timestamp_raw": number * 100000})
            wait_until(lambda: w._recorder_worker._frames_written == 2)
            w._on_stop_recording()
            wait_until(lambda: w._completion_dialog is not None)
            self.assertEqual(w._completion_dialog.heading.text(), "Recording saved")
            self.assertEqual(plot.times, [0.0, 0.1])
            self.assertEqual(plot.pressures, [11 + run, 12 + run])
            with open(w._last_recording_paths["csv"], newline="") as handle:
                rows = list(csv.DictReader(iter_csv_data_lines(handle)))
            self.assertEqual([float(row["deviceTime"]) for row in rows], plot.times)
            self.assertEqual([int(row["frameIdx"]) for row in rows], [1, 2])
            w._completion_dialog.accept()
            self.serial.data_ready.emit(999, 999.0, 999.0)
            self.assertEqual(plot.times, [0.0, 0.1])  # Idle status cannot replace it.
            plot.set_manual_x_limits(0, 0.05)
            plot.hover_annotation.set_visible(True)
            w._last_serial_activity = time.monotonic() - 1

    def test_preview_rate_change_disables_recording_until_a_new_image(self):
        w = self.window
        w._set_camera_setting("AcquisitionFrameRate", 10)
        self.assertFalse(w._camera_ready)
        self.assertFalse(w.start_recording_action.isEnabled())
        self.assertEqual(self.camera.requests, [("AcquisitionFrameRate", 10)])
        self.camera.preview_ready.emit()
        self.assertTrue(w.start_recording_action.isEnabled())

    def test_csv_only_plot_keeps_trailing_samples_during_stop_drain(self):
        w = self.window
        w.camera_control_panel.capture_setting_combo.setCurrentIndex(0)
        w._on_start_recording()
        wait_until(lambda: w._recording_state == "recording")
        self.serial.data_ready.emit(0, 0, 10)
        w._on_stop_recording()
        wait_until(lambda: w._recording_state == "finalizing")
        self.serial.data_ready.emit(0, .1, 11)
        wait_until(lambda: w._completion_dialog is not None)
        self.assertEqual(w.pressure_plot_widget.times, [0, .1])
        self.assertEqual(w.pressure_plot_widget.pressures, [10, 11])
        self.assertEqual(w._completion_dialog.heading.text(), "Recording saved")

    def test_reset_during_acquisition_still_marks_recording_incomplete(self):
        w = self.window
        self.prepare()
        self.arm()
        self.serial.data_ready.emit(1, .1, 10)
        self.serial.data_ready.emit(0, 0, 11)
        wait_until(lambda: w._completion_dialog is not None)
        self.assertIn("Incomplete", w._completion_dialog.heading.text())

    def test_close_during_recording_waits_for_saved_files(self):
        w = self.window
        self.prepare()
        run_id = self.arm()
        self.serial.data_ready.emit(1, 0, 10)
        image = QImage(5, 3, QImage.Format_Grayscale8)
        image.fill(20)
        self.camera.recording_frame_ready.emit(image, {"run_id": run_id,
            "camera_frame_id": 100, "camera_timestamp_raw": 100000})
        wait_until(lambda: w._recorder_worker._frames_written == 1)
        w.close()
        wait_until(lambda: w._completion_dialog is not None)
        self.assertTrue(w._closing)
        self.assertEqual(w._completion_dialog.heading.text(), "Recording saved")
        self.assertTrue(Path(w._last_recording_paths["tiff"]).exists())
        self.assertEqual(self.serial.commands.count("<S, 100, 1>"), 1)

    def test_camera_readback_never_emits_setting_writes(self):
        panel = CameraControlPanel()
        writes = []
        panel.setting_requested.connect(lambda *args: writes.append(args))
        panel.apply_settings({"ExposureTime": {"value": 10000, "minimum": 20, "maximum": 4000000},
                              "ExposureAuto": {"value": "Off"},
                              "AcquisitionFrameRate": {"value": 75, "minimum": 1, "maximum": 75},
                              "PixelFormat": {"value": "Mono8", "choices": ["Mono8", "Mono16"]}})
        self.assertEqual(writes, [])
        panel.sampling_spin.setValue(20)
        self.assertEqual(writes, [])
        self.assertEqual(panel.framerate_spin.value(), 75)
        panel.deleteLater()
