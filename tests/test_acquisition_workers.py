import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
from qt_support import APP
from threads.sdk_camera_thread import SDKCameraThread, PreviewRecoveryError
from threads.serial_thread import SerialThread
from utils.camera_access import require_no_other_prima


class CameraWorkerTests(unittest.TestCase):
    def preview_camera(self, rate=75.0, fail_rate=None, fail_start=False):
        camera = SDKCameraThread()
        events = []
        class Rate:
            minimum, maximum, unit = 0.1, 75.0, "fps"
            def __init__(self):
                self.current = rate
            @property
            def value(self):
                return self.current
            @value.setter
            def value(self, value):
                if camera._streaming:
                    raise RuntimeError("Frame rate cannot be changed during acquisition")
                events.append(("rate", value))
                self.current = value
                if value == fail_rate:
                    raise RuntimeError("Rejected camera rate")
        node = Rate()
        floats = {"AcquisitionFrameRate": node,
                  "ExposureTime": SimpleNamespace(value=13333.0, minimum=10, maximum=30000000, unit="us"),
                  "Gain": SimpleNamespace(value=0.0, minimum=0, maximum=40, unit="dB")}
        props = Mock()
        props.find_float.side_effect = floats.__getitem__
        props.find_enumeration.side_effect = lambda name: SimpleNamespace(value="Off", entries=[])
        def start(*args, **kwargs):
            events.append(("start", node.value))
            if fail_start is True or node.value == fail_start:
                raise RuntimeError("Stream setup failed")
        camera.grabber = SimpleNamespace(device_property_map=props,
            stream_stop=lambda: events.append(("stop", node.value)), stream_setup=start)
        camera._sink = Mock()
        camera._streaming = camera._deliver = camera._preview_announced = True
        return camera, node, events

    def test_preview_rate_is_written_while_stopped_then_preview_resumes(self):
        camera, node, events = self.preview_camera()
        camera._setting("AcquisitionFrameRate", 10)
        self.assertEqual(events, [("stop", 75), ("rate", 10), ("start", 10)])
        self.assertTrue(camera._streaming and camera._deliver)
        self.assertFalse(camera._preview_announced)
        self.assertIsNone(camera._run_id)

    def test_rejected_preview_rate_restores_previous_rate_and_stream(self):
        camera, node, events = self.preview_camera(fail_rate=10)
        with self.assertRaisesRegex(RuntimeError, "Previous preview restored"):
            camera._setting("AcquisitionFrameRate", 10)
        self.assertEqual(node.value, 75)
        self.assertTrue(camera._streaming and camera._deliver)
        self.assertEqual(events[-1], ("start", 75))

    def test_rate_that_fails_stream_setup_rolls_back_and_restarts(self):
        camera, node, events = self.preview_camera(fail_start=10)
        with self.assertRaisesRegex(RuntimeError, "Previous preview restored"):
            camera._setting("AcquisitionFrameRate", 10)
        self.assertEqual(node.value, 75)
        self.assertTrue(camera._streaming and camera._deliver)
        self.assertEqual(events[-1], ("start", 75))

    def test_failed_preview_recovery_is_fatal(self):
        camera, node, events = self.preview_camera(fail_start=True)
        with self.assertRaises(PreviewRecoveryError):
            camera._setting("AcquisitionFrameRate", 10)
        self.assertFalse(camera._deliver)

    def test_preview_watchdog_allows_slow_supported_rates_but_detects_stall(self):
        camera, node, events = self.preview_camera(rate=.1)
        camera._snapshot()
        camera._last_preview_frame = 100.0
        self.assertEqual(camera._preview_timeout_s, 30)
        with patch("threads.sdk_camera_thread.time.monotonic", return_value=107):
            camera._check_preview_timeout()
        with patch("threads.sdk_camera_thread.time.monotonic", return_value=131):
            with self.assertRaisesRegex(RuntimeError, "not delivering"):
                camera._check_preview_timeout()

    def test_preview_rate_stays_locked_during_triggered_recording(self):
        camera, node, events = self.preview_camera()
        camera._run_id = "recording"
        with self.assertRaisesRegex(RuntimeError, "locked"):
            camera._setting("AcquisitionFrameRate", 10)
        self.assertEqual(events, [])
        self.assertEqual(node.value, 75)

    def test_camera_ready_requires_a_received_preview_image(self):
        camera = SDKCameraThread()
        camera._deliver = True
        ready, preview = [], []
        camera.grabber_ready.connect(lambda: ready.append(True))
        camera.preview_ready.connect(lambda: preview.append(True))
        self.assertEqual(ready, [])
        class Buffer:
            meta_data = SimpleNamespace(device_frame_number=1, device_timestamp_ns=100000)
            data = np.full((3, 5, 1), 20, np.uint8)
            def numpy_wrap(self):
                return self.data
            def release(self):
                pass
        camera._deliver_buffer(Buffer())
        camera._deliver_buffer(Buffer())
        self.assertEqual(ready, [True])
        self.assertEqual(preview, [True])

    def test_callback_drains_buffers_and_copies_before_release(self):
        camera = SDKCameraThread()
        camera._deliver, camera._run_id = True, "test"
        saved = []
        camera.recording_frame_ready.connect(lambda image, meta: saved.append((image, meta)))
        class Buffer:
            def __init__(self, number):
                self.data = np.full((3, 5, 1), number, np.uint8)
                self.meta_data = SimpleNamespace(device_frame_number=number, device_timestamp_ns=number * 100000)
                self.releases = 0
            def numpy_wrap(self):
                return self.data
            def release(self):
                self.releases += 1
                self.data.fill(0)
        buffers = [Buffer(11), Buffer(12)]
        sink = Mock()
        sink.try_pop_output_buffer.side_effect = [*buffers, None]
        camera.frames_queued(sink)
        self.assertEqual([b.releases for b in buffers], [1, 1])
        self.assertEqual([m["camera_frame_id"] for _, m in saved], [11, 12])
        self.assertEqual([image.pixelColor(0, 0).red() for image, _ in saved], [11, 12])

    def test_failed_statistics_cannot_prevent_stream_stop(self):
        camera = SDKCameraThread()
        camera._streaming, camera._run_id = True, "test"
        class Grabber:
            stopped = False
            @property
            def stream_statistics(self):
                raise OSError("device disconnected")
            def stream_stop(self):
                self.stopped = True
        camera.grabber = Grabber()
        camera._stop_stream()
        self.assertTrue(camera.grabber.stopped)
        self.assertFalse(camera._streaming)
        self.assertIn("error", camera.capture_statistics[-1])

    def test_other_installed_prima_blocks_camera_access(self):
        with patch("utils.camera_access.sys.platform", "win32"), \
             patch("utils.camera_access.subprocess.CREATE_NO_WINDOW", 0, create=True), \
             patch("utils.camera_access.os.getpid", return_value=10), \
             patch("utils.camera_access.os.getppid", return_value=9), \
             patch("utils.camera_access.subprocess.run", return_value=SimpleNamespace(stdout='"PRIMA.exe","50","Console","1","20 K"')):
            with self.assertRaisesRegex(RuntimeError, "Close the other PRIMA"):
                require_no_other_prima()

    def test_packaged_bootstrap_is_not_a_competing_application(self):
        with patch("utils.camera_access.sys.platform", "win32"), \
             patch("utils.camera_access.subprocess.CREATE_NO_WINDOW", 0, create=True), \
             patch("utils.camera_access.os.getpid", return_value=10), \
             patch("utils.camera_access.os.getppid", return_value=9), \
             patch("utils.camera_access.subprocess.run", return_value=SimpleNamespace(stdout='"PRIMA.exe","10"\n"PRIMA.exe","9"')):
            require_no_other_prima()


