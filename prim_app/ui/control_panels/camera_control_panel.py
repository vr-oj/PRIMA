from PyQt5.QtCore import Qt, QSignalBlocker, pyqtSignal
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                            QDoubleSpinBox, QSlider, QCheckBox, QComboBox, QFrame,
                            QPushButton, QMessageBox, QSizePolicy)
from utils.config import DEFAULT_FPS
from utils.recording_settings import CAPTURE_SETTING_OPTIONS, capture_setting_label
from ui.style_constants import PANEL_STYLESHEET


class CameraControlPanel(QWidget):
    """BURST's embedded image controls; property access stays in our worker."""
    setting_requested = pyqtSignal(str, object)
    acquisition_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_recording = False
        self._settings = {}
        self._control_error = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel("IMAGE SETTINGS")
        title.setProperty("cssClass", "microLabel")
        header.addWidget(title)
        header.addStretch()
        self.control_message = QPushButton("Setting not applied…")
        self.control_message.setStyleSheet("color: #f3c969; background: transparent; border: none;")
        self.control_message.clicked.connect(lambda: QMessageBox.warning(self, "Camera setting", self._control_error))
        policy = self.control_message.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.control_message.setSizePolicy(policy)
        self.control_message.hide()
        header.addWidget(self.control_message)
        root.addLayout(header)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)
        self._rows = {}
        self._unit_labels = {}
        for row, (name, label, unit, factor, auto) in enumerate((
            ("ExposureTime", "Exposure", "ms", 1000.0, "ExposureAuto"),
            ("Gain", "Gain", "dB", 1.0, "GainAuto"),
            ("AcquisitionFrameRate", "Preview rate", "fps", 1.0, None))):
            label_widget = QLabel(label)
            label_widget.setProperty("cssClass", "detailLabel")
            label_widget.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            label_widget.setFixedWidth(80)
            grid.addWidget(label_widget, row, 0)
            spin, slider = QDoubleSpinBox(), QSlider(Qt.Horizontal)
            spin.setDecimals(2 if row < 2 else 1)
            spin.setKeyboardTracking(False)
            spin.setProperty("cssClass", "monoInput")
            spin.setFixedWidth(74)
            spin.setMinimumHeight(26)
            slider.setProperty("cssClass", "controlSlider")
            slider.setMinimumWidth(35)
            value_row = QHBoxLayout()
            value_row.setSpacing(4)
            value_row.addWidget(slider, 1)
            value_row.addWidget(spin)
            unit_label = QLabel(unit)
            self._unit_labels[name] = unit_label
            unit_label.setProperty("cssClass", "microLabel")
            unit_label.setFixedWidth(20)
            value_row.addWidget(unit_label)
            grid.addLayout(value_row, row, 1)
            check = QCheckBox("Auto") if auto else None
            if check:
                check.setProperty("cssClass", "muted")
                grid.addWidget(check, row, 2)
                check.toggled.connect(lambda state, key=auto: self.setting_requested.emit(key, "Continuous" if state else "Off"))
            slider.setRange(0, 1000)
            # Stream-affecting rate changes are applied once on release.
            if name == "AcquisitionFrameRate":
                slider.setTracking(False)
            slider.valueChanged.connect(lambda value, key=name: self._slide(key, value))
            spin.valueChanged.connect(lambda value, key=name, scale=factor: self.setting_requested.emit(key, value * scale))
            self._rows[name] = (spin, slider, check, factor, auto)
        self.exposure_spin, self.exposure_slider, self.ae_checkbox, _, _ = self._rows["ExposureTime"]
        self.gain_spin, self.gain_slider, self.ag_checkbox, _, _ = self._rows["Gain"]
        self.framerate_spin, self.framerate_slider, _, _, _ = self._rows["AcquisitionFrameRate"]
        self.framerate_spin.setToolTip("Free-running preview speed. During recording, Arduino pulses determine when images are taken.")
        self.pf_combo = QComboBox()
        self.pf_combo.setProperty("cssClass", "monoInput")
        self.pf_combo.setMinimumHeight(26)
        self.pf_combo.currentTextChanged.connect(lambda value: self.setting_requested.emit("PixelFormat", value))
        label = QLabel("Pixel Format")
        label.setProperty("cssClass", "detailLabel")
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(label, 3, 0)
        grid.addWidget(self.pf_combo, 3, 1)

        self.acquisition_controls = QFrame(self)
        self.acquisition_controls.setProperty("cssClass", "subCard")
        self.acquisition_controls.setMinimumWidth(180)
        acquisition = QVBoxLayout(self.acquisition_controls)
        acquisition.setContentsMargins(10, 8, 10, 8)
        acquisition.setSpacing(6)
        recording_header = QHBoxLayout()
        title = QLabel("RECORDING")
        title.setProperty("cssClass", "sectionLabel")
        recording_header.addWidget(title)
        recording_header.addStretch()
        self.recording_settings_info = QLabel("i")
        self.recording_settings_info.setObjectName("RecordingSettingsInfo")
        self.recording_settings_info.setAlignment(Qt.AlignCenter)
        self.recording_settings_info.setFixedSize(16, 16)
        self.recording_settings_info.setStyleSheet(
            "QLabel#RecordingSettingsInfo { color: #B9C5D0; border: 1px solid #8895A1; "
            "border-radius: 8px; font-size: 11px; font-weight: 600; } "
            "QToolTip { color: #EDF1F5; background-color: #30353C; "
            "border: 1px solid #8895A1; padding: 6px; }")
        self.recording_settings_info.setAccessibleName("Recording settings information")
        recording_hint = (
            "PRIMA applies Pressure rate and Images to the Arduino when recording starts.\n"
            "After recording with None, restart the Arduino box and match its FPS and Capture "
            "to the next recording request.")
        self.recording_settings_info.setToolTip(recording_hint)
        self.recording_settings_info.setToolTipDuration(15000)
        self.recording_settings_info.setAccessibleDescription(recording_hint)
        recording_header.addWidget(self.recording_settings_info)
        acquisition.addLayout(recording_header)
        options = QGridLayout()
        options.setHorizontalSpacing(8)
        options.setVerticalSpacing(8)
        self.sampling_spin = QDoubleSpinBox()
        self.sampling_spin.setRange(0.1, 100.0)
        self.sampling_spin.setDecimals(1)
        self.sampling_spin.setSuffix(" Hz")
        self.sampling_spin.setValue(DEFAULT_FPS)
        self.sampling_spin.setKeyboardTracking(False)
        self.sampling_spin.setProperty("cssClass", "monoInput")
        self.sampling_spin.setToolTip("Set the Arduino pressure sampling rate for the next recording. The interval is rounded up to whole milliseconds.")
        self.capture_setting_combo = QComboBox()
        self.capture_setting_combo.setProperty("cssClass", "monoInput")
        self.capture_setting_combo.setToolTip("Set the Arduino image capture for the next recording. None saves pressure data only.")
        for code, label in CAPTURE_SETTING_OPTIONS:
            self.capture_setting_combo.addItem(label, code)
        self.capture_setting_combo.setCurrentIndex(1)
        for row, (name, control) in enumerate((("Pressure rate", self.sampling_spin), ("Images", self.capture_setting_combo))):
            label = QLabel(name)
            label.setProperty("cssClass", "detailLabel")
            options.addWidget(label, row, 0)
            options.addWidget(control, row, 1)
        acquisition.addLayout(options)
        self.capture_none_notice = QLabel(
            "After recording with None, restart the Arduino box and match its FPS/Capture "
            "to the next recording request.")
        self.capture_none_notice.setWordWrap(True)
        self.capture_none_notice.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.capture_none_notice.setStyleSheet("color: #F3C969; font-size: 10px;")
        self.capture_none_notice.setVisible(False)
        acquisition.addWidget(self.capture_none_notice)
        acquisition.addStretch()
        note = QLabel("Arduino-triggered capture")
        note.setProperty("cssClass", "axisState")
        acquisition.addWidget(note)
        self.sampling_spin.valueChanged.connect(self.acquisition_changed)
        self.capture_setting_combo.currentIndexChanged.connect(self._update_capture_notice)
        self.capture_setting_combo.currentIndexChanged.connect(self.acquisition_changed)
        self.setStyleSheet(PANEL_STYLESHEET)
        self.apply_settings({})

    def _slide(self, name, value):
        spin, _, _, _, _ = self._rows[name]
        spin.setValue(spin.minimum() + (spin.maximum() - spin.minimum()) * value / 1000.0)

    def _update_capture_notice(self):
        self.capture_none_notice.setVisible(self.capture_setting_combo.currentData() == 0)

    def apply_settings(self, settings):
        self._settings = settings
        for name, (spin, slider, check, factor, auto) in self._rows.items():
            data = settings.get(name, {})
            auto_data = settings.get(auto, {})
            blockers = [QSignalBlocker(w) for w in (spin, slider, check) if w is not None]
            valid = "value" in data
            if valid:
                known = data.get("limits_known", True)
                spin.setRange(data["minimum"] / factor if known else -1e9,
                              data["maximum"] / factor if known else 1e9)
                hint = "Free-running preview speed. Arduino pulses determine recording images." if name == "AcquisitionFrameRate" else ""
                spin.setToolTip(hint if known else "The adapter does not report limits. Enter a value; the camera validates it.")
                if not spin.hasFocus():
                    spin.setValue(data["value"] / factor)
                span = spin.maximum() - spin.minimum()
                if not slider.isSliderDown():
                    slider.setValue(round((spin.value() - spin.minimum()) / span * 1000) if span else 0)
            automatic = auto_data.get("value") == "Continuous"
            spin.setEnabled(valid and data.get("writable", True) and not automatic and not self.is_recording)
            slider.setEnabled(spin.isEnabled() and data.get("limits_known", True))
            if name == "Gain":
                unit = data.get("unit", "dB")
                self._unit_labels[name].setText("units" if unit == "camera units" else unit)
                self._unit_labels[name].setFixedWidth(36 if unit == "camera units" else 20)
                self._unit_labels[name].setToolTip(unit)
            if check:
                check.setChecked(automatic)
                check.setEnabled("value" in auto_data and auto_data.get("writable", True) and not self.is_recording)
            del blockers
        blocker = QSignalBlocker(self.pf_combo)
        data = settings.get("PixelFormat", {})
        choices = data.get("choices", [])
        if choices != [self.pf_combo.itemText(i) for i in range(self.pf_combo.count())]:
            self.pf_combo.clear()
            self.pf_combo.addItems(choices)
        self.pf_combo.setCurrentText(data.get("value", ""))
        self.pf_combo.setEnabled(bool(choices) and data.get("writable", True) and not self.is_recording)
        del blocker

    def get_capture_setting(self):
        code = int(self.capture_setting_combo.currentData())
        return code, capture_setting_label(code)

    def set_recording_state(self, recording):
        self.is_recording = bool(recording)
        self.sampling_spin.setEnabled(not recording)
        self.capture_setting_combo.setEnabled(not recording)
        self.apply_settings(self._settings)

    def disable_camera_controls(self):
        self.apply_settings({})

    def show_control_error(self, message):
        self._control_error = message
        self.control_message.setToolTip(message)
        self.control_message.setAccessibleDescription(message)
        self.control_message.setVisible(bool(message))
