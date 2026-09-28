# prim_app/main_window.py

import os
import sys
import re
import logging
import csv
import json
from datetime import datetime
import subprocess
import time
import uuid

from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QFormLayout,
    QDockWidget,
    QTextEdit,
    QToolBar,
    QStatusBar,
    QAction,
    QFileDialog,
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QComboBox,
    QLabel,
    QPushButton,
    QMessageBox,
    QSizePolicy,
    QTabWidget,
    QGroupBox,
    QDoubleSpinBox,
    QCheckBox,
    QHBoxLayout,
    QFrame,
    QSplitter,
)
from PyQt5.QtCore import (
    Qt,
    pyqtSlot,
    QTimer,
    QVariant,
    QSize,
    QThread,
    QMetaObject,
    pyqtSignal,
    Q_ARG,
)
from PyQt5.QtGui import QIcon, QKeySequence, QImage, QDesktopServices
from PyQt5.QtCore import QUrl

import prim_app

from utils.app_settings import (
    save_app_setting,
    load_app_setting,
    SETTING_LAST_CAMERA_INDEX,
    SETTING_RESULTS_DIR,
    SETTING_OPEN_FOLDER_PROMPT,
)
import utils.config as config
from utils.config import (
    DEFAULT_FPS,
    DEFAULT_FRAME_SIZE,
    APP_NAME,
    APP_VERSION,
    set_results_dir,
    DEFAULT_VIDEO_EXTENSION,
    DEFAULT_VIDEO_CODEC,
    ABOUT_TEXT,
    PLOT_DEFAULT_Y_MIN,
    PLOT_DEFAULT_Y_MAX,
)
from utils.recording_settings import (
    DEFAULT_CAPTURE_SETTING_CODE,
    build_recording_settings,
    format_prim_command,
)
from utils.path_helpers import get_next_fill_folder, resource_path
from ui.canvas.qtcamera_widget import QtCameraWidget
from ui.control_panels.camera_control_panel import CameraControlPanel
from ui.control_panels.top_control_panel import TopControlPanel
from ui.control_panels.plot_control_panel import PlotControlPanel
from ui.canvas.pressure_plot_widget import PressurePlotWidget

