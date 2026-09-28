"""Bounded IC4 / PRIM electrical-trigger diagnostic.

Default: inspect camera properties only. --pulse-test arms a physical input and
runs PRIM at the explicit test rate / Capture Every (default 1 Hz).
No Z, firmware upload, or software trigger.
Run only on a supervised apparatus ready to operate its pump trigger.
This is independent of PRIMA's recorder; it does not certify pressure timing.
"""
import argparse
import gc
import json
import math
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prim_app"))
from utils.recording_settings import build_recording_settings, format_prim_command
from utils.camera_trigger import physical_trigger_source


def select_camera(devices, serial, expected_model=None):
    """Select an explicit device identity, never the first enumerated camera."""
    matches = [device for device in devices if device.serial == serial]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one camera with serial {serial}; found {len(matches)}")
    camera = matches[0]
    if expected_model is not None and camera.model_name != expected_model:
        raise RuntimeError(f"Camera {serial} is {camera.model_name}, expected {expected_model}")
    return camera


PROPERTIES = {
    "enumeration": ("TriggerMode", "TriggerSelector", "TriggerSource",
                    "TriggerActivation", "AcquisitionMode", "PixelFormat",
                    "ExposureAuto", "GainAuto", "TriggerOverlap"),
    "integer": ("AcquisitionBurstFrameCount", "Width", "Height", "GPIn"),
    "float": ("ExposureTime", "Gain", "AcquisitionFrameRate", "TriggerDelay",
              "TriggerDebouncer", "TriggerDenoise", "TriggerMask"),
    "boolean": ("AcquisitionFrameRateEnable", "IMXLowLatencyTriggerMode"),
}


def inspect_properties(props):
    result = {}
    for kind, names in PROPERTIES.items():
        for name in names:
            try:
                node = getattr(props, "find_" + kind)(name)
                result[name] = {"value": node.value}
                if kind == "enumeration":
                    result[name]["choices"] = [e.name for e in node.entries]
                elif kind == "float":
                    result[name]["unit"] = node.unit
            except Exception as exc:
                result[name] = {"unavailable": str(exc)}
    return result


def set_checked(props, kind, name, value):
    node = getattr(props, "find_" + kind)(name)
    if node.value != value:
        node.value = value
    if node.value != value:
        raise RuntimeError(f"{name}: requested {value!r}, read back {node.value!r}")


class TriggerSettings:
    """Snapshot and restore this model's FrameStart settings, including auto values."""

    def __init__(self, props):
        self.props = props
        # Do not guess selector-bank semantics for a different existing mode.
        if props.find_enumeration("TriggerSelector").value != "FrameStart":
            raise RuntimeError("Diagnostic requires existing FrameStart selector; no settings changed")
        self.saved = {
            name: props.find_enumeration(name).value
            for name in ("TriggerMode", "TriggerSource", "TriggerActivation",
                         "ExposureAuto", "GainAuto")
        }
        self.burst = props.find_integer("AcquisitionBurstFrameCount").value
        self.exposure = props.find_float("ExposureTime").value
        self.gain = props.find_float("Gain").value
        self.rate = props.find_float("AcquisitionFrameRate").value
        self.expected = {"TriggerSelector": "FrameStart",
                         "TriggerSource": physical_trigger_source(props.find_enumeration("TriggerSource")),
                         "TriggerActivation": self.saved["TriggerActivation"], "TriggerMode": "On"}

    def arm(self):
        set_checked(self.props, "enumeration", "TriggerMode", "Off")
        for name in ("TriggerSource", "TriggerActivation"):
            set_checked(self.props, "enumeration", name, self.expected[name])
        set_checked(self.props, "integer", "AcquisitionBurstFrameCount", 1)
        set_checked(self.props, "enumeration", "TriggerMode", "On")
        return self.verify()

    def verify(self):
        actual = {n: self.props.find_enumeration(n).value for n in self.expected}
        actual["AcquisitionBurstFrameCount"] = self.props.find_integer("AcquisitionBurstFrameCount").value
        if actual != {**self.expected, "AcquisitionBurstFrameCount": 1}:
            raise RuntimeError(f"External-trigger readback failed: {actual}")
        return actual

    def restore(self):
        failures = []
        actions = [("enumeration", "TriggerMode", "Off"),
                   ("enumeration", "TriggerSource", self.saved["TriggerSource"]),
                   ("enumeration", "TriggerActivation", self.saved["TriggerActivation"]),
                   ("integer", "AcquisitionBurstFrameCount", self.burst),
                   ("enumeration", "ExposureAuto", "Off"),
                   ("enumeration", "GainAuto", "Off"),
                   ("float", "AcquisitionFrameRate", self.rate),
                   ("float", "ExposureTime", self.exposure),
                   ("float", "Gain", self.gain),
                   ("enumeration", "ExposureAuto", self.saved["ExposureAuto"]),
                   ("enumeration", "GainAuto", self.saved["GainAuto"]),
                   ("enumeration", "TriggerMode", self.saved["TriggerMode"])]
        for kind, name, value in actions:
            try:
                set_checked(self.props, kind, name, value)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError("Camera restoration incomplete: " + "; ".join(failures))


