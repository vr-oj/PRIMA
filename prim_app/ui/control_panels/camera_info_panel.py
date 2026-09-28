"""BURST's camera-card arrangement with PRIM recording controls."""
import html
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QMessageBox
from ui.style_constants import PANEL_STYLESHEET


class CameraInfoPanel(QWidget):
    def __init__(self, controls, parent=None):
        super().__init__(parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setProperty("cssClass", "panelCard")
        root.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel("Camera")
        title.setProperty("cssClass", "panelTitle")
        header.addWidget(title)
        self.status_badge = QLabel()
        self.status_badge.setProperty("cssClass", "statusBadge")
        self.status_badge.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        header.addWidget(self.status_badge, 1)
        self.resolution_label = QLabel("—")
        self.frame_label = QLabel()
        for label in (self.resolution_label, self.frame_label):
            label.setProperty("cssClass", "detailValue")
            header.addWidget(label)
        self.recording_help_button = QPushButton("Recording help")
        self.recording_help_button.clicked.connect(self.show_recording_help)
        header.addWidget(self.recording_help_button)
        layout.addLayout(header)
        divider = QFrame()
        divider.setProperty("cssClass", "panelDivider")
        layout.addWidget(divider)
        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(controls, 7)
        body.addWidget(controls.acquisition_controls, 3)
        layout.addLayout(body)
        self.setStyleSheet(PANEL_STYLESHEET)
        self.update_status("Disconnected")

    def update_status(self, text):
        connected = text.startswith(("Preview", "Armed"))
        color = "#3FD58F" if connected else "#D6C832"
        self.status_badge.setText(f"<span style='color:{color}'>●</span> {html.escape(text)}")
        self.status_badge.setToolTip(text)
        if text == "Disconnected":
            self.set_recording_details("Recording setup and Arduino-triggered capture help.")

    def set_recording_details(self, text):
        self.recording_help_button.setToolTip(text)

    def update_frame(self, frame_id, width, height):
        self.resolution_label.setText(f"{width} × {height}")
        self.frame_label.setText(f"Frame {frame_id}")
        self.frame_label.setToolTip("Hardware camera frame ID, when supplied by the selected connection")

    def show_recording_help(self):
        QMessageBox.information(self, "Recording with PRIM",
            "Preview rate controls the free-running camera view.\n\n"
            "During recording, Arduino electrical pulses determine when images are taken. "
            "PRIMA verifies the trigger settings exposed by the camera connection. "
            "Where camera rate limits are reported, it selects the highest operating rate "
            "compatible with the exposure.\n\n"
            "Each recording starts with a cleared plot. The finished plot stays available "
            "for review until the next recording or Clear Data.\n\n"
            "Set Pressure rate and Images in PRIMA before recording; these settings are "
            "applied to the Arduino when the run starts. None saves pressure data only. "
            "After recording with None, restart the Arduino box and match its FPS and "
            "Capture to the next recording request. "
            "Long exposures must fit "
            "within the interval between camera triggers.\n\n"
            "The completion popup reports saved samples, images and any integrity issues. "
            "Image/sample counts do not measure the physical pressure-to-exposure offset.")
