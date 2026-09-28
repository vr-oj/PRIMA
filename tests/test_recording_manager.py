import csv
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from qt_support import APP
import numpy as np
import tifffile
from PyQt5.QtGui import QImage
from recording_manager import RecordingManager
from utils.recording_csv import iter_csv_data_lines, load_pressure_values


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.summaries = []
        self.ready = []
        self.finished = []
        self.workers = []

    def recorder(self, code=1, baseline=None):
        worker = RecordingManager(self.temp.name, 10, 100, code, record_video=code != 0,
                                  run_id="test", initial_counter=baseline)
        worker.ready_for_acquisition.connect(lambda: self.ready.append(True))
        worker.finished.connect(lambda: self.finished.append(True))
        worker.finalized.connect(self.summaries.append)
        self.workers.append(worker)
        with patch("recording_manager.MIN_FREE_SPACE_GB", 0):
            worker.start_recording()
        worker.activate({"exposure_us": 10000})
        return worker

    def tearDown(self):
        for worker in self.workers:
            worker.stop_recording()

    def frame(self, worker, number=1, run_id="test", width=5):
        image = QImage(width, 3, QImage.Format_Grayscale8)
        image.fill(42)
        worker.append_frame(image, {"run_id": run_id, "camera_frame_id": number,
                                   "camera_timestamp_raw": number * 100000})

    def rows(self, summary):
        with open(summary["csv_path"], newline="") as handle:
            return list(csv.DictReader(iter_csv_data_lines(handle)))

    def test_both_arrival_orders_and_padded_images_preserve_legacy_schema(self):
        worker = self.recorder()
        self.frame(worker, 100)
        worker.append_pressure(50, 4.0, 12.5)
        worker.append_pressure(51, 4.1, 12.6)
        self.frame(worker, 101)
        worker.stop_recording()
        summary = self.summaries[-1]
        self.assertTrue(summary["complete"], summary["issues"])
        rows = self.rows(summary)
        self.assertEqual([r["tiffFrame"] for r in rows], ["1", "2"])
        self.assertEqual(load_pressure_values(summary["csv_path"]), [12.5, 12.6])
        with tifffile.TiffFile(summary["tiff_path"]) as stack:
            self.assertEqual(len(stack.pages), 2)
            for page, row in zip(stack.pages, rows):
                self.assertEqual(page.asarray().shape, (3, 5))
                self.assertTrue(np.all(page.asarray() == 42))
                meta = json.loads(page.description)
                self.assertEqual(meta["frameIdx"], int(row["frameIdx"]))
                self.assertEqual(meta["pressure"], float(row["pressure"]))

    def test_missing_image_preserves_all_pressure_and_no_trusted_references(self):
        worker = self.recorder()
        worker.append_pressure(1, 0, 11)
        worker.append_pressure(2, .1, 12)
        self.frame(worker)
        worker.stop_recording()
        summary = self.summaries[-1]
        self.assertFalse(summary["complete"])
        self.assertTrue(summary["tiff_path"].endswith(".partial.tif"))
        self.assertEqual([r["tiffFrame"] for r in self.rows(summary)], ["", ""])
        self.assertEqual(load_pressure_values(summary["csv_path"]), [11, 12])

    def test_sparse_counter_phase_not_row_modulo(self):
        worker = self.recorder(5, baseline=8)
        for ordinal, counter in enumerate([8, 9, 9, 9, 9, 9, 10]):
            worker.append_pressure(counter, ordinal / 10, ordinal)
            if ordinal in (1, 6):
                self.frame(worker, 100 + (ordinal == 6))
        worker.stop_recording()
        summary = self.summaries[-1]
        self.assertTrue(summary["complete"], summary["issues"])
        self.assertEqual([r["tiffFrame"] for r in self.rows(summary)], ["", "1", "", "", "", "", "2"])

    def test_sparse_unknown_first_counter_is_explicitly_incomplete(self):
        worker = self.recorder(5)
        worker.append_pressure(9, 20, 11)
        worker.stop_recording()
        self.assertTrue(any("baseline" in issue for issue in self.summaries[-1]["issues"]))

    def test_csv_only_does_not_create_video(self):
        worker = self.recorder(0)
        worker.append_pressure(0, 1, 5)
        worker.append_pressure(0, 1.1, 6)
        worker.stop_recording()
        summary = self.summaries[-1]
        self.assertTrue(summary["complete"])
        self.assertIsNone(summary["tiff_path"])
        self.assertFalse(list(Path(self.temp.name).glob("*.tif")))

    def test_previous_run_and_preview_frames_are_ignored(self):
        worker = self.recorder()
        self.frame(worker, run_id=None)
        self.frame(worker, run_id="old")
        worker.append_pressure(1, 0, 5)
        self.frame(worker)
        worker.stop_recording()
        self.assertTrue(self.summaries[-1]["complete"])

    def test_unmatched_camera_images_are_still_saved(self):
        worker = self.recorder()
        self.frame(worker, 1)
        self.frame(worker, 2)
        worker.append_pressure(1, 0, 5)
        worker.stop_recording()
        summary = self.summaries[-1]
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["frames_written"], 2)
        with tifffile.TiffFile(summary["tiff_path"]) as stack:
            self.assertEqual(len(stack.pages), 2)
            self.assertEqual(json.loads(stack.pages[1].description)["association_status"], "unmatched")

    def test_camera_gap_preserves_delivered_images_without_false_pairing(self):
        worker = self.recorder()
        self.frame(worker, 1)
        worker.append_pressure(1, 0, 5)
        self.frame(worker, 3)
        self.frame(worker, 4)
        worker.stop_recording()
        summary = self.summaries[-1]
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["frames_written"], 3)
        with tifffile.TiffFile(summary["tiff_path"]) as stack:
            self.assertEqual([json.loads(p.description)["camera_frame_id"] for p in stack.pages], [1, 3, 4])

    def test_failed_image_write_does_not_claim_a_page(self):
        worker = self.recorder()
        worker.append_pressure(1, 0, 5)
        with patch.object(worker.tif_writer, "write", side_effect=OSError("disk full")):
            self.frame(worker)
        worker.stop_recording()
        summary = self.summaries[-1]
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["frames_written"], 0)
        self.assertEqual(self.rows(summary)[0]["tiffFrame"], "")

    def test_reset_is_retained_and_flagged(self):
        worker = self.recorder()
        worker.append_pressure(30, 10, 3)
        self.frame(worker)
        worker.append_pressure(1, 0, 4)
        worker.stop_recording()
        summary = self.summaries[-1]
        self.assertFalse(summary["complete"])
        self.assertIsNone(summary["duration_s"])
        self.assertEqual([r["frameIdx"] for r in self.rows(summary)], ["30", "1"])

    def test_sparse_counter_stuck_after_csv_only_is_not_a_success(self):
        worker = self.recorder(5, baseline=20)
        for i in range(5):
            worker.append_pressure(20, i / 10, 5)
        worker.stop_recording()
        self.assertFalse(self.summaries[-1]["complete"])
        self.assertTrue(any("capture interval" in s for s in self.summaries[-1]["issues"]))

    def test_backlog_warns_before_full_and_preserves_buffered_video(self):
        worker = self.recorder()
        errors = []
        worker.error_occurred.connect(errors.append)
        image = QImage(5, 3, QImage.Format_Grayscale8)
        image.fill(10)
        for i in range(32):
            worker.enqueue_frame(image, {"run_id": "test", "camera_frame_id": i,
                                          "camera_timestamp_raw": (i + 1) * 100000})
        self.assertTrue(any("capacity remains" in s for s in errors))
        self.assertEqual(worker._frame_queue.maxsize - worker._frame_queue.qsize(), 32)
        worker.stop_recording()
        self.assertEqual(self.summaries[-1]["frames_written"], 32)
        self.assertEqual(self.summaries[-1]["frames_overflowed"], 0)
        with tifffile.TiffFile(self.summaries[-1]["tiff_path"]) as stack:
            self.assertEqual(len(stack.pages), 32)
            for page in stack.pages:
                self.assertTrue(np.all(page.asarray() == 10))

    def test_file_stat_error_still_finishes_and_preserves_files(self):
        worker = self.recorder(0)
        worker.append_pressure(0, 0, 5)
        with patch("recording_manager.os.path.getsize", side_effect=OSError("device unavailable")):
            worker.stop_recording()
        self.assertEqual(len(self.finished), 1)
        self.assertFalse(self.summaries[-1]["complete"])
        self.assertIsNone(self.summaries[-1]["csv_size_bytes"])

    def test_disk_reserve_is_monitored_during_acquisition(self):
        from types import SimpleNamespace
        worker = self.recorder(0)
        worker.append_pressure(0, 0, 5)
        errors = []
        worker.error_occurred.connect(errors.append)
        with patch("recording_manager.time.monotonic", return_value=time.monotonic() + 1.1), \
             patch("recording_manager.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
            worker._tick()
        self.assertTrue(any("reserve" in s for s in errors))

    def test_setup_failure_finishes_once_without_ready(self):
        worker = RecordingManager(self.temp.name)
        worker.ready_for_acquisition.connect(lambda: self.ready.append(True))
        worker.finished.connect(lambda: self.finished.append(True))
        with patch("recording_manager.shutil.disk_usage", side_effect=OSError("unavailable")):
            worker.start_recording()
        worker.stop_recording()
        self.assertEqual(self.ready, [])
        self.assertEqual(len(self.finished), 1)

    def test_close_failure_marks_incomplete(self):
        worker = self.recorder(0)
        worker.append_pressure(0, 0, 3)
        original = worker.csv_file
        class BadClose:
            def close(self):
                original.close()
                raise OSError("flush failed")
        worker.csv_file = BadClose()
        worker.stop_recording()
        self.assertFalse(self.summaries[-1]["complete"])

    def test_stop_drains_trailing_pressure_before_finalization(self):
        worker = self.recorder(0)
        worker.append_pressure(0, 0, 1)
        worker.request_stop()
        worker.append_pressure(0, .1, 2)
        self.assertEqual(self.summaries, [])
        with patch("recording_manager.time.monotonic", return_value=time.monotonic() + 1):
            worker._tick()
        self.assertEqual(self.summaries[-1]["samples_written"], 2)

    def test_continued_data_after_stop_has_a_deadline(self):
        worker = self.recorder(0)
        worker.append_pressure(0, 0, 1)
        worker.request_stop()
        later = time.monotonic() + 6
        worker._last_activity = later
        with patch("recording_manager.time.monotonic", return_value=later):
            worker._tick()
        self.assertFalse(self.summaries[-1]["complete"])
        self.assertTrue(any("deadline" in issue for issue in self.summaries[-1]["issues"]))