from threads.serial_thread import SerialThread
from cameras.registry import CameraRegistry
from recording_manager import RecordingManager
from utils.utils import list_serial_ports
from playback_window import PlaybackWindow
from ui.style_constants import PANEL_STYLESHEET
from ui.control_panels.camera_info_panel import CameraInfoPanel
from ui.recording_completion_dialog import RecordingCompletionDialog

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    activate_recorder = pyqtSignal(object)
    recording_issue = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        # ─── State Variables ─────────────────────────────────────────────────────
        self._serial_thread = None
        self._serial_active = False
        self._recorder_thread = None
        self._recorder_worker = None
        self._current_fill_folder = None
        self._last_recording_paths = {"tiff": None, "csv": None}
        self._active_recording_settings = None
        self._recording_state = "idle"
        self._camera_ready = False
        self._camera_armed = False
        self._preview_restoring = False
        self._start_sent = False
        self._zero_before_start_pending = False
        self._stop_sent = False
        self._run_id = None
        self._serial_counter = None
        self._last_serial_activity = None
        self._completion_summary = None
        self._completion_dialog = None
        self._closing = False
        self.camera_registry = CameraRegistry()
        self.camera_registry.set_micro_manager_profiles(load_app_setting("micro_manager_profiles", []))

        # Camera‐related
        self.device_combo = None
        self.resolution_combo = None
        self.btn_start_camera = None
        self.camera_widget = None
        self.camera_control_panel = None
        self.camera_tabs = None
        self.camera_thread = None

        # Plot controls
        self.plot_control_panel = None

        # Top control (Arduino status)
        self.top_ctrl = None


        # Plotting
        self.pressure_plot_widget = None

        # Other UI
        self.lbl_cam_connection = None
        self.lbl_cam_frame = None
        self.lbl_cam_resolution = None

        self._init_paths_and_icons()
        self._build_console_log_dock()
        self._build_central_widget_layout()
        self._build_menus()
        self._build_main_toolbar()
        self._build_status_bar()

        # Populate device list so user can select camera
        self._populate_device_list()
        self._set_initial_control_states()

        self.setWindowTitle(f"{APP_NAME} - v{APP_VERSION}")
        log.info("MainWindow initialized.")
        self.showMaximized()

    # ─── UI Builders ────────────────────────────────────────────────────────

    def _init_paths_and_icons(self):
        base = resource_path()
        icon_dir = os.path.join(base, "ui", "icons")
        if not os.path.isdir(icon_dir):
            alt_icon_dir = os.path.join(
                os.path.dirname(base), "prim_app", "ui", "icons"
            )
            if os.path.isdir(alt_icon_dir):
                icon_dir = alt_icon_dir
            else:
                another_alt_icon_dir = os.path.join(
                    os.path.dirname(base), "ui", "icons"
                )
                if os.path.isdir(another_alt_icon_dir):
                    icon_dir = another_alt_icon_dir
                else:
                    log.warning(
                        f"Icon directory not found. Looked in: {icon_dir}, {alt_icon_dir}, {another_alt_icon_dir}"
                    )

        def get_icon(name):
            path = os.path.join(icon_dir, name)
            return QIcon(path) if os.path.exists(path) else QIcon()

        self.icon_record_start = get_icon("record.svg")
        self.icon_record_stop = get_icon("stop.svg")
        self.icon_recording_active = get_icon("recording_active.svg")
        self.icon_connect = get_icon("plug.svg")
        self.icon_disconnect = get_icon("plug_disconnect.svg")
        self.icon_refresh = get_icon("sync.svg")
        self.icon_playback = get_icon("image.svg")

    def _build_console_log_dock(self):
        self.dock_console = QDockWidget("Console Log", self)
        self.dock_console.setObjectName("ConsoleLogDock")
        self.dock_console.setAllowedAreas(
            Qt.BottomDockWidgetArea | Qt.TopDockWidgetArea
        )
        console_widget = QWidget()
        layout = QVBoxLayout(console_widget)
        self.console_out_textedit = QTextEdit(readOnly=True)
        self.console_out_textedit.setFontFamily("monospace")
        layout.addWidget(self.console_out_textedit)
        self.dock_console.setWidget(console_widget)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.dock_console)
        self.dock_console.setVisible(False)

    def _build_central_widget_layout(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)
        self.top_ctrl = TopControlPanel(self)
        self.top_ctrl.zero_requested.connect(self._on_zero_prim)
        self.top_ctrl.record_requested.connect(self._toggle_recording)
        root.addWidget(self.top_ctrl, 0)

        self.device_combo = QComboBox()
        self.device_combo.addItem("Choose camera…", None)
        self.device_combo.currentIndexChanged.connect(self._on_device_selected)
        self.device_combo.activated.connect(self._on_camera_choice_activated)
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItem("Choose resolution…", None)
        for combo in (self.device_combo, self.resolution_combo):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(16)
            combo.view().setStyleSheet(
                "QAbstractItemView { background: #454545; color: #ffffff; "
                "selection-background-color: #3DBD7D; selection-color: #0B1014; }")
            combo.currentTextChanged.connect(combo.setToolTip)
        self.btn_start_camera = QPushButton("Start Camera")
        self.btn_start_camera.setProperty("cssClass", "primary")
        self.btn_start_camera.clicked.connect(self._on_start_stop_camera)

        self.workspace_splitter = QSplitter(Qt.Horizontal, central)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(6)
        camera_workspace = QWidget()
        camera_workspace.setObjectName("CameraWorkspace")
        camera_layout = QVBoxLayout(camera_workspace)
        camera_layout.setContentsMargins(0, 0, 0, 0)
        camera_layout.setSpacing(6)
        self.camera_control_panel = CameraControlPanel(self)
        self.camera_control_panel.setting_requested.connect(self._set_camera_setting)
        self.camera_control_panel.acquisition_changed.connect(self._refresh_recording_button_states)
        self.camera_info_panel = CameraInfoPanel(self.camera_control_panel, self)
        self.lbl_cam_connection = self.camera_info_panel.status_badge
        self.lbl_cam_frame = self.camera_info_panel.frame_label
        self.lbl_cam_resolution = self.camera_info_panel.resolution_label
        self.camera_widget = QtCameraWidget(self)
        self.camera_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        camera_layout.addWidget(self.camera_info_panel, 0)
        camera_layout.addWidget(self.camera_widget, 1)

        plot_workspace = QWidget()
        plot_workspace.setObjectName("PlotWorkspace")
        plot_layout = QVBoxLayout(plot_workspace)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.setSpacing(6)
        self.plot_control_panel = PlotControlPanel(self)
        self.pressure_plot_widget = PressurePlotWidget(self)
        self.pressure_plot_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        plot_layout.addWidget(self.plot_control_panel, 0)
        plot_layout.addWidget(self.pressure_plot_widget, 1)
        self.workspace_splitter.addWidget(camera_workspace)
        self.workspace_splitter.addWidget(plot_workspace)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([1000, 1000])
        root.addWidget(self.workspace_splitter, 1)
        for signal, method in (("autoscale_x_changed", "set_auto_scale_x"),
                ("autoscale_y_changed", "set_auto_scale_y"), ("x_axis_limits_changed", "set_manual_x_limits"),
                ("y_axis_limits_changed", "set_manual_y_limits"), ("export_plot_image_requested", "export_as_image"),
                ("clear_plot_requested", "clear_plot")):
            if hasattr(self.pressure_plot_widget, method):
                getattr(self.plot_control_panel, signal).connect(getattr(self.pressure_plot_widget, method))
        self.plot_control_panel.reset_zoom_requested.connect(lambda: self.pressure_plot_widget.reset_zoom(
            self.plot_control_panel.is_autoscale_x(), self.plot_control_panel.is_autoscale_y()))
        self.setCentralWidget(central)
        QTimer.singleShot(0, self._equalize_workspace_panels)
        QTimer.singleShot(250, self._equalize_workspace_panels)

    def _equalize_workspace_panels(self):
        """Match BURST's control-card heights so the live panes align."""
        height = max(self.camera_info_panel.sizeHint().height(),
                     self.plot_control_panel.sizeHint().height())
        self.camera_info_panel.setFixedHeight(height)
        self.plot_control_panel.setFixedHeight(height)

    @staticmethod
    def _fit_combo_popup(combo):
        """Use BURST's compact fields with popup widths fitted to full names."""
        if combo.count():
            width = max(combo.fontMetrics().horizontalAdvance(combo.itemText(i))
                        for i in range(combo.count()))
            combo.view().setMinimumWidth(max(combo.minimumWidth(), min(width + 52, 620)))

    # ─── Camera Device & Resolution Enumeration ─────────────────────────────
    def _populate_device_list(self):
        previous = self.device_combo.currentData()
        device_list = self.camera_registry.discover_cameras()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("Choose camera…", None)
        for dev in device_list:
            self.device_combo.addItem(dev.display_name, dev)
            if getattr(previous, "backend", None) == dev.backend and getattr(previous, "id", None) == dev.id:
                self.device_combo.setCurrentIndex(self.device_combo.count() - 1)
        self.device_combo.addItem("Micro-Manager Camera Setup…", "micromanager_setup")
        self.device_combo.blockSignals(False)
        self._on_device_selected(self.device_combo.currentIndex())
        self._fit_combo_popup(self.device_combo)

    def _refresh_serial_port_list(self):
        ports = list_serial_ports()
        self.serial_port_combobox.clear()
        if ports:
            for p_dev, p_desc in ports:
                self.serial_port_combobox.addItem(
                    f"{os.path.basename(p_dev)} ({p_desc})", QVariant(p_dev)
                )
            self.serial_port_combobox.setEnabled(True)
        else:
            self.serial_port_combobox.addItem("No Serial Ports Found", QVariant())
            self.serial_port_combobox.setEnabled(False)

    @pyqtSlot()
    def _refresh_device_lists(self):
        """Re-enumerate cameras and serial ports."""
        self._populate_device_list()
        self._refresh_serial_port_list()
        self.statusBar().showMessage("Device lists refreshed", 3000)

    def _on_device_selected(self, index):
        if self.camera_thread is not None:
            return
        info = self.device_combo.itemData(index)
        self.resolution_combo.clear()
        self.resolution_combo.addItem("Choose resolution…", None)
        if not info or info == "micromanager_setup":
            return
        try:
            for mode in self.camera_registry.list_modes(info):
                self.resolution_combo.addItem(mode.display_name, mode.as_tuple())
            if self.resolution_combo.count() > 1:
                self.resolution_combo.setCurrentIndex(1)
            self._fit_combo_popup(self.resolution_combo)
        except Exception as exc:
            self.statusBar().showMessage(f"Camera information: {exc}", 8000)

    def _on_camera_choice_activated(self, index):
        if self.device_combo.itemData(index) == "micromanager_setup":
            self._setup_micro_manager()

    def _setup_micro_manager(self):
        if self._recording_state != "idle" or self.camera_thread is not None:
            return
        from ui.micro_manager_setup import MicroManagerSetupDialog
        backend = self.camera_registry.backends.get("micromanager")
        dialog = MicroManagerSetupDialog(backend.sdk if backend else None,
            load_app_setting("micro_manager_profiles", []), self,
            self.camera_registry.unavailable.get("micromanager", ""))
        if dialog.exec_() == QDialog.Accepted:
            save_app_setting("micro_manager_profiles", dialog.profiles)
            self.camera_registry.set_micro_manager_profiles(dialog.profiles)
        self._populate_device_list()

    def _on_start_stop_camera(self):
        if self._recording_state != "idle":
            return
        if self.camera_thread is not None:
            self.camera_thread.stop()
            self.btn_start_camera.setEnabled(False)
            return
        info, resolution = self.device_combo.currentData(), self.resolution_combo.currentData()
        if info is None or info == "micromanager_setup" or resolution is None:
            QMessageBox.warning(self, "Camera", "Select a camera and format first.")
            return
        camera = self.camera_registry.get_thread(info, self)
        self.camera_thread = camera
        camera.set_resolution(resolution)
        camera.grabber_ready.connect(self._on_grabber_ready)
        camera.frame_ready.connect(self.camera_widget._on_frame_ready)
        camera.frame_ready.connect(self._update_camera_info)
        camera.settings_ready.connect(self.camera_control_panel.apply_settings)
        camera.control_error.connect(self.camera_control_panel.show_control_error)
        camera.timing_ready.connect(self._on_camera_armed)
        camera.timing_failed.connect(self._on_camera_arm_failed)
        camera.preview_ready.connect(self._on_preview_ready)
        camera.error.connect(self._on_camera_error)
        camera.finished.connect(self._on_camera_finished)
        self.camera_info_panel.update_status("Connecting…")
        self.camera_control_panel.disable_camera_controls()
        self.device_combo.setEnabled(False)
        self.resolution_combo.setEnabled(False)
        self.btn_start_camera.setText("Stop Camera")
        camera.start()

    def _on_grabber_ready(self):
        self._camera_ready = True
        self.camera_info_panel.update_status("Preview · free-running")
        self._refresh_recording_button_states()

    def _set_camera_setting(self, name, value):
        if self.camera_thread and self._recording_state == "idle":
            self.camera_control_panel.show_control_error("")
            if name in ("AcquisitionFrameRate", "PixelFormat"):
                self._camera_ready = False
                self.camera_info_panel.update_status("Updating preview…")
                self._refresh_recording_button_states()
            self.camera_thread.request_setting(name, value)

    def _on_camera_finished(self):
        camera = self.camera_thread
        self.camera_thread = None
        self._camera_ready = self._camera_armed = self._preview_restoring = False
        self.camera_control_panel.disable_camera_controls()
        self.camera_info_panel.update_status("Disconnected")
        self.btn_start_camera.setText("Start Camera")
        self.btn_start_camera.setEnabled(True)
        self.device_combo.setEnabled(True)
        self.resolution_combo.setEnabled(True)
        self.camera_widget.clear_image()
        if camera:
            camera.deleteLater()
        self._finish_recording_ui()
        self._refresh_recording_button_states()

    def _on_preview_ready(self):
        self._camera_ready = True
        self._camera_armed = False
        self._preview_restoring = False
        self.camera_info_panel.update_status("Preview · free-running")
        self.camera_info_panel.set_recording_details("Preview is free-running; recording follows Arduino trigger pulses.")
        self._finish_recording_ui()
        self._refresh_recording_button_states()

    def _on_camera_armed(self, run_id, details):
        if run_id != self._run_id or self._recording_state != "preparing":
            return
        self._camera_armed = True
        self.camera_info_panel.update_status("Armed · " + details["trigger_source"])
        rate = details.get("camera_operating_fps")
        rate_text = f"{rate:g} fps" if rate is not None else "rate not reported"
        self.camera_info_panel.set_recording_details(
            f"Arduino triggered · camera {rate_text} · "
            f"exposure {details['exposure_us'] / 1000:g} ms held during recording")
        self._start_acquisition(details)

    def _on_camera_arm_failed(self, run_id, message):
        if run_id == self._run_id:
            self._handle_recorder_error(message)

    @pyqtSlot(QImage, object)
    def _update_camera_info(self, image: QImage, raw):
        """Display the actual camera ID; preview delivery is throttled."""
        self.camera_info_panel.update_frame(raw.get("camera_frame_id") if raw.get("camera_frame_id") is not None else "—", image.width(), image.height())
        device = self.device_combo.currentData()
        if getattr(device, "backend", None) == "micromanager":
            index = self.resolution_combo.currentIndex()
            self.resolution_combo.setItemText(index, f"{image.width()} × {image.height()} (Configuration)")


    def _on_camera_error(self, message, code):
        self._camera_ready = False
        self.statusBar().showMessage("Camera: " + message, 10000)
        self.camera_control_panel.show_control_error(message)
        if self._recording_state != "idle":
            self._handle_recorder_error("Camera: " + message)
        if self.camera_thread:
            self.camera_thread.stop()

    def _build_menus(self):
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        exp_data_act = QAction(
            "Export Plot &Data (CSV)…", self, triggered=self._export_plot_data_as_csv
        )
        fm.addAction(exp_data_act)
        exp_img_act = QAction("Export Plot &Image…", self)
        exp_img_act.triggered.connect(self.pressure_plot_widget.export_as_image)
        fm.addAction(exp_img_act)
        playback_act = QAction(
            "Open &Playback Window…", self, triggered=lambda: self.open_playback_window(True)
        )
        fm.addAction(playback_act)
        choose_dir_act = QAction(
            "Set &Results Folder…", self, triggered=self._choose_results_dir
        )
        fm.addAction(choose_dir_act)
        fm.addSeparator()
        exit_act = QAction(
            "&Exit", self, shortcut=QKeySequence.Quit, triggered=self.close
        )
        fm.addAction(exit_act)

        am = mb.addMenu("&Acquisition")
        self.start_recording_action = QAction(
            self.icon_record_start,
            "Start &Recording",
            self,
            shortcut=Qt.CTRL | Qt.Key_R,
            triggered=self._on_start_recording,
            enabled=False,
        )
        am.addAction(self.start_recording_action)
        self.stop_recording_action = QAction(
            self.icon_record_stop,
            "Stop R&ecording",
            self,
            shortcut=Qt.CTRL | Qt.Key_T,
            triggered=self._on_stop_recording,
            enabled=False,
        )
        am.addAction(self.stop_recording_action)

        vm = mb.addMenu("&View")
        if hasattr(self, "dock_console") and self.dock_console:
            vm.addAction(self.dock_console.toggleViewAction())

        pm = mb.addMenu("&Plot")
        clear_plot_act = QAction(
            "&Clear Plot Data", self, triggered=self._clear_pressure_plot
        )
        pm.addAction(clear_plot_act)

        def trigger_reset_zoom():
            if hasattr(self.pressure_plot_widget, "reset_zoom"):
                self.pressure_plot_widget.reset_zoom(
                    self.plot_control_panel.is_autoscale_x(),
                    self.plot_control_panel.is_autoscale_y(),
                )
            else:
                log.warning("reset_zoom() not found on PressurePlotWidget")

        reset_zoom_act = QAction("&Reset Plot Zoom", self, triggered=trigger_reset_zoom)
        pm.addAction(reset_zoom_act)

        hm = mb.addMenu("&Help")
        welcome_act = QAction("&Show Welcome", self, triggered=self._show_welcome_dialog)
        hm.addAction(welcome_act)
        readme_act = QAction("&Open User Guide", self, triggered=self._open_readme)
        hm.addAction(readme_act)
        about_act = QAction(
            f"&About {APP_NAME}", self, triggered=self._show_about_dialog
        )
        hm.addAction(about_act)
        hm.addAction("About &Qt", QApplication.instance().aboutQt)

    def _build_main_toolbar(self):
        tb = QToolBar("Main Controls")
        tb.setObjectName("MainControlsToolbar")
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setStyleSheet(PANEL_STYLESHEET)
        self.addToolBar(Qt.TopToolBarArea, tb)

        # Refresh device lists
        self.refresh_action = QAction(
            self.icon_refresh,
            "&Refresh Devices",
            self,
            triggered=self._refresh_device_lists,
        )
        tb.addAction(self.refresh_action)

        # Serial port connect/disconnect
        self.connect_serial_action = QAction(
            self.icon_connect,
            "&Connect PRIM Arduino Box",
            self,
            triggered=self._toggle_serial_connection,
        )
        tb.addAction(self.connect_serial_action)

        self.serial_port_combobox = QComboBox()
        self.serial_port_combobox.setToolTip("Select Serial Port")
        self.serial_port_combobox.setMinimumWidth(200)
        self._refresh_serial_port_list()
        tb.addWidget(self.serial_port_combobox)
        tb.addSeparator()

        camera_group = QWidget(self)
        camera_group_layout = QHBoxLayout(camera_group)
        camera_group_layout.setContentsMargins(6, 0, 4, 0)
        camera_group_layout.setSpacing(6)

        camera_label = QLabel("Camera Device")
        camera_label.setProperty("cssClass", "panelTitle")
        camera_group_layout.addWidget(camera_label)
        self.device_combo.setMinimumWidth(250)
        self.device_combo.setMaximumWidth(370)
        self.device_combo.setToolTip("Select the camera by model and serial number.")
        camera_group_layout.addWidget(self.device_combo)

        resolution_label = QLabel("Resolution")
        resolution_label.setProperty("cssClass", "detailLabel")
        camera_group_layout.addWidget(resolution_label)
        self.resolution_combo.setMinimumWidth(205)
        self.resolution_combo.setMaximumWidth(270)
        self.resolution_combo.setToolTip("Select the camera resolution")
        camera_group_layout.addWidget(self.resolution_combo)
        self.btn_start_camera.setMinimumWidth(112)
        self.btn_start_camera.setMinimumHeight(30)
        camera_group_layout.addWidget(self.btn_start_camera)
        tb.addWidget(camera_group)
        tb.addSeparator()

        self.playback_action = QAction(
            self.icon_playback,
            "Playback Last Recording",
            self,
            triggered=self.open_playback_window,
            enabled=False,
        )
        tb.addAction(self.playback_action)

    def _build_status_bar(self):
        sb = self.statusBar()
        self.serial_status_label = QLabel("Serial: Disconnected")
        self.recording_status_label = QLabel("Not Recording")
        self.app_session_time_label = QLabel("Session: 00:00:00")
        sb.addPermanentWidget(self.serial_status_label)
        sb.addPermanentWidget(self.recording_status_label)
        sb.addPermanentWidget(self.app_session_time_label)
        self._app_session_seconds = 0
        self._app_session_timer = QTimer(self)
        self._app_session_timer.setInterval(1000)
        self._app_session_timer.timeout.connect(self._update_app_session_time)
        self._app_session_timer.start()

    @pyqtSlot()
    def _update_app_session_time(self):
        """
        Increment the session timer (in seconds) and update the status‐bar label.
        """
        self._app_session_seconds += 1
        hours, rem = divmod(self._app_session_seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        self.app_session_time_label.setText(
            f"Session: {hours:02d}:{minutes:02d}:{seconds:02d}"
        )

    @pyqtSlot()
    def _clear_pressure_plot(self):
        if self.pressure_plot_widget and hasattr(
            self.pressure_plot_widget, "clear_plot"
        ):
            self.pressure_plot_widget.clear_plot()
            self.statusBar().showMessage("Pressure plot data cleared.", 3000)

    @pyqtSlot()
    def _on_zero_prim(self):
        """Send the zeroing command to the PRIM device and clear the plot."""
        if self._recording_state != "idle":
            return
        try:
            # Clear the live pressure plot regardless of connection state
            if self.pressure_plot_widget and hasattr(
                self.pressure_plot_widget, "clear_plot"
            ):
                self.pressure_plot_widget.clear_plot()

            if self._serial_thread and self._serial_thread.isRunning():
                # Send the zero command when the PRIM device is connected
                self._serial_thread.send_command(self._build_prim_command("Z"))
                msg = "Zero command sent to PRIM and plot cleared."
            else:
                msg = "PRIM device not connected; plot cleared."

            self.statusBar().showMessage(msg, 3000)
        except Exception:
            log.exception("Failed to send zero command to Arduino")

    def _set_initial_control_states(self):
        if hasattr(self, "start_recording_action"):
            self.start_recording_action.setEnabled(False)
        if hasattr(self, "stop_recording_action"):
            self.stop_recording_action.setEnabled(False)
        if hasattr(self, "camera_control_panel"):
            self.camera_control_panel.disable_camera_controls()
        if hasattr(self, "plot_control_panel"):
            self.plot_control_panel.setEnabled(True)

    # ─── Menu Actions & Dialog Slots ──────────────────────────────────────────
    def _export_plot_data_as_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Plot Data as CSV",
            config.PRIM_RESULTS_DIR,
            "CSV Files (*.csv)",
        )
        if path:
            try:
                data = self.pressure_plot_widget.get_plot_data()  # assume method exists
                with open(path, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["Time (s)", "Pressure (mmHg)"])
                    for t, p in zip(data["time"], data["pressure"]):
                        writer.writerow([t, p])
                self.statusBar().showMessage(f"Plot data exported to {path}", 3000)
            except Exception as e:
                log.error(f"Error exporting CSV: {e}")
                QMessageBox.critical(
                    self, "Export Error", f"Failed to export CSV:\n{e}"
                )

    def _choose_results_dir(self):
        new_dir = QFileDialog.getExistingDirectory(
            self, "Select Results Folder", config.PRIM_RESULTS_DIR
        )
        if new_dir:
            results_dir = os.path.join(new_dir, "PRIMAcquisition Results")
            set_results_dir(results_dir)
            save_app_setting(SETTING_RESULTS_DIR, results_dir)
            self.statusBar().showMessage(f"Results folder set to {results_dir}", 5000)

    def _show_error_dialog(self, title: str, message: str, details: str = None):
        """Display a critical error dialog with optional details."""
        dlg = QMessageBox(
            QMessageBox.Critical,
            title,
            message,
            QMessageBox.Ok,
            self,
        )
        if details:
            dlg.setDetailedText(details)
        dlg.exec_()

    def _show_about_dialog(self):
        QMessageBox.information(self, f"About {APP_NAME}", ABOUT_TEXT)

    def _show_welcome_dialog(self):
        from ui.welcome_dialog import WelcomeDialog
        dlg = WelcomeDialog(parent=self, force_show=True)
        dlg.exec_()

    def _open_readme(self):
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "README.md"))
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ─── Toggle Serial Connection ────────────────────────────────────────────
    def _toggle_serial_connection(self):
        if self._recording_state != "idle":
            return
        if self._serial_thread is not None:
            self._serial_thread.stop()
            self.connect_serial_action.setEnabled(False)
            return
        data = self.serial_port_combobox.currentData()
        port = data.value() if isinstance(data, QVariant) else data
        if not port:
            QMessageBox.warning(self, "PRIM", "Select a serial port first.")
            return
        serial = SerialThread(port=port, parent=self)
        self._serial_thread = serial
        self._serial_counter = None
        self._last_serial_activity = None
        serial.data_ready.connect(self._handle_new_serial_data)
        serial.error_occurred.connect(self._handle_serial_error)
        serial.status_changed.connect(self._handle_serial_status_change)
        serial.command_sent.connect(self._on_command_sent)
        serial.finished.connect(self._handle_serial_thread_finished)
        self.serial_port_combobox.setEnabled(False)
        self.connect_serial_action.setEnabled(False)
        serial.start()

    def _handle_serial_status_change(self, status):
        self._serial_active = status.startswith("Connected to ")
        self.top_ctrl.update_connection_status(status, self._serial_active)
        self.serial_status_label.setText("Serial: " + status)
        self.connect_serial_action.setEnabled(self._recording_state == "idle")
        if self._serial_active:
            self.connect_serial_action.setText("Disconnect PRIM Device")
            self.connect_serial_action.setIcon(self.icon_disconnect)
        elif self._recording_state in ("preparing", "recording"):
            self._handle_recorder_error("PRIM connection was lost")
        self._refresh_recording_button_states()

    def _on_command_sent(self, command):
        if command.startswith("<Z,"):
            self._serial_counter = 0
            if self._zero_before_start_pending:
                self._zero_before_start_pending = False
                if self._recording_state == "preparing":
                    self._send_prim_start()
        elif command.startswith("<G,") and self._recording_state == "preparing":
            self._set_recording_state("recording")
            self._serial_thread.set_idle_timeout_enabled(True)
        elif command.startswith("<S,") and self._recording_state == "stopping":
            self._stop_sent = True
            self._request_recorder_stop()

    def _handle_serial_error(self, message):
        self.statusBar().showMessage("PRIM: " + message, 10000)
        self._serial_active = False
        self.top_ctrl.update_connection_status("Connection error", False)
        if self._recording_state != "idle":
            self._handle_recorder_error("PRIM: " + message)
            self._request_recorder_stop()
        self._refresh_recording_button_states()

    def _handle_recorder_error(self, message):
        log.error("Recording: %s", message)
        self.statusBar().showMessage(message, 10000)
        if self._completion_summary is not None:
            # A preview-restoration failure happens after files are closed.
            # Preserve the acquisition result and report this separately.
            summary = self._completion_summary
            summary.setdefault("post_recording_issues", []).append(message)
            path = summary.get("summary_path")
            if path:
                try:
                    with open(path + ".tmp", "w", encoding="utf-8") as handle:
                        json.dump(summary, handle, indent=2)
                        handle.write("\n")
                    os.replace(path + ".tmp", path)
                except OSError as exc:
                    log.error("Could not append cleanup information to recording summary: %s", exc)
        elif self._recorder_worker is not None:
            self.recording_issue.emit(message)
        if self._recording_state in ("preparing", "recording"):
            self._on_stop_recording()

    def _handle_serial_thread_finished(self):
        serial = self._serial_thread
        self._serial_thread = None
        self._serial_active = False
        self._serial_counter = None
        self.connect_serial_action.setEnabled(True)
        self.connect_serial_action.setText("Connect PRIM Device")
        self.connect_serial_action.setIcon(self.icon_connect)
        self.serial_port_combobox.setEnabled(True)
        if serial:
            serial.deleteLater()
        if self._recording_state in ("preparing", "recording", "stopping"):
            self._handle_recorder_error("PRIM transport stopped before recording finalized")
            self._request_recorder_stop()
        self._refresh_recording_button_states()

    @pyqtSlot(int, float, float)
    def _handle_new_serial_data(self, idx: int, t: float, p: float):
        """
        Called whenever SerialThread emits data_ready(idx, t, p).
        Pushes new data into TopControlPanel and the live plot.
        """
        # 1) Update TopControlPanel (frame count, device time, pressure)
        self._serial_counter = idx
        self._last_serial_activity = time.monotonic()
        if self._recording_state == "preparing" and not self._start_sent:
            self._handle_recorder_error("PRIM was already producing data before the recording start")
        self.top_ctrl.update_prim_data(idx, t, p)

        # 2) Read the auto-scale checkboxes from PlotControlPanel
        ax = self.plot_control_panel.auto_x_cb.isChecked()
        ay = self.plot_control_panel.auto_y_cb.isChecked()

        # Keep the completed run available for review, including while idle
        # serial status changes. Raw device timestamps still go to the writer.
        if (self._start_sent and self._completion_summary is None
                and self._recording_state in ("preparing", "recording", "stopping", "finalizing")):
            self.pressure_plot_widget.update_plot(t, p, ax, ay)

        # 4) Also log it to the console dock if visible
        if self.dock_console.isVisible():
            self.console_out_textedit.append(
                f"PRIM Data: Idx={idx}, Time={t:.3f}s, P={p:.2f}"
            )

    # ──────────────────────────────────────────────────────────────
    # Recording Management
    # ──────────────────────────────────────────────────────────────

    def _current_recording_settings(self):
        """Return the recording settings currently selected in the UI."""
        fps = DEFAULT_FPS
        if self.camera_control_panel is not None:
            selected_fps = self.camera_control_panel.sampling_spin.value()
            if selected_fps > 0:
                fps = selected_fps

        capture_code = DEFAULT_CAPTURE_SETTING_CODE
        if (
            self.camera_control_panel is not None
            and hasattr(self.camera_control_panel, "get_capture_setting")
        ):
            capture_code, _ = self.camera_control_panel.get_capture_setting()

        return build_recording_settings(fps, capture_code)

    def _build_prim_command(self, command_char, settings=None):
        """Build the Arduino command packet required by the PRIM firmware."""
        settings = settings or self._current_recording_settings()
        return format_prim_command(
            command_char,
            settings.frame_interval_ms,
            settings.capture_setting_code,
        )

    def _on_start_recording(self):
        if self._recording_state != "idle" or not self._serial_active:
            return
        settings = self._current_recording_settings()
        if settings.record_video and not self._camera_ready:
            QMessageBox.warning(self, "Recording", "Start the camera or choose Images: None for CSV-only recording.")
            return
        if self._last_serial_activity is not None and time.monotonic() - self._last_serial_activity < max(0.5, 2 * settings.frame_interval_ms / 1000):
            QMessageBox.warning(self, "PRIM is running", "Stop the manual PRIM run before starting a recording.")
            return
        try:
            output_dir = get_next_fill_folder()
        except Exception as exc:
            self._show_error_dialog("Recording preparation", str(exc))
            return
        self._current_fill_folder = output_dir
        self._active_recording_settings = settings
        self._run_id = uuid.uuid4().hex
        self._start_sent = self._stop_sent = self._camera_armed = False
        self._zero_before_start_pending = False
        self._completion_summary = None
        self._last_recording_paths = {"csv": None, "tiff": None}
        self.pressure_plot_widget.clear_plot()
        self._set_recording_state("preparing")
        worker = RecordingManager(output_dir, settings.recording_fps, settings.frame_interval_ms,
            settings.capture_setting_code, settings.capture_setting_label, settings.record_video,
            run_id=self._run_id, initial_counter=0)
        thread = QThread(self)
        self._recorder_worker, self._recorder_thread = worker, thread
        worker.moveToThread(thread)
        thread.started.connect(worker.start_recording)
        worker.ready_for_acquisition.connect(self._on_recorder_ready)
        worker.activated.connect(self._on_recorder_activated)
        worker.error_occurred.connect(self._handle_recorder_error)
        worker.progress.connect(self._on_recording_progress)
        worker.finalized.connect(self._on_recording_finalized)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_recorder_thread_finished)
        self.activate_recorder.connect(worker.activate)
        self.recording_issue.connect(worker.mark_incomplete)
        self._serial_thread.data_ready.connect(worker.append_pressure)
        if settings.record_video:
            self.camera_thread.recording_frame_ready.connect(worker.enqueue_frame, Qt.DirectConnection)
        thread.start()
        run_id = self._run_id
        QTimer.singleShot(10000, lambda: self._preparation_deadline(run_id))

    def _toggle_recording(self):
        if self._recording_state == "idle":
            self._on_start_recording()
        elif self._recording_state in ("preparing", "recording"):
            self._on_stop_recording()

    def _preparation_deadline(self, run_id):
        if run_id == self._run_id and self._recording_state == "preparing":
            self._handle_recorder_error("Recording preparation did not complete within 10 seconds")

    def _set_recording_state(self, state):
        self._recording_state = state
        self.camera_control_panel.set_recording_state(state != "idle")
        self.recording_status_label.setText({"idle": "Not Recording", "preparing": "Preparing…",
            "recording": "Recording → " + os.path.basename(self._current_fill_folder or ""),
            "stopping": "Stopping PRIM…", "finalizing": "Finalizing files…"}[state])
        self._refresh_recording_button_states()

    def _on_recorder_ready(self):
        if self._recording_state != "preparing":
            self._request_recorder_stop()
            return
        settings = self._active_recording_settings
        if settings.record_video:
            self._camera_ready = False
            self.camera_thread.request_recording(self._run_id, settings.frame_interval_ms * settings.capture_setting_code / 1000.0)
        else:
            self._start_acquisition({"timing_mode": "csv_only"})

    def _start_acquisition(self, details):
        if self._recording_state != "preparing":
            return
        if not self._serial_active or (self._active_recording_settings.record_video and not self._camera_armed):
            self._handle_recorder_error("Camera or PRIM is no longer ready")
            return
        self.activate_recorder.emit(details)

    def _on_recorder_activated(self):
        if self._recording_state != "preparing":
            return
        # Outputs and camera are ready. Zero the public counter/clock before G,
        # using command_sent to preserve ordering (it is not a device ACK).
        self._zero_before_start_pending = True
        if not self._serial_thread.send_command(self._build_prim_command("Z", self._active_recording_settings)):
            self._zero_before_start_pending = False
            self._handle_recorder_error("Could not queue PRIM zero command")
            self._request_recorder_stop()

    def _send_prim_start(self):
        self._start_sent = True
        if not self._serial_thread.send_command(self._build_prim_command("G", self._active_recording_settings)):
            self._handle_recorder_error("Could not queue PRIM start command")
            self._request_recorder_stop()

    def _on_stop_recording(self):
        if self._recording_state not in ("preparing", "recording"):
            return
        self._set_recording_state("stopping")
        if self._serial_thread:
            self._serial_thread.set_idle_timeout_enabled(False)
        if self._start_sent and self._serial_thread:
            if self._serial_thread.send_command(self._build_prim_command("S", self._active_recording_settings)):
                run_id = self._run_id
                QTimer.singleShot(1500, lambda: self._stop_deadline(run_id))
                return
            self.recording_issue.emit("PRIM stop could not be sent; use the device's manual stop")
        self._request_recorder_stop()

    def _stop_deadline(self, run_id):
        if run_id == self._run_id and self._recording_state == "stopping" and not self._stop_sent:
            self.recording_issue.emit("PRIM stop write was not confirmed; use the device's manual stop")
            self._request_recorder_stop()

    def _request_recorder_stop(self):
        if self._recorder_worker is not None and self._recording_state != "idle":
            self._set_recording_state("finalizing")
            try:
                QMetaObject.invokeMethod(self._recorder_worker, "request_stop", Qt.QueuedConnection)
            except RuntimeError:
                pass  # The finalized signal may already be queued for this worker.

    def _on_recording_progress(self, samples, images):
        self.top_ctrl.images_lbl.setText(f"{images:,}")

    def _on_recording_finalized(self, summary):
        self._completion_summary = summary
        self._last_recording_paths = {"csv": summary["csv_path"], "tiff": summary["tiff_path"] if summary["complete"] else None}
        self.top_ctrl.images_lbl.setText(f"{summary['frames_written']:,}")
        self._set_recording_state("finalizing")
        if self.camera_thread and self.camera_thread.isRunning() and self._active_recording_settings.record_video:
            self._preview_restoring = True
            self.camera_thread.request_preview()

    def _on_recorder_thread_finished(self):
        thread = self._recorder_thread
        self._recorder_worker = self._recorder_thread = None
        if thread:
            thread.deleteLater()
        self._finish_recording_ui()

    def _finish_recording_ui(self):
        if self._completion_summary is None or self._recorder_thread is not None or self._preview_restoring:
            return
        summary, self._completion_summary = self._completion_summary, None
        self._active_recording_settings = None
        self._run_id = None
        self._set_recording_state("idle")
        dialog = RecordingCompletionDialog(summary, self)
        self._completion_dialog = dialog
        dialog.playback_requested.connect(self.open_playback_window)
        dialog.finished.connect(self._on_completion_closed)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.open()

    def _on_completion_closed(self, result):
        self._completion_dialog = None
        if self._closing:
            self.close()

    def _refresh_recording_button_states(self):
        if not hasattr(self, "start_recording_action"):
            return
        settings = self._current_recording_settings()
        idle = self._recording_state == "idle"
        can_start = idle and self._serial_active and (not settings.record_video or self._camera_ready) and not self._closing
        can_stop = self._recording_state in ("preparing", "recording")
        self.start_recording_action.setEnabled(can_start)
        self.stop_recording_action.setEnabled(can_stop)
        self.top_ctrl.set_recording_state(self._recording_state, can_start or can_stop)
        self.btn_start_camera.setEnabled(idle)
        if hasattr(self, "connect_serial_action"):
            self.connect_serial_action.setEnabled(idle)
            self.refresh_action.setEnabled(idle and self.camera_thread is None and self._serial_thread is None)
            self.playback_action.setEnabled(idle and bool(self._last_recording_paths.get("tiff")))
        port = self._serial_thread.port if self._serial_thread else "Disconnected"
        self.top_ctrl.set_acquisition_details(f"{port} · 115200 baud · Pressure {1000 / settings.frame_interval_ms:g} Hz "
            f"({settings.frame_interval_ms} ms) · Images: {settings.capture_setting_label} · "
            + ("Arduino external trigger" if settings.record_video else "CSV only"))

    # ─── Window Close Cleanup ──────────────────────────────────────────────────
    def closeEvent(self, event):
        self._closing = True
        if self._completion_dialog is not None:
            event.ignore()
            return
        if self._recording_state != "idle":
            event.ignore()
            self._on_stop_recording()
            return
        active = False
        for thread in (self.camera_thread, self._serial_thread, self._recorder_thread):
            if thread is not None and thread.isRunning():
                active = True
                thread.stop() if hasattr(thread, "stop") else thread.quit()
        if active:
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        super().closeEvent(event)


    def open_playback_window(self, ask_user=False):
        """Open a :class:`PlaybackWindow` with the last recording or ask for files."""
        tiff_path = self._last_recording_paths.get("tiff")
        csv_path = self._last_recording_paths.get("csv")
        if ask_user:
            tiff_path = csv_path = None
        elif tiff_path and csv_path and (
            not os.path.exists(tiff_path) or not os.path.exists(csv_path)
        ):
            tiff_path = csv_path = None

        self.playback_window = PlaybackWindow(tiff_path, csv_path, parent=self)
        self.playback_window.showMaximized()
