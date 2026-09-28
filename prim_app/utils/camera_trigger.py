"""IC4 external-trigger preparation. Call only with acquisition stopped.

Rate selection follows BURST's ic4_recording_rate approach. PRIM freezes the
current auto exposure/gain during a run and restores preview settings afterward.
"""
import math


def checked(node, value, name):
    def same(actual):
        if not isinstance(value, float):
            return actual == value
        if name == "AcquisitionFrameRate":
            if not (math.isfinite(actual) and math.isfinite(value)):
                return False
            # IC4 may quantize the requested rate. Keep the 10 ppm limit,
            # allowing one floating-point step for boundary arithmetic:
            # 30.00030000300003 -> 30.0 otherwise fails by ~1.5e-15 fps.
            tolerance = max(1e-7, 1e-5 * max(abs(actual), abs(value)))
            rounding = max(math.ulp(actual), math.ulp(value))
            return abs(actual - value) <= tolerance + rounding
        return math.isclose(actual, value, rel_tol=1e-7, abs_tol=1e-7)
    if not same(node.value):
        node.value = value
    if not same(node.value):
        raise RuntimeError(f"{name}: requested {value!r}, read back {node.value!r}")
    return node.value


def highest_rate(node, exposure_us):
    low, high = float(node.minimum), float(node.maximum)
    if not all(math.isfinite(v) for v in (low, high, exposure_us)) or low <= 0 or high < low or exposure_us <= 0:
        raise RuntimeError("Invalid camera frame-rate or exposure limits")
    high = min(high, 1_000_000.0 / exposure_us)
    mode = getattr(getattr(node, "increment_mode", None), "name", "NONE")
    if mode == "VALUE_SET":
        values = [float(v) for v in node.valid_value_set if low <= float(v) <= high]
        if not values:
            raise RuntimeError("No camera operating rate preserves this exposure")
        return max(values)
    if mode == "INCREMENT":
        step = float(node.increment)
        if not math.isfinite(step) or step <= 0:
            raise RuntimeError("Invalid camera frame-rate increment")
        high = min(high, low + math.floor(math.nextafter((high - low) / step, math.inf)) * step)
    if high < low:
        raise RuntimeError("No camera operating rate preserves this exposure")
    return high


def physical_trigger_source(node):
    """Retain the selected physical line, or choose the sole available line."""
    physical = [entry.name for entry in node.entries
                if entry.name.startswith("Line") and entry.name[4:].isdigit()]
    if node.value in physical:
        return node.value
    if len(physical) == 1:
        return physical[0]
    raise RuntimeError("No unambiguous physical camera trigger input is available")


