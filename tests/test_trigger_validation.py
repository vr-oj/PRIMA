import importlib.util
from pathlib import Path
import threading
import unittest
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "trigger_validation", Path(__file__).resolve().parents[1] / "scripts" / "validate_prim_trigger.py")
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


class Properties:
    def __init__(self):
        self.values = {name: SimpleNamespace(value=value) for name, value in {
            "TriggerSelector": "FrameStart", "TriggerMode": "Off", "TriggerSource": "Any",
            "TriggerActivation": "FallingEdge", "AcquisitionBurstFrameCount": 3,
            "ExposureTime": 100000.0, "Gain": 19.0, "AcquisitionFrameRate": 10.0,
            "ExposureAuto": "Continuous", "GainAuto": "Continuous"}.items()}
        self.values["TriggerSource"].entries = [SimpleNamespace(name=n) for n in ("Line1", "Software", "Any")]

    def find_enumeration(self, name):
        return self.values[name]

    find_integer = find_enumeration
    find_float = find_enumeration


class Port:
    def __init__(self):
        self.packets = []
        self.stopped = threading.Event()
        self.fail_start = False

    def write(self, packet):
        self.packets.append(packet)
        if packet.startswith(b"<G") and self.fail_start:
            raise OSError("start write failed")
        if packet.startswith(b"<S"):
            self.stopped.set()
        return len(packet)


