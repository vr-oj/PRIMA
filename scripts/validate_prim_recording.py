"""Supervised, bounded test of the production camera/serial/recording workers.

Temporarily use a specified manual exposure and restore the original camera
settings afterward. G/S only. Never run on an apparatus not ready for its pump.
"""
import argparse
import gc
import json
import os
from pathlib import Path
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--camera-serial", required=True)
    parser.add_argument("--camera-model", help="Optional expected model name; an identity check, not a supported-model list")
    parser.add_argument("--port", required=True)
    parser.add_argument("--exposure-ms", type=float, required=True)
    parser.add_argument("--seconds", type=int, choices=range(2, 11), default=8)
    parser.add_argument("--armed-idle-seconds", type=int, choices=(0, 1, 2), default=0,
                        help="Observe external-trigger idle before G; any image aborts the test")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.exposure_ms <= 10:
        parser.error("This diagnostic supports exposures up to 10 ms at 10 Hz")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["PRIM_RESULTS_DIR"] = str(output)
    os.environ["PRIMA_CONFIG_DIR"] = str(output / "config")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prim_app"))
    import imagingcontrol4 as ic4
    import tifffile
    from PyQt5.QtCore import QCoreApplication, QObject, QThread, QTimer, Qt, QMetaObject, QEvent, pyqtSignal
    from recording_manager import RecordingManager
    from threads.sdk_camera_thread import SDKCameraThread
    from threads.serial_thread import SerialThread
    from validate_prim_trigger import TriggerSettings, inspect_properties, set_checked, select_camera
    from utils.camera_access import require_no_other_prima

    require_no_other_prima()

    app = QCoreApplication([])
    report = {"commands": [], "errors": [], "settings_restored": False,
              "requested_exposure_ms": args.exposure_ms, "seconds": args.seconds,
              "camera_model": args.camera_model, "camera_serial": args.camera_serial, "port": args.port,
              "armed_idle_seconds": args.armed_idle_seconds, "frames_before_g": 0}

    class Run(QObject):
        activate = pyqtSignal(object)
        issue = pyqtSignal(str)

        def __init__(self, info):
            super().__init__()
            self.camera = SDKCameraThread()
            self.camera.set_device_info(info)
            self.serial = SerialThread(args.port)
            self.worker = RecordingManager(str(output), 10, 100, 1, record_video=True, run_id="validation")
            self.thread = QThread()
            self.worker.moveToThread(self.thread)
            self.camera.grabber_ready.connect(self.preview)
            self.camera.error.connect(lambda msg, code: self.fail("Camera: " + msg))
            self.camera.timing_failed.connect(lambda run_id, msg: self.fail(msg))
            self.camera.timing_ready.connect(self.armed)
            self.camera.recording_frame_ready.connect(self.worker.enqueue_frame, Qt.DirectConnection)
            self.camera.recording_frame_ready.connect(self.observe_triggered_frame)
            self.serial.status_changed.connect(self.serial_status)
            self.serial.command_sent.connect(self.command_sent)
            self.serial.error_occurred.connect(self.fail)
            self.serial.data_ready.connect(self.check_idle)
            self.serial.data_ready.connect(self.worker.append_pressure)
            self.thread.started.connect(self.worker.start_recording)
            self.worker.ready_for_acquisition.connect(self.prepared)
            self.worker.activated.connect(self.acquire)
            self.worker.error_occurred.connect(self.fail)
            self.worker.finalized.connect(self.finalized)
            self.worker.finished.connect(self.thread.quit)
            self.activate.connect(self.worker.activate)
            self.issue.connect(self.worker.mark_incomplete)
            self.serial_ready = self.camera_ready = self.preparing = self.started = self.failed = False
            self.watchdog = None

        def check_idle(self, *args):
            if not self.started:
                self.fail("Serial data appeared before our G command; apparatus was not idle")

        def serial_status(self, status):
            if status.startswith("Connected to "):
                self.serial_ready = True
                QTimer.singleShot(1200, self.maybe_prepare)

        def preview(self):
            self.camera_ready = True
            QTimer.singleShot(1200, self.maybe_prepare)

        def maybe_prepare(self):
            if self.serial_ready and self.camera_ready and not self.preparing and not self.failed:
                self.preparing = True
                self.thread.start()

        def prepared(self):
            if self.failed:
                QMetaObject.invokeMethod(self.worker, "request_stop", Qt.QueuedConnection)
                return
            self.camera.request_recording("validation", 0.1)

        def armed(self, run_id, details):
            if self.failed:
                return
            report["camera_preparation"] = details
            QTimer.singleShot(args.armed_idle_seconds * 1000, self.activate_after_idle)

        def observe_triggered_frame(self, image, metadata):
            if not self.started:
                report["frames_before_g"] += 1
                self.fail("Camera delivered an image while externally armed before G")

        def activate_after_idle(self):
            if not self.failed:
                self.activate.emit(report["camera_preparation"])

        def acquire(self):
            if self.failed:
                return
            details = report["camera_preparation"]
            self.started = True
            self.watchdog = threading.Timer(args.seconds, lambda: self.serial.send_command("<S, 100, 1>"))
            self.watchdog.start()
            print(f"Verified external trigger at camera {details['camera_operating_fps']:g} fps; starting PRIM 10 Hz for {args.seconds}s.", flush=True)
            if not self.serial.send_command("<G, 100, 1>"):
                self.fail("Could not queue G")

        def command_sent(self, command):
            report["commands"].append({"packet": command, "host_monotonic": time.monotonic()})
            if command.startswith("<G,"):
                self.serial.set_idle_timeout_enabled(True)
            if command.startswith("<S,"):
                self.serial.set_idle_timeout_enabled(False)
                QMetaObject.invokeMethod(self.worker, "request_stop", Qt.QueuedConnection)

        def fail(self, message):
            report["errors"].append(message)
            self.issue.emit(message)
            if self.failed:
                return
            self.failed = True
            if self.started:
                self.serial.send_command("<S, 100, 1>")
            if self.thread.isRunning():
                QMetaObject.invokeMethod(self.worker, "request_stop", Qt.QueuedConnection)
            else:
                app.quit()

        def finalized(self, summary):
            report["recording"] = summary
            self.camera.stop()
            self.serial.stop()
            QTimer.singleShot(100, app.quit)

    def hardware_session():
        device_info = select_camera(ic4.DeviceEnum.devices(), args.camera_serial, args.camera_model)
        report["camera_model"] = device_info.model_name
        grabber = ic4.Grabber()
        snapshot = None
        run = None
        deadline = None
        try:
            grabber.device_open(device_info)
            props = grabber.device_property_map
            report["before"] = inspect_properties(props)
            snapshot = TriggerSettings(props)
            original_rate = props.find_float("AcquisitionFrameRate").value
            saved_values = (dict(snapshot.saved), snapshot.burst, snapshot.exposure, snapshot.gain)
            set_checked(props, "enumeration", "ExposureAuto", "Off")
            set_checked(props, "float", "ExposureTime", args.exposure_ms * 1000)
            set_checked(props, "enumeration", "GainAuto", "Off")
            grabber.device_close()
            snapshot = props = None
            run = Run(device_info)
            deadline = QTimer()
            deadline.setSingleShot(True)
            deadline.timeout.connect(lambda: (run.fail("Diagnostic exceeded its overall deadline"), app.quit()))
            deadline.start((args.seconds + 15) * 1000)
            run.camera.start()
            run.serial.start()
            app.exec_()
        finally:
            if deadline:
                deadline.stop()
                deadline.timeout.disconnect()
            if run:
                if run.watchdog:
                    run.watchdog.cancel()
                    run.watchdog.join(2)
                run.serial.stop()  # Includes best-effort S if acquisition remains active.
                run.camera.stop()
                if run.thread.isRunning():
                    QMetaObject.invokeMethod(run.worker, "request_stop", Qt.QueuedConnection)
                end = time.monotonic() + 7
                while time.monotonic() < end and any(t.isRunning() for t in (run.camera, run.serial, run.thread)):
                    app.processEvents()
                    time.sleep(.02)
                if any(t.isRunning() for t in (run.camera, run.serial, run.thread)):
                    raise RuntimeError("A hardware worker did not shut down; do not reopen the device")
                report["stream_statistics"] = run.camera.capture_statistics
                for obj in (run.worker, run.thread, run.serial, run.camera, run):
                    obj.deleteLater()
                obj = None
                app.sendPostedEvents(None, QEvent.DeferredDelete)
                app.processEvents()
            if not grabber.is_device_open:
                grabber.device_open(device_info)
            props = grabber.device_property_map
            if 'saved_values' in locals():
                snapshot = TriggerSettings(props)
                snapshot.saved, snapshot.burst, snapshot.exposure, snapshot.gain = saved_values
                snapshot.rate = original_rate
                snapshot.restore()
                report["after"] = inspect_properties(props)
                report["settings_restored"] = report["before"] == report["after"]
            grabber.device_close()
            snapshot = props = grabber = run = device_info = None
            gc.collect()
    # End the acquisition function's frame before shutting down the SDK, so
    # Python/Qt callback references cannot retain its native wrappers past exit.
    with ic4.Library.init_context(api_log_level=ic4.LogLevel.DEBUG, log_targets=ic4.LogTarget.FILE, log_file=str(output / "sdk.log")):
        try:
            hardware_session()
        except Exception as exc:
            report["errors"].append("Diagnostic: " + str(exc))
            exc.__traceback__ = None
        gc.collect()
        report["sdk_objects_before_exit"] = [type(obj).__name__ for obj in gc.get_objects()
            if type(obj).__module__.startswith("imagingcontrol4.") and type(obj).__name__ in ("Grabber", "DeviceInfo", "PropertyMap")]
    summary = report.get("recording", {})
    if summary.get("tiff_path"):
        try:
            with tifffile.TiffFile(summary["tiff_path"]) as stack:
                for page in stack.pages:
                    page.asarray()
                report["decoded_tiff_pages"] = len(stack.pages)
        except Exception as exc:
            report["errors"].append("Saved TIFF validation: " + str(exc))
    (output / "validation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"errors": report["errors"], "settings_restored": report["settings_restored"],
                      "recording": summary, "decoded_tiff_pages": report.get("decoded_tiff_pages")}, indent=2))
    return 0 if summary.get("complete") and report["settings_restored"] and not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
