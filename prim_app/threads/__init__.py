"""Load camera SDKs only when their connection is requested."""
from importlib import import_module

__all__ = [
    "SerialThread",
    "SDKCameraThread",
    "MicroManagerCameraThread",
    "MMCoreCameraThread",
]


def __getattr__(name):
    modules = {"SerialThread": "serial_thread", "SDKCameraThread": "sdk_camera_thread",
               "MicroManagerCameraThread": "micromanager_camera_thread", "MMCoreCameraThread": "mmcore_camera_thread"}
    if name not in modules:
        raise AttributeError(name)
    return getattr(import_module("." + modules[name], __name__), name)
