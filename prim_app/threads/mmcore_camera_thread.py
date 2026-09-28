"""PRIMA recording lifecycle over BURST's isolated Micro-Manager connection."""
import logging
import math
import queue
import time
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QImage
from cameras.micro_manager_process import MicroManagerClient, MicroManagerCancelled
from cameras.pixels import copy_mm_frame
from utils.camera_access import require_no_other_prima

log = logging.getLogger(__name__)
CONTROL_NAMES = {"ExposureTime": "exposure", "Gain": "gain", "AcquisitionFrameRate": "fps",
                 "ExposureAuto": "auto_exposure", "GainAuto": "auto_gain", "PixelFormat": "pixel_format"}


def control_snapshot(snapshot):
    result = {}
    for name, key in CONTROL_NAMES.items():
        control = snapshot.get("controls", {}).get(key)
        if control is None:
            continue
        result[name] = {"value": control.value, "minimum": control.minimum,
                        "maximum": control.maximum, "unit": control.unit,
                        "limits_known": control.limits_known, "writable": control.writable,
                        "choices": list(control.choices)}
    return result


class MMCoreCameraThread(QThread):
    grabber_ready = pyqtSignal()
    frame_ready = pyqtSignal(QImage, object)
    recording_frame_ready = pyqtSignal(QImage, object)
    error = pyqtSignal(str, str)
    settings_ready = pyqtSignal(object)
    timing_ready = pyqtSignal(str, object)
    timing_failed = pyqtSignal(str, str)
    preview_ready = pyqtSignal()
    control_error = pyqtSignal(str)

    def __init__(self, parent=None, *, sdk=None, profile, client_factory=MicroManagerClient):
        super().__init__(parent)
        self.profile = dict(profile)
        self.client_factory = client_factory
        self._commands = queue.Queue()
        self._stop_requested = False
        self._run_id = None
        self._preview_announced = False
        self._last_frame = self._last_emit = 0.0
        self._preview_timeout_s = 6.0
        self.capture_statistics = []

    def set_resolution(self, resolution):
        # Geometry is read from the saved Micro-Manager connection at open.
        pass

    def request_setting(self, name, value):
        self._commands.put(("setting", (name, value)))

    def request_recording(self, run_id, interval_s):
        self._commands.put(("record", (run_id, interval_s)))

    def request_preview(self):
        self._commands.put(("preview", None))

    def stop(self):
        self._stop_requested = True

    def _snapshot(self, snapshot):
        settings = control_snapshot(snapshot)
        periods = [2.0]
        rate = settings.get("AcquisitionFrameRate", {}).get("value")
        exposure = settings.get("ExposureTime", {}).get("value")
        if rate is not None and math.isfinite(float(rate)) and float(rate) > 0:
            periods.append(1 / float(rate))
        if exposure is not None and math.isfinite(float(exposure)) and float(exposure) > 0:
            periods.append(float(exposure) / 1e6)
        self._preview_timeout_s = 3 * max(periods)
        self.settings_ready.emit(settings)

    def _preview(self, client):
        # Drop the run identity before transition-generated images can arrive.
        self._run_id = None
        self._preview_announced = False
        self._snapshot(client.request("restore_preview", timeout=10))
        self._last_frame = time.monotonic()

    def _record(self, client, run_id, interval_s):
        try:
            details = client.request("prepare_recording", interval_s, timeout=10)
            self._run_id = run_id
            details.update(model=self.profile.get("display_name", self.profile.get("camera", "Micro-Manager")),
                           serial=self.profile.get("serial"))
            self.timing_ready.emit(run_id, details)
        except Exception as exc:
            self.timing_failed.emit(run_id, str(exc))
            self._preview(client)

    def _setting(self, client, name, value):
        if self._run_id is not None:
            self.control_error.emit("Camera settings are locked until recording finishes")
            return
        if name not in CONTROL_NAMES:
            self.control_error.emit(f"Unsupported camera setting: {name}")
            return
        self._preview_announced = False
        try:
            client.request("set", CONTROL_NAMES[name], value, timeout=10)
        except Exception as exc:
            self.control_error.emit(str(exc))
        self._snapshot(client.request("snapshot", timeout=10))
        self._last_frame = time.monotonic()

    def _deliver_frame(self, payload):
        frame, components, bit_depth, tags = payload
        image, pixels, pixel_format = copy_mm_frame(frame, components, bit_depth)
        now = time.monotonic()
        metadata = {"run_id": self._run_id, "camera_frame_id": None, "camera_timestamp_raw": None,
                    "host_monotonic": now, "camera_backend": "micromanager",
                    "source_bit_depth": bit_depth, "pixel_format": pixel_format,
                    "camera_metadata": tags}
        if self._run_id is not None:
            self.recording_frame_ready.emit(image, {**metadata, "_pixels": pixels})
        else:
            self._last_frame = now
            if not self._preview_announced:
                self._preview_announced = True
                self.grabber_ready.emit()
                self.preview_ready.emit()
        if now - self._last_emit >= .05:
            self._last_emit = now
            self.frame_ready.emit(image, metadata)

    def run(self):
        try:
            require_no_other_prima()
            with self.client_factory(cancelled=lambda: self._stop_requested) as client:
                self._snapshot(client.request("open", self.profile, "", timeout=30))
                self._last_frame = time.monotonic()
                next_snapshot = self._last_frame + .5
                try:
                    while not self._stop_requested:
                        try:
                            command, value = self._commands.get_nowait()
                        except queue.Empty:
                            command = None
                        if command == "record":
                            self._record(client, *value)
                        elif command == "preview":
                            self._preview(client)
                        elif command == "setting":
                            self._setting(client, *value)
                        payload = client.request("next", timeout=10)
                        if payload is not None:
                            self._deliver_frame(payload)
                        elif self._run_id is None and time.monotonic() - self._last_frame > self._preview_timeout_s:
                            raise RuntimeError("Camera preview is not delivering images. Verify Live in Micro-Manager, close it, then reopen this camera in PRIMA.")
                        if self._run_id is None and time.monotonic() >= next_snapshot:
                            self._snapshot(client.request("snapshot", timeout=10))
                            next_snapshot = time.monotonic() + .5
                        if payload is None:
                            self.msleep(5)
                finally:
                    if self._run_id is not None:
                        # Cleanup is handled in the helper even after cancellation.
                        self._run_id = None
        except MicroManagerCancelled:
            pass
        except Exception as exc:
            log.exception("Micro-Manager acquisition failed")
            if not self._stop_requested:
                self.error.emit(str(exc), "micromanager")