class TriggerProfile:
    def __init__(self, sdk, props):
        self.sdk, self.props = sdk, props
        self.saved = []
        self.expected = []
        self.details = {}

    def node(self, kind, name, optional=False):
        try:
            return getattr(self.props, "find_" + kind)(name)
        except Exception as exc:
            if optional and getattr(exc, "code", None) == self.sdk.ErrorCode.GenICamFeatureNotFound:
                return None
            raise

    def remember(self, kind, name, optional=False):
        node = self.node(kind, name, optional)
        if node is not None:
            self.saved.append((name, node, node.value))
        return node

    def set_expected(self, name, node, value):
        actual = checked(node, value, name)
        # Subsequent verification compares against the accepted readback,
        # not the unrepresentable request; no ongoing drift is allowed.
        self.expected.append((name, node, actual))
        return actual

    def prepare(self, trigger_interval_s):
        if self.saved:
            raise RuntimeError("Camera is already prepared")
        selector = self.node("enumeration", "TriggerSelector")
        if selector.value != "FrameStart":
            raise RuntimeError("Choose FrameStart in the camera configuration before recording")
        mode = self.remember("enumeration", "TriggerMode")
        source = self.remember("enumeration", "TriggerSource")
        activation = self.remember("enumeration", "TriggerActivation")
        burst = self.remember("integer", "AcquisitionBurstFrameCount", True)
        exposure_auto = self.remember("enumeration", "ExposureAuto", True)
        gain_auto = self.remember("enumeration", "GainAuto", True)
        exposure = self.remember("float", "ExposureTime")
        gain = self.remember("float", "Gain", True)
        rate = self.remember("float", "AcquisitionFrameRate")
        enabled = self.remember("boolean", "AcquisitionFrameRateEnable", True)
        try:
            chosen = physical_trigger_source(source)
            edge = activation.value
            if edge not in ("RisingEdge", "FallingEdge"):
                raise RuntimeError("Select a rising or falling trigger edge for this camera before recording")
            checked(mode, "Off", "TriggerMode")
            for name, node in (("ExposureAuto", exposure_auto), ("GainAuto", gain_auto)):
                if node is not None:
                    self.set_expected(name, node, "Off")
            exposure_us = float(exposure.value)
            gain_value = float(gain.value) if gain is not None else None
            rate_fps = highest_rate(rate, exposure_us)
            # Conservative non-overlapping budget. Passing is not an electrical
            # timing measurement; actual pulse/image counts are still checked.
            budget = exposure_us / 1_000_000.0 + 1.0 / rate_fps
            if trigger_interval_s < budget:
                raise RuntimeError(
                    f"Exposure {exposure_us / 1000:g} ms and camera rate {rate_fps:g} fps "
                    f"need at least {budget * 1000:.1f} ms between triggers. "
                    "Choose a shorter exposure or a lower image capture rate; PRIM has not started.")
            if enabled is not None:
                self.set_expected("AcquisitionFrameRateEnable", enabled, False)
            requested_rate = rate_fps
            rate_fps = float(self.set_expected("AcquisitionFrameRate", rate, rate_fps))
            self.set_expected("ExposureTime", exposure, exposure_us)
            if gain is not None:
                self.set_expected("Gain", gain, gain_value)
            actual_budget = exposure_us / 1_000_000.0 + 1.0 / rate_fps
            if trigger_interval_s < actual_budget:
                raise RuntimeError(
                    f"Actual camera rate {rate_fps:g} fps needs at least "
                    f"{actual_budget * 1000:.3f} ms between triggers at this exposure; PRIM has not started.")
            self.expected.append(("TriggerSelector", selector, "FrameStart"))
            self.set_expected("TriggerSource", source, chosen)
            self.set_expected("TriggerActivation", activation, edge)
            if burst is not None:
                self.set_expected("AcquisitionBurstFrameCount", burst, 1)
            self.set_expected("TriggerMode", mode, "On")
            self.details = {"timing_mode": "external_trigger", "trigger_source": chosen,
                            "trigger_activation": edge,
                            "camera_operating_fps": rate_fps, "exposure_us": exposure_us,
                            "camera_requested_fps": requested_rate,
                            "trigger_interval_s": trigger_interval_s,
                            "auto_controls_held": True,
                            "timestamp_units": "raw SDK device_timestamp_ns; not calibrated"}
            self.verify()
            return dict(self.details)
        except Exception:
            self.restore()
            raise

    def verify(self):
        for name, node, value in self.expected:
            actual = node.value
            valid = math.isclose(actual, value, rel_tol=1e-7, abs_tol=1e-7) if isinstance(value, float) else actual == value
            if not valid:
                raise RuntimeError(f"Camera {name} changed during recording preparation: {actual!r}")

    def restore(self):
        saved = {name: (node, value) for name, node, value in self.saved}
        errors = []
        actions = []
        for name in ("TriggerMode", "ExposureAuto", "GainAuto"):
            if name in saved:
                actions.append((name, saved[name][0], "Off"))
        for name in ("AcquisitionFrameRate", "AcquisitionFrameRateEnable", "ExposureTime", "Gain",
                     "TriggerSource", "TriggerActivation", "AcquisitionBurstFrameCount",
                     "ExposureAuto", "GainAuto", "TriggerMode"):
            if name in saved:
                actions.append((name, *saved[name]))
        for name, node, value in actions:
            try:
                checked(node, value, name)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("Camera preview restoration failed: " + "; ".join(errors))
        self.saved.clear()
        self.expected.clear()
