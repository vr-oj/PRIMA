"""Hold image controls and validate external capture before PRIM receives G."""
import math


class RecordingProfile:
    def __init__(self, service):
        self.service = service
        self.saved = {}

    def prepare(self, interval_s):
        service, adapter = self.service, self.service.adapter
        if not math.isfinite(interval_s) or interval_s <= 0:
            raise ValueError("Invalid trigger interval")
        service.session.stop()
        controls = adapter.read_controls()
        self.saved = {name: controls[name].value for name in
                      ("exposure", "gain", "fps", "auto_exposure", "auto_gain") if name in controls}
        try:
            ambiguous_auto = [name for name in ("auto_exposure", "auto_gain") if name in adapter.issues]
            if ambiguous_auto:
                raise RuntimeError("Map automatic camera controls before recording: " + ", ".join(ambiguous_auto))
            for name in ("auto_exposure", "auto_gain"):
                if name in controls and controls[name].value != "Off":
                    adapter.set_value(name, "Off")
                    if adapter.read_controls()[name].value != "Off":
                        raise RuntimeError(f"Could not hold {name} during recording")
            controls = adapter.read_controls()
            if "exposure" not in controls:
                raise RuntimeError("Map the camera's exposure control before externally triggered recording.")
            exposure = float(controls["exposure"].value)
            if not math.isfinite(exposure) or exposure <= 0:
                raise RuntimeError("Invalid camera exposure")
            rate = controls.get("fps")
            if rate and rate.writable and rate.limits_known and rate.maximum > rate.minimum:
                desired = min(rate.maximum, 1e6 / exposure)
                if rate.choices:
                    choices = [float(v) for v in rate.choices if rate.minimum <= float(v) <= desired]
                    if not choices:
                        raise RuntimeError("No camera rate preserves the selected exposure")
                    desired = max(choices)
                elif rate.increment > 0:
                    desired = rate.minimum + math.floor((desired - rate.minimum) / rate.increment) * rate.increment
                if rate.value_type == "int":
                    desired = math.floor(desired)
                adapter.set_value("fps", desired)
            # The helper rechecks mapped trigger settings after starting the
            # sequence; no PRIM command is involved in this operation.
            snapshot = service.dispatch("timing", ("auto",))
            controls = snapshot["controls"]
            actual_exposure = float(controls["exposure"].value)
            if not math.isclose(actual_exposure, exposure, rel_tol=1e-7, abs_tol=1e-7):
                raise RuntimeError("Exposure changed while arming the camera")
            rate = controls.get("fps")
            actual_rate = float(rate.value) if rate else None
            if actual_rate is not None and (not math.isfinite(actual_rate) or actual_rate <= 0):
                raise RuntimeError("Invalid camera operating rate")
            budget = exposure / 1e6 + (1 / actual_rate if actual_rate else 0)
            if interval_s < budget:
                raise RuntimeError(f"Camera needs at least {budget * 1000:.3f} ms between triggers at this exposure; PRIM has not started.")
            source = snapshot.get("trigger_input") or {}
            return {"camera_backend": "micromanager", "timing_mode": "external_trigger",
                    "trigger_source": source.get("name", "Configured external input"),
                    "trigger_readback": snapshot["trigger_configuration"], "trigger_input": source,
                    "camera_operating_fps": actual_rate, "exposure_us": exposure,
                    "trigger_interval_s": interval_s, "auto_controls_held": True,
                    "hardware_metadata_required": False,
                    "metadata_limitations": "MMCore tags are not hardware frame IDs or exposure timestamps; matching checks counts and order.",
                    "rate_limitations": "Camera rate not exposed; only exposure budget checked" if rate is None else "",
                    "adapter": snapshot["diagnostics"]}
        except Exception:
            self.restore()
            raise

    def restore(self):
        service = self.service
        service.dispatch("timing", ("",))
        errors = []
        for name, value in self.saved.items():
            try:
                current = service.adapter.read_controls().get(name)
                if current is None:
                    raise RuntimeError("control disappeared")
                if current.value != value:
                    service.adapter.set_value(name, value)
                actual = service.adapter.read_controls()[name].value
                matches = (math.isclose(float(actual), value, rel_tol=1e-5, abs_tol=1e-7)
                           if isinstance(value, (float, int)) else actual == value)
                if not matches:
                    raise RuntimeError(f"requested {value}, read back {actual}")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if errors:
            raise RuntimeError("Camera preview restoration failed: " + "; ".join(errors))
        self.saved.clear()
        return service.dispatch("snapshot", ())
