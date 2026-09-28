"""Detached capability snapshots shared with the camera helper."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CameraControl:
    value: float | str
    minimum: float = 0
    maximum: float = 0
    increment: float = 0
    choices: tuple[str, ...] = ()
    writable: bool = True
    unit: str = ""
    value_type: str = "float"
    limits_known: bool = True
    requires_stop: bool = False