class BoundedPrimRun:
    """An independent stop deadline plus a finally-path retry of the same S packet."""

    def __init__(self, port, seconds, frame_interval_ms=1000):
        self.port, self.seconds = port, seconds
        self.frame_interval_ms = frame_interval_ms
        self.lock = threading.Lock()
        self.commands, self.errors = [], []
        self.started = False
        self.timer = None

    def send(self, command):
        packet = (format_prim_command(command, self.frame_interval_ms, 1) + "\n").encode("ascii")
        with self.lock:
            written = self.port.write(packet)
            if written != len(packet):
                raise RuntimeError(f"Incomplete {command} write: {written}/{len(packet)} bytes")
            self.commands.append({"packet": packet.decode().strip(), "host_monotonic": time.monotonic()})

    def start(self):
        self.started = True  # Even a partially sent G requires a stop attempt.
        self.timer = threading.Timer(self.seconds, self.stop)
        self.timer.start()
        self.send("G")

    def stop(self):
        if self.started:
            try:
                self.send("S")
            except Exception as exc:
                self.errors.append(str(exc))

    def close(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer.join(timeout=2)
        self.stop()


def read_serial_for(port, seconds, rows, errors, observe=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if observe is not None:
            observe()
        line = port.readline()
        if not line:
            continue
        text = line.decode("utf-8", errors="replace").strip()
        try:
            fields = text.split(",")
            if len(fields) != 3:
                raise ValueError("expected three fields")
            counter, device_time, pressure = int(fields[0]), float(fields[1]), float(fields[2])
            if not math.isfinite(device_time) or not math.isfinite(pressure):
                raise ValueError("non-finite time or pressure")
            rows.append({"counter": counter, "device_time_s": device_time,
                         "pressure_mmHg": pressure, "host_monotonic": time.monotonic()})
        except ValueError as exc:
            errors.append({"line": text, "error": str(exc)})


def summarize(report):
    frames, rows = report["frames"], report["pressure_rows"]
    ids = [f["camera_frame_id"] for f in frames]
    times = [f["camera_timestamp_ns"] for f in frames]
    start = next((c["host_monotonic"] for c in report["commands"] if c["packet"].startswith("<G,")), None)
    stop = next((c["host_monotonic"] for c in report["commands"] if c["packet"].startswith("<S,")), None)
    return {
        "pressure_rows": len(rows), "camera_frames": len(frames),
        "equal_counts": bool(rows) and len(rows) == len(frames),
        "camera_id_discontinuities": sum(b != a + 1 for a, b in zip(ids, ids[1:])),
        "camera_timestamp_nonincreasing": sum(b <= a for a, b in zip(times, times[1:])),
        "pressure_counter_discontinuities": sum(b["counter"] != a["counter"] + 1 for a, b in zip(rows, rows[1:])),
        "frames_before_start": sum(f["host_monotonic"] < start for f in frames) if start else None,
        "frames_over_1s_after_stop": sum(f["host_monotonic"] > stop + 1 for f in frames) if stop else None,
        "electrical_acceptance": "review physical-input readback, idle windows and frame delivery together",
        "pressure_to_exposure_offset": "not measured; device and host clocks are distinct",
    }


def validation_issues(report):
    """A completed diagnostic process is not necessarily a passing pulse test."""
    issues = []
    if report.get("error"):
        issues.append(report["error"])
    for name in ("cleanup_errors", "callback_errors", "serial_parse_errors", "serial_write_errors"):
        if report.get(name):
            issues.append(name)
    if report.get("pulse_test"):
        summary = report["summary"]
        if not summary["equal_counts"]:
            issues.append("Nonempty pressure/frame counts do not match")
        for name in ("camera_id_discontinuities", "camera_timestamp_nonincreasing",
                     "pressure_counter_discontinuities", "frames_before_start",
                     "frames_over_1s_after_stop"):
            if summary[name] != 0:
                issues.append(name)
        if report.get("settings_restored") is not True:
            issues.append("Camera settings restoration not confirmed")
    return issues


def stream_statistics(grabber):
    stats = grabber.stream_statistics
    return {name: getattr(stats, name) for name in (
        "device_delivered", "device_transmission_error", "device_transform_underrun",
        "device_underrun", "transform_delivered", "transform_underrun",
        "sink_delivered", "sink_underrun", "sink_ignored")}


def run_camera(ic4, args, report):
    camera = select_camera(ic4.DeviceEnum.devices(), args.camera_serial, args.camera_model)
    grabber = ic4.Grabber()
    settings = sink = port = run = None
    streaming = False
    frames = report["frames"]

    class Listener(ic4.QueueSinkListener):
        def sink_connected(self, sink, image_type, min_buffers_required):
            report["image_type"] = str(image_type)
            return True

        def frames_queued(self, sink):
            buf = None
            try:
                buf = sink.pop_output_buffer()
                meta = buf.meta_data
                frames.append({"host_monotonic": time.monotonic(),
                               "camera_frame_id": meta.device_frame_number,
                               "camera_timestamp_ns": meta.device_timestamp_ns})
            except Exception as exc:
                report["callback_errors"].append(str(exc))
            finally:
                if buf is not None:
                    buf.release()

    try:
        grabber.device_open(camera)
        props = grabber.device_property_map
        report["camera"] = {"model": camera.model_name, "serial": camera.serial}
        report["before"] = inspect_properties(props)
        if not args.pulse_test:
            return
        import serial
        # Opening the serial device is confined to the explicitly requested test.
        port = serial.Serial(args.port, 115200, timeout=0.05, write_timeout=0.5)
        idle_rows, idle_errors = [], []
        read_serial_for(port, 1.2, idle_rows, idle_errors)
        if idle_rows or idle_errors:
            raise RuntimeError("Serial traffic already present. Leave the apparatus stopped before testing.")
        settings = TriggerSettings(props)
        if args.exposure_ms is not None:
            from utils.camera_trigger import TriggerProfile
            set_checked(props, "enumeration", "ExposureAuto", "Off")
            set_checked(props, "float", "ExposureTime", args.exposure_ms * 1000)
            set_checked(props, "enumeration", "GainAuto", "Off")
            profile = TriggerProfile(ic4, props)
            report["recording_profile"] = profile.prepare(1 / args.fps)
            report["armed_before_stream"] = settings.verify()
        else:
            profile = None
            report["armed_before_stream"] = settings.arm()
        listener = Listener()
        sink = ic4.QueueSink(listener, max_output_buffers=16)
        grabber.stream_setup(sink, setup_option=ic4.StreamSetupOption.ACQUISITION_START)
        streaming = True
        report["armed_after_stream"] = settings.verify()
        if profile:
            profile.verify()
        print(f"Camera armed on {settings.expected['TriggerSource']}. Checking idle for 2 seconds.", flush=True)
        read_serial_for(port, 2, idle_rows, idle_errors)
        if frames or idle_rows or idle_errors:
            raise RuntimeError("Camera frames or serial traffic appeared before G; test aborted.")
        report["idle_before_seconds"] = 2
        recording_settings = build_recording_settings(args.fps, 1)
        report["requested_fps"] = args.fps
        report["frame_interval_ms"] = recording_settings.frame_interval_ms
        report["stream_statistics_before_start"] = stream_statistics(grabber)
        report["stream_statistics_samples"] = []
        last_statistics_time = 0.0

        def observe_stream():
            nonlocal last_statistics_time
            now = time.monotonic()
            if now - last_statistics_time >= 0.25:
                report["stream_statistics_samples"].append({
                    "host_monotonic": now, "frames_received": len(frames),
                    **stream_statistics(grabber)})
                last_statistics_time = now

        run = BoundedPrimRun(port, args.seconds, recording_settings.frame_interval_ms)
        report["commands"] = run.commands
        report["serial_write_errors"] = run.errors
        print(f"Starting PRIM at {args.fps} Hz for {args.seconds} seconds; independent S deadline active.", flush=True)
        run.start()
        read_serial_for(port, args.seconds + 3, report["pressure_rows"], report["serial_parse_errors"], observe_stream)
        report["armed_at_end"] = settings.verify()
        if profile:
            profile.verify()
            profile.restore()
        report["stream_statistics"] = stream_statistics(grabber)
    finally:
        if run is not None:
            run.close()
        if port is not None:
            try:
                port.close()
            except Exception as exc:
                report["cleanup_errors"].append("serial close: " + str(exc))
        if streaming:
            try:
                grabber.stream_stop()
            except Exception as exc:
                report["cleanup_errors"].append("stream stop: " + str(exc))
        if settings is not None:
            try:
                settings.restore()
                report["settings_restored"] = True
            except Exception as exc:
                report["cleanup_errors"].append(str(exc))
                report["settings_restored"] = False
            report["after"] = inspect_properties(grabber.device_property_map)
        if grabber.is_device_open:
            grabber.device_close()
        # Drop every SDK object before Library.exit (including node references).
        settings = sink = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera-serial", required=True)
    parser.add_argument("--camera-model", help="Optional expected model name; an identity check, not a supported-model list")
    parser.add_argument("--pulse-test", action="store_true", help="Operate PRIM and arm camera; requires supervised pump-ready setup")
    parser.add_argument("--port", help="Explicit PRIM serial port, only opened with --pulse-test")
    parser.add_argument("--seconds", type=int, default=8, choices=range(2, 11))
    parser.add_argument("--fps", type=int, default=1, choices=(1, 10), help="PRIM pressure/pulse rate; camera exposure and operating rate are preserved")
    parser.add_argument("--exposure-ms", type=float, help="Explicit temporary manual exposure; use production maximum-rate preparation")
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path; existing files are never overwritten")
    args = parser.parse_args(argv)
    if args.pulse_test and not args.port:
        parser.error("--pulse-test requires an explicit --port")
    if args.exposure_ms is not None and (not args.pulse_test or not 0 < args.exposure_ms <= 10):
        parser.error("--exposure-ms requires --pulse-test and an exposure in (0, 10] ms")
    # Establish report ownership before touching hardware.
    with args.output.open("x", encoding="utf-8") as output:
        report = {"diagnostic": "PRIM external-trigger pulse acceptance", "pulse_test": args.pulse_test,
                  "camera_timestamp_note": "Raw SDK device_timestamp_ns values; units are unverified on this device/driver",
                  "frames": [], "pressure_rows": [], "commands": [], "callback_errors": [],
                  "serial_parse_errors": [], "serial_write_errors": [], "cleanup_errors": []}
        try:
            import imagingcontrol4 as ic4
            with ic4.Library.init_context():
                try:
                    run_camera(ic4, args, report)
                except Exception as exc:
                    report["error"] = str(exc)
                    # Release SDK objects retained by an exception traceback
                    # while the library is still initialized.
                    exc.__traceback__ = None
                finally:
                    gc.collect()
        except Exception as exc:
            report["error"] = str(exc)
        if args.pulse_test:
            report["summary"] = summarize(report)
        report["validation_issues"] = validation_issues(report)
        json.dump(report, output, indent=2)
        output.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k in ("error", "summary", "settings_restored", "cleanup_errors", "validation_issues")}, indent=2))
    print(f"Report: {args.output}")
    return 1 if report["validation_issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
