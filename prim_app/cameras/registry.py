"""Optional native IC4 and Micro-Manager connections for PRIMA."""
import importlib
import importlib.util
import logging
from .models import CameraDeviceInfo, CameraMode
from .micro_manager_backend import MicroManagerBackend

log = logging.getLogger(__name__)


class IC4Backend:
    def __init__(self, sdk):
        self.sdk = sdk

    def discover(self):
        return [CameraDeviceInfo("ic4", str(d.unique_name),
                f"{d.model_name} (S/N: {d.serial}) — IC4", serial=d.serial, native_info=d)
                for d in self.sdk.DeviceEnum.devices()]

    def list_modes(self, device):
        from utils.camera_access import require_no_other_prima
        require_no_other_prima()
        grabber = self.sdk.Grabber()
        try:
            grabber.device_open(device.native_info)
            props = grabber.device_property_map
            width, height = props.find_integer("Width").value, props.find_integer("Height").value
            pixel = props.find_enumeration("PixelFormat")
            choices = [entry.name for entry in pixel.entries]
            choices.sort(key=lambda name: name != pixel.value)
            return [CameraMode(width, height, name) for name in choices]
        finally:
            if grabber.is_device_open:
                grabber.device_close()

    def create_thread(self, device, parent=None):
        from threads.sdk_camera_thread import SDKCameraThread
        thread = SDKCameraThread(parent)
        thread.set_device_info(device.native_info)
        return thread


class CameraRegistry:
    def __init__(self):
        self.backends = {}
        self.unavailable = {}
        try:
            self.backends["ic4"] = IC4Backend(importlib.import_module("imagingcontrol4"))
        except Exception as exc:
            self.unavailable["ic4"] = str(exc)
        try:
            if importlib.util.find_spec("pymmcore") is None:
                raise ImportError("pymmcore is not installed")
            self.backends["micromanager"] = MicroManagerBackend(True)
        except Exception as exc:
            self.unavailable["micromanager"] = (
                "The Micro-Manager Python bridge is unavailable. Install PRIMA's camera requirements "
                "in the Python environment used to launch PRIMA, or use a build including pymmcore. " + str(exc))

    def set_micro_manager_profiles(self, profiles):
        backend = self.backends.get("micromanager")
        if backend:
            backend.profiles = [dict(p) for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []

    def discover_cameras(self):
        devices = []
        for name, backend in self.backends.items():
            try:
                devices.extend(backend.discover())
            except Exception as exc:
                log.warning("Camera discovery for %s: %s", name, exc)
        return devices

    def list_modes(self, device):
        return self.backends[device.backend].list_modes(device)

    def get_thread(self, device, parent=None):
        return self.backends[device.backend].create_thread(device, parent)
