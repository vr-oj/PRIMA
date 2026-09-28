# pressure_plot_widget.py
import os
import time
import bisect
import logging
import math

from PyQt5.QtWidgets import (
    QWidget,
    QSizePolicy,
    QVBoxLayout,
    QMessageBox,
    QFileDialog,
    QScrollBar,
)
from PyQt5.QtCore import Qt, pyqtSlot
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
import matplotlib as mpl

from utils.config import PLOT_DEFAULT_Y_MIN, PLOT_DEFAULT_Y_MAX

log = logging.getLogger(__name__)


class PressurePlotWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        mpl.rcParams.update(
            {
                "font.size": 11,
                "axes.edgecolor": "#333333",
                "axes.labelweight": "bold",
                "axes.labelsize": 12,
                "axes.linewidth": 1.5,
                "xtick.color": "#333333",
                "ytick.color": "#333333",
                "figure.facecolor": "#ffffff",
                "savefig.facecolor": "#ffffff",
            }
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Matplotlib canvas
        self.fig = Figure(facecolor="white", tight_layout=True)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("white")
        self.ax.set_xlabel("Time from recording start (s)", fontsize=16, fontweight="bold")
        self.ax.set_ylabel("Pressure (mmHg)", fontsize=16, fontweight="bold")
        self.ax.tick_params(labelsize=10)
        for spine in self.ax.spines.values():
            spine.set_color("#D8DEE9")
        # Add a faint grid to the plot
        self.ax.grid(True, linestyle="--", alpha=0.7, color="lightgray")

        (self.line,) = self.ax.plot([], [], "-", lw=2, color="black")

        self.canvas = FigureCanvas(self.fig)
        layout.addWidget(self.canvas)

        # Scrollbar for manual X panning
        self.scrollbar = QScrollBar(Qt.Horizontal, self)
        self.scrollbar.hide()
        layout.addWidget(self.scrollbar)
        self.scrollbar.valueChanged.connect(self._on_scroll)

        # MODIFIED: Apply a simple stylesheet to the scrollbar for better visibility
        self.scrollbar.setStyleSheet(
            """
            QScrollBar:horizontal {
                border: 1px solid #C0C0C0; /* Light border for the scrollbar itself */
                background: #F0F0F0;    /* Background of the scrollbar groove */
                height: 15px;           /* Height of the scrollbar */
                margin: 0px 20px 0 20px;/* Margins to make space for add/sub-line buttons if they were visible */
            }
            QScrollBar::handle:horizontal {
                background: #A0A0A0;    /* A medium gray for the handle */
                min-width: 20px;        /* Minimum width of the handle */
                border-radius: 5px;     /* Rounded corners for the handle */
                border: 1px solid #808080; /* Border for the handle */
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                /* Style for arrow buttons if you want them, currently not explicitly shown */
                /* border: 1px solid grey; background: #E0E0E0; width: 18px; */
                width: 0px; /* Hide standard arrow buttons by making them zero width */
                height: 0px;
                background: none;
                border: none;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: none; /* Background of the area where you click to page scroll */
            }
        """
        )

        # Data storage
        self.times = []
        self.pressures = []
        self.manual_xlim = None
        self.manual_ylim = (PLOT_DEFAULT_Y_MIN, PLOT_DEFAULT_Y_MAX)
        self.ax.set_ylim(self.manual_ylim)
        self.window_duration = 100  # Duration of the visible window in seconds

        # Placeholder text
        self.placeholder = self.ax.text(
            0.5,
            0.5,
            "Waiting for PRIM device data...",
            transform=self.ax.transAxes,
            ha="center",
            va="center",
            fontsize=12,
            color="gray",
            bbox=dict(boxstyle="round,pad=0.5", fc="#ECEFF4", alpha=0.8),
        )

        # Hover Label
        self.hover_annotation = self.ax.annotate(
            "",
            xy=(0, 0),
            xytext=(15, 15),
            textcoords="offset points",
            bbox=dict(boxstyle="round,pad=0.4", fc="wheat", alpha=0.85),
            arrowprops=dict(
                arrowstyle="->", connectionstyle="arc3,rad=.2", color="black"
            ),
        )
        self.hover_annotation.set_visible(False)

        # Connect hover event
        self.canvas.mpl_connect("motion_notify_event", self._on_hover)

    def _update_placeholder(self, text=None):
        if text:
            self.line.set_data([], [])  #
            if self.hover_annotation.get_visible():  # Hide hover if placeholder appears
                self.hover_annotation.set_visible(False)
            if self.placeholder:
                self.placeholder.set_text(text)
                self.placeholder.set_visible(True)
            else:
                self.placeholder = self.ax.text(
                    0.5,
                    0.5,
                    text,
                    transform=self.ax.transAxes,
                    ha="center",
                    va="center",
                    fontsize=12,
                    color="gray",
                    bbox=dict(boxstyle="round,pad=0.5", fc="#ECEFF4", alpha=0.8),
                )
        else:
            if self.placeholder:
                self.placeholder.set_visible(
                    False
                )  # Just hide, no need to remove and recreate
                # self.placeholder.remove() # Removing and recreating can be less efficient
                # self.placeholder = None
        self.canvas.draw_idle()

    def _find_nearest_datapoint(self, x_coord):
        """Finds the nearest data point (time, pressure, index) to the given x_coord."""
        if not self.times:
            return None, None, -1

        # bisect_left finds the insertion point for x_coord to maintain sorted order
        idx = bisect.bisect_left(self.times, x_coord)

        if idx == 0:  # x_coord is at or before the first element
            best_idx = 0
        elif idx == len(self.times):  # x_coord is after the last element
            best_idx = len(self.times) - 1
        else:  # x_coord is between self.times[idx-1] and self.times[idx]
            # Determine which of the two neighbours is closer
            dist1 = abs(x_coord - self.times[idx - 1])
            dist2 = abs(x_coord - self.times[idx])
            if dist1 <= dist2:
                best_idx = idx - 1
            else:
                best_idx = idx

        # Optional: Add a threshold if you only want to show hover for very close points
        # For example, if the closest point's x value is too far from mouse x_coord:
        # x_axis_range = self.ax.get_xlim()[1] - self.ax.get_xlim()[0]
        # if x_axis_range > 0 and abs(self.times[best_idx] - x_coord) > x_axis_range * 0.05: # 5% of current x-axis view
        #     return None, None, -1

        return self.times[best_idx], self.pressures[best_idx], best_idx

    def _on_hover(self, event):
        """Handles mouse motion event to show data point information."""
        # If no data, or placeholder is visible, or line is not visible, do nothing with hover
        if (
            not self.times
            or (self.placeholder and self.placeholder.get_visible())
            or not self.line.get_visible()
        ):
            if self.hover_annotation.get_visible():
                self.hover_annotation.set_visible(False)
                self.canvas.draw_idle()
            return

        annotation_visible = self.hover_annotation.get_visible()
        needs_redraw = False

        if event.inaxes == self.ax:  # Check if mouse is over the plot axes
            x_mouse, y_mouse = (
                event.xdata,
                event.ydata,
            )  # Mouse coordinates in data space

            # Find the data point on the line closest to the mouse's x-coordinate
            target_x, target_y, _ = self._find_nearest_datapoint(x_mouse)

            if target_x is not None:
                self.hover_annotation.xy = (target_x, target_y)
                new_text = f"Time: {target_x:.2f} s\nPressure: {target_y:.2f} mmHg"

                if self.hover_annotation.get_text() != new_text:
                    self.hover_annotation.set_text(new_text)
                    needs_redraw = True

                if not annotation_visible:
                    self.hover_annotation.set_visible(True)
                    needs_redraw = True
            else:  # No suitable data point found
                if annotation_visible:
                    self.hover_annotation.set_visible(False)
                    needs_redraw = True
        else:  # Mouse is not over the axes
            if annotation_visible:
                self.hover_annotation.set_visible(False)
                needs_redraw = True

        if needs_redraw:
            self.canvas.draw_idle()

    @pyqtSlot(float, float, bool, bool)
    def update_plot(self, t, p, auto_x, auto_y):
        # Remove placeholder on first data
        if not self.times and self.placeholder and self.placeholder.get_visible():
            self._update_placeholder(None)

        # Append new data
        self.times.append(t)
        self.pressures.append(p)
        self.line.set_data(self.times, self.pressures)
        self._update_axes(auto_x, auto_y)

    def _update_axes(self, auto_x, auto_y):
        # Remember the current axis limits so we only redraw if something changes:
        current_xlim = self.ax.get_xlim()
        current_ylim = self.ax.get_ylim()

        # ─── X-axis handling ───────────────────────────────
        if auto_x:
            # Clear any manual X-limits and let matplotlib autoscale
            self.manual_xlim = None
            self.scrollbar.hide()
            if len(self.times) > 1:
                start, end = self.times[0], self.times[-1]
                pad = max(1, (end - start) * 0.05)
                self.ax.set_xlim(max(0, start - pad * 0.1), end + pad * 0.9)
            elif self.times:  # single data point
                t0 = self.times[-1]
                self.ax.set_xlim(max(0, t0 - 0.5), t0 + 0.5)
        else:
            # SLIDING WINDOW: always show [t_latest - window_duration, t_latest]
            t_latest = self.times[-1]
            xmin = max(0.0, t_latest - self.window_duration)
            xmax = max(t_latest, xmin + 0.5)
            self.manual_xlim = (xmin, xmax)
            self.ax.set_xlim(self.manual_xlim)

            # Hide scrollbar since we're “following” the newest point
            self.scrollbar.hide()

        # ─── Y-axis handling ───────────────────────────────
        if auto_y:
            self.manual_ylim = None  # clear any manual Y-limits
            if self.pressures:
                mn, mx = min(self.pressures), max(self.pressures)
                pad = max(abs(mx - mn) * 0.1, 2.0)
                self.ax.set_ylim(mn - pad, mx + pad)
        else:
            if self.manual_ylim:
                # Use whatever manual Y-limits have been set previously
                self.ax.set_ylim(self.manual_ylim)
            elif not self.pressures:
                # No data yet → fallback to default
                self.ax.set_ylim(PLOT_DEFAULT_Y_MIN, PLOT_DEFAULT_Y_MAX)

        # ─── Only redraw if limits or data changed ───────
        new_xlim = self.ax.get_xlim()
        new_ylim = self.ax.get_ylim()

        if current_xlim != new_xlim or current_ylim != new_ylim or self.line.stale:
            self.canvas.draw_idle()

    def _update_scrollbar(self):
        if not self.times or not self.manual_xlim:  # Ensure data and manual_xlim exist
            self.scrollbar.hide()
            return

        # Determine index-based window for manual_xlim
        xmin, xmax = self.manual_xlim
        idx0 = bisect.bisect_left(self.times, xmin)
        idx1 = bisect.bisect_right(self.times, xmax)
        window_size = max(idx1 - idx0, 1)  # Ensure window_size is at least 1
        full_len = len(self.times)

        if full_len <= window_size:  # If the window covers all data
            self.scrollbar.hide()
            return

        # Configure scrollbar
        self.scrollbar.setMinimum(0)
        self.scrollbar.setMaximum(
            max(full_len - window_size, 0)
        )  # Ensure maximum is not negative
        self.scrollbar.setPageStep(window_size)
        self.scrollbar.setSingleStep(
            max(window_size // 10, 1)
        )  # Ensure singleStep is at least 1

        # Position scrollbar thumb
        self.scrollbar.setValue(idx0)  # Set value after setting min/max/pageStep
        self.scrollbar.show()

    @pyqtSlot(int)
    def _on_scroll(self, pos):
        # Pan X-axis window based on scroll position
        if not self.manual_xlim or not self.times or len(self.times) <= 1:
            return

        # Determine window size from current manual_xlim to maintain zoom level
        current_xmin, current_xmax = self.manual_xlim
        # Find indices for the current xlim to estimate window width in data units
        current_idx_min = bisect.bisect_left(self.times, current_xmin)
        current_idx_max = bisect.bisect_right(self.times, current_xmax)

        # Calculate window width in terms of number of data points
        # This uses pageStep as an approximation of window size in indices
        window_indices = self.scrollbar.pageStep()

        # New start index from scrollbar position
        start_idx = pos
        end_idx = min(
            start_idx + window_indices - 1, len(self.times) - 1
        )  # Ensure end_idx is valid

        if start_idx >= end_idx and len(self.times) > 1:  # Ensure valid range
            # This can happen if window_indices is too small or pos is at the very end.
            # Default to a small window at the end.
            start_idx = max(0, len(self.times) - 2)
            end_idx = len(self.times) - 1

        if start_idx < 0:
            start_idx = 0  # Should not happen with QScrollBar limits

        xmin_new = self.times[start_idx]
        xmax_new = self.times[end_idx]

        # Ensure xmax_new is greater than xmin_new, especially for small datasets
        if xmin_new == xmax_new and len(self.times) > 1:
            if end_idx + 1 < len(self.times):
                xmax_new = self.times[end_idx + 1]
            elif start_idx - 1 >= 0:
                xmin_new = self.times[start_idx - 1]
            else:  # Single point, or all points identical; give a small default range
                xmax_new = xmin_new + 1.0

        self.manual_xlim = (xmin_new, xmax_new)
        self.ax.set_xlim(self.manual_xlim)
        self.canvas.draw_idle()

    def set_manual_x_limits(self, xmin, xmax):
        if xmin < xmax:
            self.manual_xlim = (xmin, xmax)
            self.ax.set_xlim(self.manual_xlim)
            self._update_scrollbar()  # Update scrollbar based on new manual limits
            self.canvas.draw_idle()  # Redraw
        else:
            log.warning("X min must be less than X max")

    def set_manual_y_limits(self, ymin, ymax):
        if (
            ymin < ymax and math.isfinite(ymin) and math.isfinite(ymax)
        ):  # Ensure finite values
            self.manual_ylim = (ymin, ymax)
            self.ax.set_ylim(self.manual_ylim)
            self.canvas.draw_idle()
        else:
            log.warning(
                f"Y limits must be finite and min < max. Received: {ymin}, {ymax}"
            )

    def reset_zoom(self, auto_x, auto_y):
        self.manual_xlim = None  # Reset manual x-limits
        if auto_x:
            self.scrollbar.hide()

        if not auto_y:  # If auto_y is false, reset to default Y manual limits
            self.manual_ylim = (PLOT_DEFAULT_Y_MIN, PLOT_DEFAULT_Y_MAX)
            self.ax.set_ylim(self.manual_ylim)
        else:  # If auto_y is true, clear manual y-limits for full auto-scaling
            self.manual_ylim = None

        # Re-evaluate plot based on current data and new auto settings
        if self.times:
            # Recompute the view without appending a duplicate sample.
            self._update_axes(auto_x, auto_y)
        else:  # No data, set to default view
            self.ax.set_xlim(0, 10)  # Default X if no data
            if self.manual_ylim and not auto_y:  # Apply manual Y if set and not auto_y
                self.ax.set_ylim(self.manual_ylim)
            elif not auto_y:  # Default Y if not auto_y and no manual_ylim
                self.ax.set_ylim(PLOT_DEFAULT_Y_MIN, PLOT_DEFAULT_Y_MAX)
            # If auto_y is true and no data, it will use the default Y from ax.plot if any, or we might need to set one
            # For now, let it use existing or default limits if auto_y is true.
            self._update_placeholder("Plot cleared or waiting for data.")
            self.canvas.draw_idle()

    def clear_plot(self):
        self.times.clear()
        self.pressures.clear()
        self.manual_xlim = None
        self.scrollbar.hide()
        self.scrollbar.setRange(0, 0)

        self.line.set_data([], [])
        self.ax.set_xlim(0, 100)  # Reset to a default X view

        if self.manual_ylim is None:  # Y-axis is meant to be auto-scaled
            # On clear with auto-y, set to default. update_plot will auto-scale if data comes.
            self.ax.set_ylim(PLOT_DEFAULT_Y_MIN, PLOT_DEFAULT_Y_MAX)
        elif (
            isinstance(self.manual_ylim, tuple)
            and len(self.manual_ylim) == 2
            and all(v is not None and math.isfinite(v) for v in self.manual_ylim)
        ):  # Check for valid finite tuple
            self.ax.set_ylim(self.manual_ylim)  # Apply valid stored manual limits
        else:
            # Fallback: manual_ylim is invalid (should not happen now) or some other state. Reset to default.
            self.manual_ylim = (PLOT_DEFAULT_Y_MIN, PLOT_DEFAULT_Y_MAX)
            self.ax.set_ylim(self.manual_ylim)

        if self.hover_annotation.get_visible():
            self.hover_annotation.set_visible(False)

        self._update_placeholder("Plot data cleared.")  # This will also call draw_idle
        # self.canvas.draw_idle() # Called by _update_placeholder

    def export_as_image(self):
        if not self.times and not (
            self.placeholder and self.placeholder.get_visible()
        ):  #
            QMessageBox.warning(self, "Empty Plot", "Plot has no data to export.")
            return
        default_name = f"plot_export_{time.strftime('%Y%m%d-%H%M%S')}.png"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Plot Image",
            default_name,
            "PNG (*.png);;JPEG (*.jpg);;SVG (*.svg);;PDF (*.pdf)",
        )
        if not path:
            return
        try:
            # Temporarily hide hover annotation and placeholder for export
            hover_visible = self.hover_annotation.get_visible()
            placeholder_text_visible = (
                self.placeholder and self.placeholder.get_visible()
            )

            if hover_visible:
                self.hover_annotation.set_visible(False)
            if placeholder_text_visible:
                self.placeholder.set_visible(False)

            # Redraw canvas without annotations before saving
            self.canvas.draw()

            self.fig.savefig(path, dpi=300, facecolor=self.fig.get_facecolor())

            # Restore visibility
            if hover_visible:
                self.hover_annotation.set_visible(True)
            if placeholder_text_visible:
                self.placeholder.set_visible(True)

            # Redraw canvas with annotations again
            self.canvas.draw_idle()

            sb = self.window().statusBar() if self.window() else None
            if sb:
                sb.showMessage(f"Plot exported to {os.path.basename(path)}", 3000)
        except Exception as e:  # Use 'e' for the exception instance
            log.exception(f"Error exporting plot image: {e}")
            QMessageBox.critical(
                self, "Export Error", f"Could not save plot image: {e}"
            )
