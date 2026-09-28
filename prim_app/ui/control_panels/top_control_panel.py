"""BURST's status-strip layout, using PRIM's pressure and command contract."""
import html
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QSizePolicy
from ui.style_constants import PANEL_STYLESHEET
from ui.widgets.metric_card import MetricCard


class TopControlPanel(QWidget):
    zero_requested = pyqtSignal()
    record_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._connected = False
        self._state = "idle"
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setProperty("cssClass", "panelCard")
        root.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(10)
        layout.addLayout(row)
        identity = QWidget()
        identity.setMinimumWidth(218)
        identity_layout = QVBoxLayout(identity)
        identity_layout.setContentsMargins(2, 0, 2, 0)
        identity_layout.setSpacing(4)
        title = QLabel("PRIM Arduino Box Status")
        title.setProperty("cssClass", "panelTitle")
        identity_layout.addWidget(title)
        status_row = QHBoxLayout()
        self.conn_lbl = QLabel()
        self.conn_lbl.setProperty("cssClass", "statusBadge")
        self.conn_lbl.setTextFormat(Qt.RichText)
        status_row.addWidget(self.conn_lbl, 1)
        self.details_toggle = QPushButton("Details ▾")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setProperty("cssClass", "ghost")
        self.details_toggle.toggled.connect(self.set_details_expanded)
        status_row.addWidget(self.details_toggle)
        identity_layout.addLayout(status_row)
        row.addWidget(identity)
        for attr, title_text, initial in (("pres_lbl", "Pressure", "— mmHg"),
                ("time_lbl", "Device time", "— s"), ("idx_lbl", "Trigger counter", "—"),
                ("images_lbl", "Images saved", "—")):
            card = MetricCard(title_text, placeholder=initial)
            setattr(self, attr, card.value_label)
            row.addWidget(card, 1)
        self.record_btn = QPushButton()
        self.record_btn.setProperty("cssClass", "record")
        self.record_btn.setMinimumWidth(170)
        self.record_btn.setMinimumHeight(58)
        self.record_btn.clicked.connect(self.record_requested.emit)
        row.addWidget(self.record_btn, 1)
        self.zero_btn = QPushButton("Zero PRIM")
        self.zero_btn.setProperty("cssClass", "ghost")
        self.zero_btn.setToolTip("Send PRIM's Z command: stop the device and reset its counter/time origin. Never sent automatically.")
        self.zero_btn.clicked.connect(self.zero_requested.emit)
        row.addWidget(self.zero_btn)
        self.details = QLabel("115200 baud · Firmware identity is not reported by this protocol")
        self.details.setProperty("cssClass", "detailLabel")
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        self.set_details_expanded(False)
        self.setStyleSheet(PANEL_STYLESHEET)
        self.update_connection_status("Disconnected", False)
        self.set_recording_state("idle", False)

    def set_details_expanded(self, expanded):
        self.details.setVisible(expanded)
        self.details_toggle.setText("Hide Details ▴" if expanded else "Details ▾")

    def update_connection_status(self, text, connected):
        self._connected = bool(connected)
        color = "#3FD58F" if connected else "#D6C832"
        self.conn_lbl.setText(f"<span style='color:{color}'>●</span> {html.escape(text)}")
        self.zero_btn.setEnabled(self._connected and self._state == "idle")

    def update_prim_data(self, idx, t_dev, p_dev):
        self.idx_lbl.setText(str(idx))
        self.time_lbl.setText(f"{t_dev:.2f} s")
        self.pres_lbl.setText(f"{p_dev:.2f} mmHg")

    def set_recording_state(self, state, enabled):
        self._state = state
        self.record_btn.setText({"idle": "●  Start Recording", "preparing": "Preparing…\nCancel",
                                "recording": "■  Stop Recording", "stopping": "Stopping…",
                                "finalizing": "Finalizing Recording…"}.get(state, state))
        self.record_btn.setEnabled(enabled)
        self.record_btn.setProperty("recordState", state)
        self.record_btn.style().unpolish(self.record_btn)
        self.record_btn.style().polish(self.record_btn)
        self.zero_btn.setEnabled(self._connected and state == "idle")

    def set_acquisition_details(self, text):
        self.details.setText(text + "\nFirmware identity is not reported by PRIM's serial protocol.")