class SerialWorkerTests(unittest.TestCase):
    def run_transport(self, chunks, command=None, partial_write=False):
        worker = SerialThread("TEST")
        rows, errors, sent = [], [], []
        worker.data_ready.connect(lambda *values: rows.append(values))
        worker.error_occurred.connect(errors.append)
        worker.command_sent.connect(sent.append)
        class Port:
            in_waiting = 1
            def __init__(self):
                self.chunks = iter(chunks)
                self.writes = []
                self.closed = False
            def read(self, size):
                try:
                    return next(self.chunks)
                except StopIteration:
                    worker.stop()
                    return b""
            def write(self, packet):
                self.writes.append(packet)
                return 2 if partial_write and packet.startswith(b"<G,") else len(packet)
            def close(self):
                self.closed = True
        port = Port()
        if command:
            worker.command_queue.put(command)
        with patch("threads.serial_thread.serial.Serial", return_value=port):
            worker.run()
        self.assertTrue(port.closed)
        self.assertFalse(worker.send_command("<G, 100, 1>"))
        return rows, errors, sent, port.writes

    def test_partial_lines_and_sparse_repeated_counters_are_preserved(self):
        rows, errors, sent, writes = self.run_transport([b"20,1.0,", b"42\n20,1.1,43\r\n"])
        self.assertEqual(rows, [(20, 1.0, 42.0), (20, 1.1, 43.0)])
        self.assertEqual(errors, [])
        self.assertEqual(writes, [])  # Does not stop a manually started PRIM run.

    def test_partial_g_write_attempts_s_without_false_acknowledgment(self):
        rows, errors, sent, writes = self.run_transport([], b"<G, 100, 1>\n", True)
        self.assertEqual(writes, [b"<G, 100, 1>\n", b"<S, 100, 1>\n"])
        self.assertEqual(sent, [])
        self.assertTrue(any("Partial serial write" in s for s in errors))

    def test_invalid_data_stops_only_the_acquisition_we_started(self):
        rows, errors, sent, writes = self.run_transport([b"invalid\n"], b"<G, 100, 1>\n")
        self.assertEqual(sent, ["<G, 100, 1>"])
        self.assertEqual(writes[-1], b"<S, 100, 1>\n")
        self.assertTrue(any("Invalid PRIM data" in s for s in errors))