class TriggerDiagnosticTests(unittest.TestCase):
    def test_camera_selection_uses_serial_and_accepts_unlisted_models(self):
        first = SimpleNamespace(serial="first", model_name="Example A")
        second = SimpleNamespace(serial="second", model_name="Another IC4 Camera")
        self.assertIs(diagnostic.select_camera([first, second], "second"), second)
        self.assertIs(diagnostic.select_camera([second, first], "second"), second)

    def test_missing_or_duplicate_serial_never_falls_back_to_first_camera(self):
        camera = SimpleNamespace(serial="wanted", model_name="Example")
        for devices in ([], [camera, camera]):
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                diagnostic.select_camera(devices, "wanted")
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            diagnostic.select_camera([camera], "missing")

    def test_optional_model_check_rejects_wrong_identity(self):
        camera = SimpleNamespace(serial="wanted", model_name="Observed")
        self.assertIs(diagnostic.select_camera([camera], "wanted", "Observed"), camera)
        with self.assertRaisesRegex(RuntimeError, "expected"):
            diagnostic.select_camera([camera], "wanted", "Different")

    def test_diagnostic_uses_available_physical_line_instead_of_fixed_line1(self):
        props = Properties()
        props.values["TriggerSource"].entries = [SimpleNamespace(name=n) for n in ("Line3", "Software")]
        settings = diagnostic.TriggerSettings(props)
        self.assertEqual(settings.arm()["TriggerSource"], "Line3")
        settings.restore()
        self.assertEqual(props.values["TriggerSource"].value, "Any")

    def test_line1_excludes_software_and_restores_original_settings(self):
        props = Properties()
        before = {n: p.value for n, p in props.values.items()}
        settings = diagnostic.TriggerSettings(props)
        armed = settings.arm()
        self.assertEqual(armed["TriggerSource"], "Line1")
        self.assertEqual(armed["TriggerMode"], "On")
        self.assertEqual(armed["TriggerActivation"], before["TriggerActivation"])
        self.assertEqual(armed["AcquisitionBurstFrameCount"], 1)
        props.values["ExposureTime"].value = 1234.0
        props.values["Gain"].value = 2.0
        settings.restore()
        self.assertEqual({n: p.value for n, p in props.values.items()}, before)

    def test_changed_readback_rejects_armed_state(self):
        props = Properties()
        settings = diagnostic.TriggerSettings(props)
        settings.arm()
        props.values["TriggerSource"].value = "Software"
        with self.assertRaisesRegex(RuntimeError, "readback failed"):
            settings.verify()
        settings.restore()
        self.assertEqual(props.values["TriggerSource"].value, "Any")

    def test_other_selector_is_rejected_before_any_changes(self):
        props = Properties()
        props.values["TriggerSelector"].value = "ExposureActive"
        before = {n: p.value for n, p in props.values.items()}
        with self.assertRaisesRegex(RuntimeError, "no settings changed"):
            diagnostic.TriggerSettings(props)
        self.assertEqual({n: p.value for n, p in props.values.items()}, before)

    def test_silently_ignored_write_is_detected(self):
        class ReadOnly:
            @property
            def value(self):
                return "Off"

            @value.setter
            def value(self, value):
                pass
        props = Properties()
        props.values["TriggerMode"] = ReadOnly()
        with self.assertRaisesRegex(RuntimeError, "read back"):
            diagnostic.TriggerSettings(props).arm()

    def test_watchdog_sends_stop_without_main_loop_progress(self):
        port = Port()
        run = diagnostic.BoundedPrimRun(port, 0.03)
        try:
            run.start()
            self.assertTrue(port.stopped.wait(1))
        finally:
            run.close()
        self.assertEqual(port.packets[0], b"<G, 1000, 1>\n")
        self.assertTrue(all(p == b"<S, 1000, 1>\n" for p in port.packets[1:]))

    def test_failed_start_still_attempts_stop(self):
        port = Port()
        port.fail_start = True
        run = diagnostic.BoundedPrimRun(port, 2)
        try:
            with self.assertRaises(OSError):
                run.start()
        finally:
            run.close()
        self.assertTrue(port.stopped.is_set())

    def test_ten_hz_start_and_stop_preserve_prim_packet_format(self):
        port = Port()
        settings = diagnostic.build_recording_settings(10, 1)
        run = diagnostic.BoundedPrimRun(port, 2, settings.frame_interval_ms)
        try:
            run.start()
        finally:
            run.close()
        self.assertEqual(port.packets, [b"<G, 100, 1>\n", b"<S, 100, 1>\n"])

    def test_completed_run_with_missing_images_fails_validation(self):
        report = {"pulse_test": True, "settings_restored": True,
                  "frames": [], "pressure_rows": [{"counter": 1}],
                  "commands": [{"packet": "<G, 100, 1>", "host_monotonic": 1},
                               {"packet": "<S, 100, 1>", "host_monotonic": 2}]}
        report["summary"] = diagnostic.summarize(report)
        self.assertIn("Nonempty pressure/frame counts do not match", diagnostic.validation_issues(report))

    def test_callback_or_parse_errors_fail_even_if_counts_match(self):
        report = {"pulse_test": False, "callback_errors": ["buffer read failed"],
                  "serial_parse_errors": ["bad line"]}
        self.assertEqual(diagnostic.validation_issues(report), ["callback_errors", "serial_parse_errors"])

    def test_no_start_means_no_stop_command(self):
        port = Port()
        run = diagnostic.BoundedPrimRun(port, 2)
        run.close()
        self.assertEqual(port.packets, [])

    def test_empty_counts_do_not_indicate_success(self):
        result = diagnostic.summarize({"frames": [], "pressure_rows": [], "commands": []})
        self.assertFalse(result["equal_counts"])
        self.assertIsNone(result["frames_before_start"])

    def test_summary_reports_missing_frames_and_idle_violations(self):
        result = diagnostic.summarize({
            "frames": [
                {"camera_frame_id": 1, "camera_timestamp_ns": 10, "host_monotonic": 5},
                {"camera_frame_id": 3, "camera_timestamp_ns": 9, "host_monotonic": 20}],
            "pressure_rows": [{"counter": 1}, {"counter": 2}, {"counter": 1}],
            "commands": [{"packet": "<G, 1000, 1>", "host_monotonic": 6},
                         {"packet": "<S, 1000, 1>", "host_monotonic": 12}]})
        self.assertFalse(result["equal_counts"])
        for key in ("camera_id_discontinuities", "camera_timestamp_nonincreasing",
                    "pressure_counter_discontinuities", "frames_before_start",
                    "frames_over_1s_after_stop"):
            self.assertEqual(result[key], 1, key)


if __name__ == "__main__":
    unittest.main()
