import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import numpy as np
import tifffile
from qt_support import APP
from cameras.micro_manager_backend import MicroManagerBackend
from cameras.micro_manager_process import MicroManagerService
from cameras.micro_manager_profiles import normalize_profile, read_profile, write_profile
from cameras.pixels import copy_mm_frame, display_gray
from cameras.registry import CameraRegistry
from threads.mmcore_camera_thread import MMCoreCameraThread, control_snapshot
from recording_manager import RecordingManager


def fake_core():
    core = Mock()
    values = {"Exposure": "10", "Gain": "2", "PixelType": "16bit",
              "TriggerMode": "Off", "TriggerSelector": "FrameStart",
              "TriggerSource": "Line1", "TriggerActivation": "FallingEdge",
              "ExposureAuto": "Continuous", "GainAuto": "Continuous",
              "AcquisitionFrameRate": "30", "Serial": "123"}
    choices = {"TriggerMode": ("Off", "On"), "TriggerSelector": ("FrameStart",),
               "TriggerSource": ("Line1", "Software"), "TriggerActivation": ("RisingEdge", "FallingEdge"),
               "ExposureAuto": ("Off", "Continuous"), "GainAuto": ("Off", "Continuous"),
               "PixelType": ("8bit", "16bit")}
    core.getLoadedDevicesOfType.return_value = ["Camera"]
    core.getCameraDevice.return_value = "Camera"
    core.getDeviceLibrary.return_value = "TestAdapter"
    core.getVersionInfo.return_value = "MMCore test"
    core.getAPIVersionInfo.return_value = "Device API version 75"
    core.getDevicePropertyNames.side_effect = lambda camera: list(values)
    core.hasProperty.side_effect = lambda camera, key: key in values
    core.getProperty.side_effect = lambda camera, key: values[key]
    core.setProperty.side_effect = lambda camera, key, value: values.__setitem__(key, str(value))
    core.getPropertyType.side_effect = lambda camera, key: 2 if key in ("Exposure", "Gain", "AcquisitionFrameRate") else 1
    core.hasPropertyLimits.side_effect = lambda camera, key: key in ("Exposure", "AcquisitionFrameRate")
    core.getPropertyLowerLimit.return_value = 1
    core.getPropertyUpperLimit.return_value = 100
    core.isPropertyReadOnly.side_effect = lambda camera, key: key == "Serial"
    core.isPropertyPreInit.return_value = False
    core.getAllowedPropertyValues.side_effect = lambda camera, key: choices.get(key, ())
    core.getExposure.side_effect = lambda: float(values["Exposure"])
    core.setExposure.side_effect = lambda value: values.__setitem__("Exposure", str(value))
    core.getROI.return_value = (0, 0, 4, 2)
    core.getNumberOfComponents.return_value = 1
    core.getNumberOfCameraChannels.return_value = 1
    core.getImageWidth.return_value = 4
    core.getImageHeight.return_value = 2
    core.getImageBitDepth.return_value = 12
    core.getBytesPerPixel.return_value = 2
    core.isBufferOverflowed.return_value = False
    core.getRemainingImageCount.return_value = 0
    core.isSequenceRunning.return_value = True
    return core, NS(CMMCore=lambda: core, CameraDevice=2), values


class MicroManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = Path(self.temp.name) / "camera.cfg"
        cfg.write_text("# test-only configuration")
        self.profile = {"installation": self.temp.name, "config": str(cfg), "camera": "Camera"}
        self.core, sdk, self.values = fake_core()
        self.service = MicroManagerService(sdk)
        self.addCleanup(self.service.close)

    def open(self):
        return self.service.dispatch("open", (self.profile, ""))

    def test_saved_connections_list_without_initializing_hardware(self):
        backend = MicroManagerBackend(True)
        backend.profiles = [{**self.profile, "configured_mode": {"width": 4, "height": 2}}]
        devices = backend.discover()
        self.assertEqual(devices[0].backend, "micromanager")
        self.assertEqual(backend.list_modes(devices[0])[0].as_tuple(), (4, 2, "Configuration"))
        self.core.loadSystemConfiguration.assert_not_called()

    def test_profile_round_trip_and_invalid_timing_mapping(self):
        path = Path(self.temp.name) / "profile.json"
        write_profile(path, self.profile)
        self.assertEqual(read_profile(path)["camera"], "Camera")
        with self.assertRaisesRegex(ValueError, "preview assignments"):
            normalize_profile({**self.profile, "timing": {"external": [{"property": "TriggerMode", "value": "On"}]}})

    def test_optional_bridges_do_not_prevent_startup(self):
        with patch("cameras.registry.importlib.util.find_spec", return_value=None), \
             patch("cameras.registry.importlib.import_module", side_effect=ImportError("missing")):
            registry = CameraRegistry()
        self.assertEqual(registry.discover_cameras(), [])
        self.assertEqual(set(registry.unavailable), {"ic4", "micromanager"})

    def test_preview_arm_and_restore_preserve_edge_exposure_gain_and_auto(self):
        self.open()
        self.assertEqual(self.values["AcquisitionFrameRate"], "30")
        before = dict(self.values)
        details = self.service.dispatch("prepare_recording", (.1,))
        self.assertEqual(details["timing_mode"], "external_trigger")
        self.assertEqual(details["camera_operating_fps"], 100)
        self.assertEqual(self.values["TriggerActivation"], "FallingEdge")
        self.assertEqual(self.values["TriggerMode"], "On")
        self.assertEqual(self.values["ExposureAuto"], "Off")
        self.assertEqual(self.values["GainAuto"], "Off")
        self.assertEqual(self.values["Exposure"], "10")
        self.assertEqual(self.values["Gain"], "2")
        self.service.dispatch("restore_preview", ())
        for key, value in before.items():
            self.assertEqual(float(self.values[key]), float(value)) if key == "AcquisitionFrameRate" else self.assertEqual(self.values[key], value)
        self.core.clearCircularBuffer.assert_called()

    def test_too_fast_trigger_interval_is_rejected_and_preview_restored(self):
        self.open()
        with self.assertRaisesRegex(RuntimeError, "between triggers"):
            self.service.dispatch("prepare_recording", (.01,))
        self.assertEqual(self.values["TriggerMode"], "Off")
        self.assertEqual(float(self.values["AcquisitionFrameRate"]), 30)
        self.assertEqual(self.values["GainAuto"], "Continuous")

    def test_missing_trigger_interface_allows_preview_but_rejects_recording(self):
        del self.values["TriggerSource"]
        self.open()
        with self.assertRaisesRegex(RuntimeError, "cannot yet configure external"):
            self.service.dispatch("prepare_recording", (.1,))
        self.assertEqual(self.values["TriggerMode"], "Off")

    def test_trigger_rewrite_on_start_is_rejected(self):
        self.open()
        self.core.startContinuousSequenceAcquisition.side_effect = lambda *a: self.values.__setitem__("TriggerMode", "Off")
        with self.assertRaisesRegex(RuntimeError, "Trigger settings changed"):
            self.service.dispatch("prepare_recording", (.1,))

    def test_buffer_overflow_cannot_silently_drop_images(self):
        self.open()
        self.core.isBufferOverflowed.return_value = True
        with self.assertRaisesRegex(RuntimeError, "buffer overflow"):
            self.service.dispatch("next", ())

    def test_readonly_and_unknown_limits_reach_the_camera_controls(self):
        self.values["GainAuto"] = "Off"
        snapshot = self.open()
        settings = control_snapshot(snapshot)
        self.assertFalse(settings["Gain"]["limits_known"])
        self.assertEqual(settings["Gain"]["unit"], "camera units")
        from ui.control_panels.camera_control_panel import CameraControlPanel
        panel = CameraControlPanel()
        panel.apply_settings(settings)
        self.assertTrue(panel.gain_spin.isEnabled())
        self.assertFalse(panel.gain_slider.isEnabled())
        settings["Gain"]["writable"] = False
        panel.apply_settings(settings)
        self.assertFalse(panel.gain_spin.isEnabled())
        panel.deleteLater()

    def test_native_pixels_are_owned_and_not_relabelled_as_hardware_metadata(self):
        thread = MMCoreCameraThread(profile=self.profile)
        thread._run_id = "run"
        emitted = []
        thread.recording_frame_ready.connect(lambda image, meta: emitted.append((image, meta)))
        pixels = np.full((2, 4), 2048, np.uint16)
        thread._deliver_frame((pixels, 1, 12, {"micro_manager": {"ImageNumber": "123"}}))
        pixels.fill(0)
        image, meta = emitted[0]
        self.assertEqual(image.pixelColor(0, 0).red(), 128)
        self.assertIsNone(meta["camera_frame_id"])
        self.assertIsNone(meta["camera_timestamp_raw"])
        self.assertEqual(meta["_pixels"][0, 0], 2048)
        self.assertEqual(meta["camera_metadata"]["micro_manager"]["ImageNumber"], "123")

    def test_worker_preview_record_preview_cycle_uses_isolated_protocol(self):
        service = self.service
        class Client:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                service.close()
            def request(self, method, *args, **kwargs):
                return service.dispatch(method, args)
        self.core.getRemainingImageCount.return_value = 1
        self.core.popNextImage.return_value = np.full((2, 4), 2048, np.uint16)
        thread = MMCoreCameraThread(profile=self.profile, client_factory=lambda **kw: Client())
        previews, armed, recorded, errors = [], [], [], []
        def ready():
            previews.append(True)
            if len(previews) == 1:
                thread.request_recording("run", .1)
            else:
                thread.stop()
        def frame(image, meta):
            recorded.append(meta)
            thread.request_preview()
        thread.preview_ready.connect(ready)
        thread.timing_ready.connect(lambda run, details: armed.append((run, details)))
        thread.recording_frame_ready.connect(frame)
        thread.error.connect(lambda *args: errors.append(args))
        with patch("threads.mmcore_camera_thread.require_no_other_prima"):
            thread.run()
        self.assertEqual(errors, [])
        self.assertEqual(len(previews), 2)
        self.assertEqual(len(armed), 1)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["run_id"], "run")
        self.assertEqual(self.values["TriggerMode"], "Off")
        self.assertEqual(float(self.values["AcquisitionFrameRate"]), 30)
        self.assertEqual(self.values["GainAuto"], "Continuous")
        self.core.unloadAllDevices.assert_called_once()

    def test_native_16bit_tiff_and_pressure_association_survive_missing_hardware_ids(self):
        self.open()
        details = self.service.dispatch("prepare_recording", (.1,))
        writer = RecordingManager(self.temp.name, run_id="run", initial_counter=0)
        summaries = []
        writer.finalized.connect(summaries.append)
        with patch("recording_manager.MIN_FREE_SPACE_GB", 0):
            writer.start_recording()
        writer.activate(details)
        raw = np.array([[0, 17, 2048, 4095], [3, 16, 2000, 4000]], dtype=np.uint16)
        image, pixels, fmt = copy_mm_frame(raw, 1, 12)
        writer.append_pressure(1, .1, 12.3)
        writer.append_frame(image, {"run_id": "run", "camera_frame_id": None, "camera_timestamp_raw": None,
                                   "_pixels": pixels, "source_bit_depth": 12, "pixel_format": fmt})
        writer.stop_recording()
        self.assertTrue(summaries[0]["complete"], summaries[0]["issues"])
        with tifffile.TiffFile(summaries[0]["tiff_path"]) as tif:
            np.testing.assert_array_equal(tif.pages[0].asarray(), raw)
            metadata = json.loads(tif.pages[0].description)
            self.assertEqual(metadata["frameIdx"], 1)
            self.assertNotIn("_pixels", metadata)
            self.assertIsNone(metadata["camera_frame_id"])
        from playback_window import PlaybackLoader
        frames, errors = [], []
        loader = PlaybackLoader(summaries[0]["tiff_path"], summaries[0]["csv_path"])
        loader.frame_loaded.connect(lambda index, frame, pressure, total: frames.append(frame))
        loader.error.connect(errors.append)
        loader.run()
        self.assertEqual(errors, [])
        np.testing.assert_array_equal(frames[0], raw >> 4)

    def test_color_pixels_are_decoded_and_float_images_rejected(self):
        image, pixels, fmt = copy_mm_frame(np.array([[0x00FF0000]], np.uint32), 4, 8)
        self.assertEqual(image.pixelColor(0, 0).red(), 255)
        self.assertEqual(pixels.tolist(), [[[255, 0, 0]]])
        self.assertEqual(fmt, "RGB8")
        self.assertEqual(display_gray(pixels).shape, (1, 1))
        with self.assertRaises(ValueError):
            copy_mm_frame(np.zeros((2, 4), np.float32), 1, 32)
