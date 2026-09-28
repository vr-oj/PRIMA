import os
from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout, QGridLayout, QLabel
from ui.panel_style import PANEL_STYLE


class RecordingCompletionDialog(QDialog):
    playback_requested = pyqtSignal()

    def __init__(self, summary, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recording Complete" if summary["complete"] else "Recording Needs Review")
        self.setMinimumWidth(530)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        self.heading = QLabel("Recording saved" if summary["complete"] else "Incomplete recording — review before analysis")
        self.heading.setProperty("role", "title" if summary["complete"] else "warning")
        self.heading.setWordWrap(True)
        root.addWidget(self.heading)
        metrics = QGridLayout()
        def size(value, divisor, unit):
            return "unavailable" if value is None else f"{value / divisor:.1f} {unit}"
        for row, (label, value) in enumerate((
            ("Pressure samples", f"{summary['samples_written']:,}"),
            ("Images saved / requested", f"{summary['frames_written']:,} / {summary['captures_expected']:,}" if summary["record_video"] else "CSV only"),
            ("Device duration", f"{summary['duration_s']:.2f} s" if summary["duration_s"] is not None else "Unavailable (device time reset or no samples)"),
            ("File sizes", "CSV " + size(summary['csv_size_bytes'], 1024, "KiB")
             + " · TIFF " + size(summary['tiff_size_bytes'], 1048576, "MiB")))):
            metrics.addWidget(QLabel(label), row, 0)
            metrics.addWidget(QLabel(value), row, 1)
        root.addLayout(metrics)
        issues = list(summary["issues"]) + list(summary.get("post_recording_issues", []))
        self.issues_label = QLabel("\n".join("• " + issue for issue in issues))
        self.issues_label.setWordWrap(True)
        self.issues_label.setProperty("role", "warning")
        self.issues_label.setVisible(bool(issues))
        root.addWidget(self.issues_label)
        note = QLabel("Arduino-triggered capture. Count checks do not measure the pressure-to-exposure offset." if summary["record_video"] else "All saved pressure samples are available in the CSV.")
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        root.addWidget(note)
        limitations = summary.get("camera", {}).get("metadata_limitations", "")
        if limitations:
            metadata_note = QLabel(limitations)
            metadata_note.setWordWrap(True)
            metadata_note.setProperty("role", "muted")
            root.addWidget(metadata_note)
        path = QLabel(summary["output_dir"])
        path.setWordWrap(True)
        path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(path)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        folder = buttons.addButton("Open Run Folder", QDialogButtonBox.ActionRole)
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(summary["output_dir"])))
        self.playback_button = buttons.addButton("Open Playback", QDialogButtonBox.ActionRole)
        self.playback_button.setEnabled(summary["complete"] and summary["frames_written"] > 0)
        self.playback_button.clicked.connect(self._open_playback)
        buttons.rejected.connect(self.accept)
        root.addWidget(buttons)
        self.setStyleSheet(PANEL_STYLE)

    def _open_playback(self):
        self.accept()
        self.playback_requested.emit()
