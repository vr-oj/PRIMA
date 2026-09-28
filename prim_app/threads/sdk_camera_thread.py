"""IC4 acquisition and property access owned by one worker thread."""
import logging
import math
import queue
import time

import imagingcontrol4 as ic4
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QImage

from utils.camera_trigger import TriggerProfile, checked
from utils.camera_access import require_no_other_prima

log = logging.getLogger(__name__)


class PreviewRecoveryError(RuntimeError):
    """A control change failed and the previous preview could not be restored."""


class SDKCameraThread(QThread):
    grabber_ready = pyqtSignal()
    frame_ready = pyqtSignal(QImage, object)
    recording_frame_ready = pyqtSignal(QImage, object)
    error = pyqtSignal(str, str)
    settings_ready = pyqtSignal(object)
    timing_ready = pyqtSignal(str, object)
    timing_failed = pyqtSignal(str, str)
    preview_ready = pyqtSignal()
    control_error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.grabber = self._sink = self._profile = None
        self._device_info = self._resolution = None
        self._stop_requested = False
        self._streaming = False
        self._deliver = False
        self._run_id = None
        self._commands = queue.Queue()
        self._last_preview_emit = 0.0
        self._last_preview_frame = 0.0
        self._preview_timeout_s = 6.0
        self._preview_announced = self._grabber_announced = False
        self.capture_statistics = []

    def set_device_info(self, info):
        self._device_info = info

    def set_resolution(self, resolution):
        self._resolution = resolution

    def request_setting(self, name, value):
        self._commands.put(("setting", (name, value)))

    def request_recording(self, run_id, interval_s):
        self._commands.put(("record", (run_id, interval_s)))

    def request_preview(self):
        self._commands.put(("preview", None))

    def _start_stream(self):
        if self._sink is None:
            self._sink = ic4.QueueSink(self, [ic4.PixelFormat.Mono8], max_output_buffers=16)
        self.grabber.stream_setup(self._sink, setup_option=ic4.StreamSetupOption.ACQUISITION_START)
        self._streaming = True

    def _stop_stream(self):
        self._deliver = False
        if self._streaming:
            if self._run_id is not None:
                try:
                    stats = self.grabber.stream_statistics
                    sizes = self._sink.queue_sizes()
                    self.capture_statistics.append({
                        "run_id": self._run_id, "device_delivered": stats.device_delivered,
                        "device_transmission_error": stats.device_transmission_error,
                        "device_underrun": stats.device_underrun, "sink_delivered": stats.sink_delivered,
                        "sink_underrun": stats.sink_underrun, "sink_ignored": stats.sink_ignored,
                        "free_buffers": sizes.free_queue_length, "queued_buffers": sizes.output_queue_length,
                        "camera_readback": {name: node.value for name, node, value in self._profile.expected}})
                except Exception as exc:
                    self.capture_statistics.append({"run_id": self._run_id, "error": str(exc)})
            self.grabber.stream_stop()
            self._streaming = False

    def _snapshot(self):
        props = self.grabber.device_property_map
        result = {}
        for name in ("ExposureTime", "Gain", "AcquisitionFrameRate"):
            try:
                node = props.find_float(name)
                result[name] = {"value": node.value, "minimum": node.minimum,
                                "maximum": node.maximum, "unit": node.unit}
            except Exception as exc:
                result[name] = {"error": str(exc)}
        for name in ("ExposureAuto", "GainAuto", "PixelFormat"):
            try:
                node = props.find_enumeration(name)
                result[name] = {"value": node.value, "choices": [e.name for e in node.entries]}
            except Exception as exc:
                result[name] = {"error": str(exc)}
        periods = [2.0]
        rate = result.get("AcquisitionFrameRate", {}).get("value")
        exposure = result.get("ExposureTime", {}).get("value")
        if rate is not None and math.isfinite(rate) and rate > 0:
            periods.append(1.0 / rate)
        if exposure is not None and math.isfinite(exposure) and exposure > 0:
            periods.append(exposure / 1_000_000.0)
        self._preview_timeout_s = 3 * max(periods)
        self.settings_ready.emit(result)

    def _resume_preview_stream(self):
        self._preview_announced = False
        self._last_preview_frame = time.monotonic()
        self._start_stream()
        self._deliver = True

    def _preview(self):
        self._stop_stream()
        if self._profile:
            self._profile.restore()
            self._profile = None
        self._run_id = None
        checked(self.grabber.device_property_map.find_enumeration("TriggerMode"), "Off", "TriggerMode")
        self._resume_preview_stream()
        self._snapshot()

    def _record(self, run_id, interval_s):
        self._stop_stream()
        self._profile = TriggerProfile(ic4, self.grabber.device_property_map)
        try:
            details = self._profile.prepare(interval_s)
            self._run_id = run_id
            self._start_stream()
            self._profile.verify()
            self._deliver = True
            details.update(model=self._device_info.model_name, serial=self._device_info.serial)
            self.timing_ready.emit(run_id, details)
        except Exception as exc:
            self.timing_failed.emit(run_id, str(exc))
            self._preview()

    def _setting(self, name, value):
        if self._run_id is not None:
            raise RuntimeError("Camera settings are locked until recording finishes")
        props = self.grabber.device_property_map
        if name in ("AcquisitionFrameRate", "PixelFormat"):
            self._restart_preview_setting(name, value)
        elif name in ("ExposureTime", "Gain"):
            checked(props.find_float(name), float(value), name)
        elif name in ("ExposureAuto", "GainAuto"):
            checked(props.find_enumeration(name), value, name)
        else:
            raise ValueError(f"Unsupported camera control: {name}")

    def _restart_preview_setting(self, name, value):
        """Apply stream-affecting controls while stopped, restoring on failure."""
        props = self.grabber.device_property_map
        node = None
        previous = None
        try:
            self._stop_stream()
            if name == "AcquisitionFrameRate":
                node, value = props.find_float(name), float(value)
            else:
                node = props.find_enumeration(name)
            previous = node.value
            checked(node, value, name)
            self._resume_preview_stream()
        except Exception as change_error:
            try:
                self._stop_stream()
                if node is not None and previous is not None:
                    checked(node, previous, name)
                self._resume_preview_stream()
            except Exception as restore_error:
                raise PreviewRecoveryError(
                    f"Could not change {name}: {change_error}. "
                    f"Could not restore preview: {restore_error}") from restore_error
            raise RuntimeError(
                f"{name} was not applied: {change_error}. Previous preview restored.") from change_error

    def _check_preview_timeout(self):
        if time.monotonic() - self._last_preview_frame > self._preview_timeout_s:
            raise RuntimeError("Camera preview is not delivering images; recording is unavailable")

    def run(self):
        try:
            if self._device_info is None:
                raise RuntimeError("No camera selected")
            require_no_other_prima()
            self.grabber = ic4.Grabber()
            self.grabber.device_open(self._device_info)
            props = self.grabber.device_property_map
            if self._resolution:
                width, height, pixel_format = self._resolution
                checked(props.find_enumeration("PixelFormat"), pixel_format, "PixelFormat")
                checked(props.find_integer("Width"), width, "Width")
                checked(props.find_integer("Height"), height, "Height")
            checked(props.find_enumeration("AcquisitionMode"), "Continuous", "AcquisitionMode")
            self._preview()
            next_snapshot = time.monotonic() + 0.5
            while not self._stop_requested:
                try:
                    command, value = self._commands.get(timeout=0.02)
                except queue.Empty:
                    command = None
                if command == "record":
                    self._record(*value)
                elif command == "preview":
                    self._preview()
                elif command == "setting":
                    try:
                        self._setting(*value)
                    except PreviewRecoveryError:
                        raise
                    except Exception as exc:
                        self.control_error.emit(str(exc))
                    self._snapshot()
                if time.monotonic() >= next_snapshot:
                    # Controls are locked during recording; use the verified
                    # arming snapshot and avoid unnecessary SDK traffic.
                    if self._profile is None:
                        self._snapshot()
                        self._check_preview_timeout()
                    next_snapshot = time.monotonic() + 0.5
        except Exception as exc:
            log.exception("Camera acquisition failed")
            self.error.emit(str(exc), str(getattr(exc, "code", "")))
        finally:
            try:
                self._stop_stream()
                if self._profile:
                    self._profile.restore()
            except Exception as exc:
                self.error.emit("Camera cleanup: " + str(exc), "")
            finally:
                self._profile = self._sink = None
                if self.grabber and self.grabber.is_device_open:
                    try:
                        self.grabber.device_close()
                    except Exception as exc:
                        self.error.emit("Camera close: " + str(exc), "")
                self.grabber = None
                self._device_info = None
                props = None

    def frames_queued(self, sink):
        # One notification can represent several queued images.
        while True:
            try:
                buf = sink.try_pop_output_buffer()
            except Exception as exc:
                self.error.emit(str(exc), str(getattr(exc, "code", "")))
                return
            if buf is None:
                return
            self._deliver_buffer(buf)

    def _deliver_buffer(self, buf):
        try:
            if not self._deliver:
                return
            arr = buf.numpy_wrap()
            if arr.dtype != np.uint8:
                raise RuntimeError("Camera sink must deliver 8-bit images")
            gray = arr[:, :, 0] if arr.ndim == 3 else arr
            height, width = gray.shape
            image = QImage(gray.data, width, height, gray.strides[0], QImage.Format_Grayscale8).copy()
            meta = buf.meta_data
            metadata = {"run_id": self._run_id,
                "camera_frame_id": meta.device_frame_number,
                "camera_timestamp_raw": meta.device_timestamp_ns,
                "host_monotonic": time.monotonic()}
            if self._run_id is not None:
                self.recording_frame_ready.emit(image, metadata)
            else:
                self._last_preview_frame = metadata["host_monotonic"]
                if not self._preview_announced:
                    self._preview_announced = True
                    self.preview_ready.emit()
                    if not self._grabber_announced:
                        self._grabber_announced = True
                        self.grabber_ready.emit()
            if metadata["host_monotonic"] - self._last_preview_emit >= 0.05:
                self.frame_ready.emit(image, metadata)
                self._last_preview_emit = metadata["host_monotonic"]
        except Exception as exc:
            self.error.emit(str(exc), str(getattr(exc, "code", "")))
        finally:
            if buf is not None:
                buf.release()

    def sink_connected(self, sink, image_type, min_buffers_required):
        sink.alloc_and_queue_buffers(max(16, min_buffers_required))
        return True

    def sink_disconnected(self, sink):
        pass

    def stop(self):
        self._stop_requested = True
