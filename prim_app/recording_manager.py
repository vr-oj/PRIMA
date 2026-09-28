"""Preserve video and pressure with bounded matching and explicit finalization."""
import csv
import json
import math
import os
import queue
import shutil
import time
from collections import deque
from datetime import datetime

import numpy as np
import tifffile
from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QImage

from utils.config import MIN_FREE_SPACE_GB
from utils.recording_csv import write_recording_csv_metadata_and_header, iter_csv_data_lines
from utils.recording_settings import DEFAULT_CAPTURE_SETTING_CODE, capture_setting_label as capture_label


class RecordingManager(QObject):
    ready_for_acquisition = pyqtSignal()
    activated = pyqtSignal()
    finished = pyqtSignal()
    finalized = pyqtSignal(object)
    progress = pyqtSignal(int, int)
    error_occurred = pyqtSignal(str)

    def __init__(self, output_dir, recording_fps=None, frame_interval_ms=None,
                 capture_setting_code=DEFAULT_CAPTURE_SETTING_CODE, capture_setting_label=None,
                 record_video=True, parent=None, run_id=None, initial_counter=None):
        super().__init__(parent)
        self.output_dir = output_dir
        self.recording_fps = recording_fps
        self.frame_interval_ms = frame_interval_ms or 100
        self.capture_setting_code = int(capture_setting_code)
        self.capture_setting_label = capture_setting_label or capture_label(capture_setting_code)
        self.record_video = bool(record_video)
        self.run_id = run_id
        self._counter = initial_counter
        self._first_time = self._last_time = None
        self._time_reset = False
        self._samples_written = self._frames_written = self._captures_expected = 0
        self._frames_received = 0
        self._last_camera_id = self._last_camera_time = None
        self._pending_samples, self._pending_images = deque(), deque()
        self._frame_queue = queue.Queue(maxsize=64)
        self._queue_warning = False
        self._queue_overflow = False
        self._frames_overflowed = 0
        self._samples_since_capture = 0
        self._writer_failed = False
        self._associations = {}
        self._issues = []
        self._accepting = self.is_recording = False
        self._finalized = False
        self._stop_at = None
        self._activated_at = self._last_activity = time.monotonic()
        self._last_flush = self._last_activity
        self._timer = None
        self.csv_file = self.csv_writer = self.tif_writer = self._tif_file = None
        self._csv_path = self._tiff_path = self._journal_path = self._partial_tiff_path = self._summary_path = None
        self.camera_details = {}

    def _issue(self, message, notify=True):
        if message not in self._issues:
            self._issues.append(message)
            if notify:
                self.error_occurred.emit(message)

    @pyqtSlot()
    def start_recording(self):
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            if shutil.disk_usage(self.output_dir).free < MIN_FREE_SPACE_GB * 1024 ** 3:
                raise RuntimeError("Not enough free disk space for recording")
            base = os.path.join(self.output_dir, datetime.now().strftime("recording_%Y-%m-%d_%H-%M-%S_%f"))
            self._csv_path, self._journal_path = base + "_pressure.csv", base + "_pressure.partial.csv"
            self._summary_path = base + "_summary.json"
            self._tiff_path = base + "_video.tif" if self.record_video else None
            self._partial_tiff_path = base + "_video.partial.tif" if self.record_video else None
            self.csv_file = open(self._journal_path, "x", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
            self._write_header(self.csv_writer)
            self.csv_file.flush()
            if self.record_video:
                self._tif_file = open(self._partial_tiff_path, "xb")
                self.tif_writer = tifffile.TiffWriter(self._tif_file, bigtiff=True)
            self._write_summary({"status": "preparing", "complete": False,
                                 "csv_journal": self._journal_path, "partial_tiff": self._partial_tiff_path})
            self.is_recording = True
            self._timer = QTimer(self)
            self._timer.setInterval(20)
            self._timer.timeout.connect(self._tick)
            self._timer.start()
            self.ready_for_acquisition.emit()
        except Exception as exc:
            self._issue("Recording preparation failed: " + str(exc))
            self.stop_recording()

    @pyqtSlot(object)
    def activate(self, camera_details):
        if not self.is_recording or self._stop_at is not None:
            return
        self.camera_details = dict(camera_details or {})
        self._accepting = True
        self._activated_at = self._last_activity = time.monotonic()
        self.activated.emit()

    @pyqtSlot(str)
    def mark_incomplete(self, message):
        self._issue(message, notify=False)

    def _selected(self, counter):
        previous, self._counter = self._counter, counter
        if not self.record_video:
            return False
        if previous is None:
            if self.capture_setting_code == 1:
                return True
            if counter == 0:
                return False
            self._issue("Sparse capture began without a known trigger-counter baseline; image associations are uncertain")
            return False
        delta = counter - previous
        if delta not in (0, 1) or (self.capture_setting_code == 1 and delta != 1):
            self._issue(f"Unexpected PRIM trigger-counter change: {previous} to {counter}")
            return False
        return delta == 1

    @pyqtSlot(int, float, float)
    def append_pressure(self, counter, device_time, pressure):
        if not self.is_recording or not self._accepting:
            return
        now = time.monotonic()
        self._last_activity = now
        try:
            # Journal every pressure row immediately; references are added only
            # to the final CSV after successful image writes and run checks.
            self.csv_writer.writerow([counter, device_time, pressure, ""])
            self._samples_written += 1
            if not math.isfinite(device_time) or not math.isfinite(pressure):
                self._issue("Non-finite pressure or device time received")
            if self._first_time is None:
                self._first_time = device_time
            if self._last_time is not None and device_time < self._last_time:
                self._time_reset = True
                self._issue("Device time reset during this recording")
            self._last_time = device_time
            self._samples_since_capture += 1
            if self._selected(counter):
                self._samples_since_capture = 0
                self._captures_expected += 1
                if not self._issues:
                    self._pending_samples.append((self._samples_written, counter, device_time, pressure, now))
            elif self.record_video and self._samples_since_capture >= self.capture_setting_code:
                self._issue("PRIM trigger counter did not advance at the selected capture interval")
            self._match()
        except Exception as exc:
            self._issue("Pressure write failed: " + str(exc))

    @pyqtSlot(QImage, object)
    def enqueue_frame(self, image, metadata):
        """Direct camera callback: bounded, thread-safe handoff without disk I/O."""
        if not isinstance(metadata, dict) or metadata.get("run_id") != self.run_id or self._finalized:
            return
        try:
            self._frame_queue.put_nowait((image, metadata))
            if self._frame_queue.qsize() >= 32 and not self._queue_warning:
                self._queue_warning = True
                self.error_occurred.emit("Video writer is falling behind; stopping while buffer capacity remains")
        except queue.Full:
            self._frames_overflowed += 1
            if not self._queue_overflow:
                self._queue_overflow = True
                self.error_occurred.emit("Video writer cannot keep up; camera buffer limit reached")

    def _drain_frames(self):
        for _ in range(64):
            try:
                image, metadata = self._frame_queue.get_nowait()
            except queue.Empty:
                break
            self.append_frame(image, metadata)
        if self._queue_overflow:
            self._issue("Video buffer overflow; some received images could not be saved")
        elif self._queue_warning:
            self._issue("Video writer fell behind; recording stopped before the buffer filled")

    @pyqtSlot(QImage, object)
    def append_frame(self, image, metadata):
        if not self.is_recording or not self._accepting or not self.record_video:
            return
        if not isinstance(metadata, dict) or metadata.get("run_id") != self.run_id:
            return  # Preview and previous-run frames cannot enter this run.
        self._last_activity = time.monotonic()
        self._frames_received += 1
        frame_id, timestamp = metadata.get("camera_frame_id"), metadata.get("camera_timestamp_raw")
        metadata_optional = (self.camera_details.get("camera_backend") == "micromanager"
                             and self.camera_details.get("timing_mode") == "external_trigger"
                             and self.camera_details.get("hardware_metadata_required") is False)
        if not metadata_optional and (frame_id is None or timestamp is None):
            self._issue("Camera frame metadata is missing")
        if frame_id is not None and self._last_camera_id is not None and frame_id != self._last_camera_id + 1:
            self._issue("Camera frame-ID discontinuity; later associations were stopped")
        if timestamp is not None and self._last_camera_time is not None and timestamp <= self._last_camera_time:
            self._issue("Camera timestamp reset or repeated")
        self._last_camera_id, self._last_camera_time = frame_id, timestamp
        if self._issues:
            self._preserve_unmatched()
            self._write_image(image, metadata)
            return
        if len(self._pending_images) >= 32:
            self._issue("Camera/pressure matching queue exceeded its limit")
            self._preserve_unmatched()
            self._write_image(image, metadata)
            return
        self._pending_images.append((image.copy(), dict(metadata), time.monotonic()))
        self._match()

    def _match(self):
        while self._pending_samples and self._pending_images and not self._issues:
            ordinal, counter, device_time, pressure, _ = self._pending_samples[0]
            image, metadata, _ = self._pending_images[0]
            if not self._write_image(image, metadata, (counter, device_time, pressure)):
                return
            self._associations[ordinal] = self._frames_written
            self._pending_samples.popleft()
            self._pending_images.popleft()

    def _write_image(self, image, metadata, sample=None):
        if self._writer_failed or self.tif_writer is None:
            return False
        counter, device_time, pressure = sample if sample is not None else (None, None, None)
        description = {"frameIdx": counter, "deviceTime": device_time, "pressure": pressure,
            "tiffFrame": self._frames_written + 1, **{key: value for key, value in metadata.items() if key != "_pixels"},
            "association_status": "candidate; valid only if run summary is complete" if sample else "unmatched"}
        try:
            pixels = metadata.get("_pixels")
            if pixels is None:
                pixels = self._qimage_to_numpy(image)
            else:
                pixels = np.asarray(pixels)
                if not ((pixels.ndim == 2 and pixels.dtype in (np.dtype("uint8"), np.dtype("uint16")))
                        or (pixels.ndim == 3 and pixels.shape[2] == 3 and pixels.dtype == np.uint8)):
                    raise ValueError("Unsupported native recording pixels")
                if pixels.shape[:2] != (image.height(), image.width()):
                    raise ValueError("Native recording pixels do not match the preview dimensions")
            self.tif_writer.write(pixels, photometric="rgb" if pixels.ndim == 3 else "minisblack",
                                  description=json.dumps(description))
        except Exception as exc:
            self._writer_failed = True
            self._issue("Image write failed: " + str(exc))
            return False
        self._frames_written += 1
        return True

    def _preserve_unmatched(self):
        """Pressure ambiguity must not discard otherwise writable camera images."""
        while self._pending_images:
            image, metadata, _ = self._pending_images.popleft()
            self._write_image(image, metadata)

    @pyqtSlot()
    def request_stop(self):
        if not self.is_recording:
            self.stop_recording()
        elif not self._accepting:
            self.stop_recording()
        elif self._stop_at is None:
            self._stop_at = time.monotonic()

    def _tick(self):
        now = time.monotonic()
        if not self.is_recording:
            return
        self._drain_frames()
        if now - self._last_flush >= 1.0:
            try:
                # Keep the same headroom used at preparation, including space
                # for images in flight and file finalization.
                if shutil.disk_usage(self.output_dir).free < MIN_FREE_SPACE_GB * 1024 ** 3:
                    self._issue("Free disk space reached the recording reserve; stopping before the disk fills")
                self.csv_file.flush()
                os.fsync(self.csv_file.fileno())
                if self._tif_file is not None and not self._writer_failed:
                    self._tif_file.flush()
                    os.fsync(self._tif_file.fileno())
            except Exception as exc:
                self._issue("Recording flush failed: " + str(exc))
            self._last_flush = now
        self.progress.emit(self._samples_written, self._frames_written)
        if self._stop_at is not None:
            # Wait through a quiet period for data already in flight after S.
            quiet = max(0.5, 2 * self.frame_interval_ms / 1000.0,
                        self.camera_details.get("exposure_us", 0) / 1_000_000.0 + 0.25)
            quiet = min(quiet, 3.0)
            if now - self._stop_at >= 5.0:
                if now - self._last_activity < quiet:
                    self._issue("Stop deadline reached while data was still arriving", notify=False)
                self.stop_recording()
            elif now - max(self._stop_at, self._last_activity) >= quiet:
                self.stop_recording()
        elif self._accepting and not self._issues:
            timeout = max(3.0, 3 * self.frame_interval_ms / 1000.0)
            if now - self._last_activity > timeout:
                self._issue("No acquisition data arrived within the expected interval")
            for pending in (self._pending_samples, self._pending_images):
                if pending and now - pending[0][-1] > max(2.0, self.camera_details.get("exposure_us", 0) / 1e6 + 1.0):
                    self._issue("A camera frame and its pressure trigger could not be matched before the deadline")
                    break

    def _write_header(self, writer):
        write_recording_csv_metadata_and_header(writer, self.recording_fps, self.frame_interval_ms,
                                                self.capture_setting_label, self.capture_setting_code)

    def _write_summary(self, summary):
        if self._summary_path:
            temp = self._summary_path + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(summary, handle, indent=2)
                handle.write("\n")
            os.replace(temp, self._summary_path)

    def _publish_csv(self, include_associations):
        temp = self._csv_path + ".tmp"
        with open(self._journal_path, newline="", encoding="utf-8") as source, open(temp, "x", newline="", encoding="utf-8") as target:
            writer = csv.writer(target)
            self._write_header(writer)
            for ordinal, row in enumerate(csv.DictReader(iter_csv_data_lines(source)), 1):
                writer.writerow([row["frameIdx"], row["deviceTime"], row["pressure"],
                                 self._associations.get(ordinal, "") if include_associations else ""])
        os.replace(temp, self._csv_path)

    @pyqtSlot()
    def stop_recording(self):
        if self._finalized:
            return
        self._drain_frames()
        self._finalized = True
        self.is_recording = self._accepting = False
        if self._timer:
            self._timer.stop()
        if self._samples_written == 0:
            self._issue("No pressure samples were recorded", notify=False)
        if self.record_video and self._captures_expected != self._frames_received:
            self._issue(f"Requested {self._captures_expected} images; received {self._frames_received}", notify=False)
        if self._pending_samples or self._pending_images:
            self._issue("Unmatched pressure triggers or camera images remain", notify=False)
        self._preserve_unmatched()
        if self._frames_received != self._frames_written:
            self._issue(f"Received {self._frames_received} images; saved {self._frames_written}", notify=False)
        for name, handle in (("TIFF", self.tif_writer), ("TIFF file", self._tif_file), ("CSV", self.csv_file)):
            if handle is not None:
                try:
                    handle.close()
                except Exception as exc:
                    self._issue(f"Could not close {name}: {exc}", notify=False)
        self.tif_writer = self._tif_file = self.csv_file = self.csv_writer = None
        video_path = self._partial_tiff_path
        try:
            if self.record_video and not self._issues:
                os.replace(self._partial_tiff_path, self._tiff_path)
                video_path = self._tiff_path
            if self._journal_path and os.path.exists(self._journal_path):
                self._publish_csv(not self._issues)
        except Exception as exc:
            self._issue("Could not finalize output files: " + str(exc), notify=False)
        # If publication failed after renaming the TIFF, leave it visibly partial.
        if self._issues and video_path == self._tiff_path and self._tiff_path:
            try:
                os.replace(self._tiff_path, self._partial_tiff_path)
                video_path = self._partial_tiff_path
            except OSError as exc:
                self._issue("Could not mark TIFF as partial: " + str(exc), notify=False)
        duration = None if self._time_reset or self._first_time is None else max(0.0, self._last_time - self._first_time)
        existing = lambda path: path if path and os.path.exists(path) else None
        csv_path = existing(self._csv_path) or existing(self._journal_path)
        video_path = existing(video_path)
        def file_size(path):
            try:
                return os.path.getsize(path) if path else 0
            except OSError as exc:
                self._issue("Could not inspect saved file size: " + str(exc), notify=False)
                return None
        csv_size, video_size = file_size(csv_path), file_size(video_path)
        summary = {"status": "complete" if not self._issues else "incomplete", "complete": not self._issues,
            "output_dir": self.output_dir, "record_video": self.record_video,
            "samples_written": self._samples_written, "frames_written": self._frames_written,
            "frames_received": self._frames_received, "captures_expected": self._captures_expected,
            "frames_overflowed": self._frames_overflowed,
            "duration_s": duration, "issues": list(self._issues), "csv_path": csv_path,
            "tiff_path": video_path, "csv_journal": existing(self._journal_path),
            "csv_size_bytes": csv_size, "tiff_size_bytes": video_size,
            "summary_path": self._summary_path,
            "camera": self.camera_details, "capture_setting_code": self.capture_setting_code,
            "association_checks": "count/order consistency; physical pressure-to-exposure offset unmeasured"}
        try:
            self._write_summary(summary)
        except Exception as exc:
            summary["complete"] = False
            summary["status"] = "incomplete"
            summary["issues"].append("Could not save summary: " + str(exc))
        self._pending_images.clear()
        self._pending_samples.clear()
        self.finalized.emit(summary)
        self.finished.emit()

    @staticmethod
    def _qimage_to_numpy(image):
        gray = image.convertToFormat(QImage.Format_Grayscale8)
        ptr = gray.constBits()
        ptr.setsize(gray.byteCount())
        return np.frombuffer(ptr, np.uint8).reshape(gray.height(), gray.bytesPerLine())[:, :gray.width()].copy()
