import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prim_app"))
from utils.camera_trigger import TriggerProfile, highest_rate, checked


class Node:
    def __init__(self, value, minimum=1.0, maximum=75.0, choices=()):
        self.value, self.minimum, self.maximum = value, minimum, maximum
        self.entries = [SimpleNamespace(name=name) for name in choices]


class Properties:
    def __init__(self, exposure=10000.0):
        values = {"TriggerSelector": "FrameStart", "TriggerMode": "Off", "TriggerSource": "Any",
                  "TriggerActivation": "FallingEdge", "AcquisitionBurstFrameCount": 2,
                  "ExposureTime": exposure, "Gain": 19.0, "ExposureAuto": "Continuous",
                  "GainAuto": "Continuous", "AcquisitionFrameRate": 10.0,
                  "AcquisitionFrameRateEnable": True}
        self.values = {k: Node(v) for k, v in values.items()}
        self.values["TriggerSource"].entries = [SimpleNamespace(name=n) for n in ("Any", "Line1", "Software")]

    def find(self, name):
        return self.values[name]

    find_float = find_integer = find_enumeration = find_boolean = find


class TriggerTests(unittest.TestCase):
    def test_30fps_rounding_at_tolerance_boundary_arms_and_restores(self):
        class RoundedRate(Node):
            @property
            def value(self):
                return self._value
            @value.setter
            def value(self, value):
                self._value = round(value, 3)
        props = Properties(33333.0)
        props.values["AcquisitionFrameRate"] = RoundedRate(7.0)
        before = {k: n.value for k, n in props.values.items()}
        profile = TriggerProfile(None, props)
        details = profile.prepare(.1)
        self.assertEqual(details["camera_requested_fps"], 30.00030000300003)
        self.assertEqual(details["camera_operating_fps"], 30.0)
        self.assertEqual(props.values["TriggerMode"].value, "On")
        profile.verify()
        props.values["AcquisitionFrameRate"].value = 29.999
        with self.assertRaisesRegex(RuntimeError, "AcquisitionFrameRate changed"):
            profile.verify()
        profile.restore()
        self.assertEqual({k: n.value for k, n in props.values.items()}, before)

    def test_rate_rounding_uses_actual_readback_and_restores_preview(self):
        class RoundedRate(Node):
            @property
            def value(self):
                return self._value
            @value.setter
            def value(self, value):
                self._value = round(value, 3)
        props = Properties(16666.0)
        props.values["AcquisitionFrameRate"] = RoundedRate(7.0, maximum=60.626)
        profile = TriggerProfile(None, props)
        details = profile.prepare(.1)
        self.assertAlmostEqual(details["camera_requested_fps"], 60.002400096)
        self.assertEqual(details["camera_operating_fps"], 60.002)
        profile.verify()
        props.values["AcquisitionFrameRate"].value = 60.001
        with self.assertRaisesRegex(RuntimeError, "AcquisitionFrameRate changed"):
            profile.verify()
        profile.restore()
        self.assertEqual(props.values["AcquisitionFrameRate"].value, 7.0)

    def test_rate_rounding_does_not_relax_other_controls_or_accept_large_errors(self):
        class FixedReadback:
            def __init__(self, value):
                self._value = value
            @property
            def value(self):
                return self._value
            @value.setter
            def value(self, value):
                pass
        for name, desired, actual in (("AcquisitionFrameRate", 60.0024, 60.0),
                                      ("AcquisitionFrameRate", 30.00031, 30.0),
                                      ("AcquisitionFrameRate", 30.00030000300003, float("inf")),
                                      ("AcquisitionFrameRate", 30.00030000300003, float("nan")),
                                      ("ExposureTime", 16666.0, 16666.1),
                                      ("Gain", 10.0, 10.00005),
                                      ("TriggerMode", "On", "Off")):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                checked(FixedReadback(actual), desired, name)

    def test_trigger_budget_uses_rounded_rate_before_arming(self):
        class RoundedRate(Node):
            @property
            def value(self):
                return self._value
            @value.setter
            def value(self, value):
                self._value = round(value, 3)
        for exposure_us, interval_s in ((16666.0, .03333205), (33333.0, .0666661)):
            with self.subTest(exposure_us=exposure_us):
                props = Properties(exposure_us)
                props.values["AcquisitionFrameRate"] = RoundedRate(7.0, maximum=60.626)
                # Requested-rate budget fits; the slightly slower readback does not.
                with self.assertRaisesRegex(RuntimeError, "Actual camera rate"):
                    TriggerProfile(None, props).prepare(interval_s)
                self.assertEqual(props.values["TriggerMode"].value, "Off")
                self.assertEqual(props.values["AcquisitionFrameRate"].value, 7.0)

    def test_10ms_uses_75fps_holds_auto_and_restores_all_settings(self):
        props = Properties()
        before = {k: n.value for k, n in props.values.items()}
        profile = TriggerProfile(None, props)
        details = profile.prepare(0.1)
        self.assertEqual(details["camera_operating_fps"], 75)
        self.assertEqual(props.values["TriggerSource"].value, "Line1")
        self.assertEqual(props.values["ExposureAuto"].value, "Off")
        self.assertEqual(props.values["ExposureTime"].value, 10000)
        profile.restore()
        self.assertEqual({k: n.value for k, n in props.values.items()}, before)

    def test_100ms_exposure_rejects_10hz_and_restores(self):
        props = Properties(100000)
        before = {k: n.value for k, n in props.values.items()}
        with self.assertRaisesRegex(RuntimeError, "shorter exposure"):
            TriggerProfile(None, props).prepare(0.1)
        self.assertEqual({k: n.value for k, n in props.values.items()}, before)

    def test_each_setup_retains_its_configured_trigger_edge(self):
        for edge in ("RisingEdge", "FallingEdge"):
            with self.subTest(edge=edge):
                props = Properties()
                props.values["TriggerActivation"].value = edge
                profile = TriggerProfile(None, props)
                details = profile.prepare(0.1)
                self.assertEqual(props.values["TriggerActivation"].value, edge)
                self.assertEqual(details["trigger_activation"], edge)
                profile.restore()
                self.assertEqual(props.values["TriggerActivation"].value, edge)

    def test_missing_optional_node_only_accepts_feature_not_found(self):
        class Missing(Exception):
            code = 101
        props = Properties()
        original = props.find_boolean
        def optional(name):
            raise Missing()
        props.find_boolean = optional
        sdk = SimpleNamespace(ErrorCode=SimpleNamespace(GenICamFeatureNotFound=101))
        profile = TriggerProfile(sdk, props)
        profile.prepare(0.1)
        profile.restore()
        props.find_boolean = lambda name: (_ for _ in ()).throw(OSError("device lost"))
        with self.assertRaisesRegex(OSError, "device lost"):
            TriggerProfile(sdk, props).prepare(0.1)

    def test_driver_rewrite_after_stream_setup_is_rejected(self):
        props = Properties()
        profile = TriggerProfile(None, props)
        profile.prepare(0.1)
        props.values["TriggerMode"].value = "Off"
        with self.assertRaisesRegex(RuntimeError, "TriggerMode changed"):
            profile.verify()
        profile.restore()

    def test_ambiguous_or_software_only_input_fails(self):
        for choices in (("Software",), ("Line1", "Line2", "Any")):
            with self.subTest(choices=choices):
                props = Properties()
                props.values["TriggerSource"].entries = [SimpleNamespace(name=n) for n in choices]
                with self.assertRaisesRegex(RuntimeError, "physical"):
                    TriggerProfile(None, props).prepare(0.1)
                self.assertEqual(props.values["TriggerMode"].value, "Off")

    def test_rate_respects_discrete_values_and_exposure(self):
        node = Node(10.0)
        node.increment_mode = SimpleNamespace(name="VALUE_SET")
        node.valid_value_set = [5, 10, 30, 60, 75]
        self.assertEqual(highest_rate(node, 20000), 30)

    def test_rate_and_input_follow_capabilities_without_a_model_identifier(self):
        props = Properties()
        props.values["AcquisitionFrameRate"].maximum = 47.5
        props.values["TriggerSource"].value = "Line3"
        props.values["TriggerSource"].entries = [SimpleNamespace(name=n) for n in ("Line1", "Line3", "Software")]
        profile = TriggerProfile(None, props)
        details = profile.prepare(.1)
        self.assertEqual(details["camera_operating_fps"], 47.5)
        self.assertEqual(details["trigger_source"], "Line3")
        profile.restore()
