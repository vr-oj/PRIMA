"""PRIM serial transport. G/S/Z packets remain unchanged; writes are acknowledged."""
import logging
import math
import queue
import time
import serial
from PyQt5.QtCore import QThread, pyqtSignal

log = logging.getLogger(__name__)


class SerialThread(QThread):
    data_ready = pyqtSignal(int, float, float)
    error_occurred = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    command_sent = pyqtSignal(str)

    def __init__(self, port=None, baud=115200, test_csv=None, parent=None):
        super().__init__(parent)
        self.port, self.baud = port, baud
        self.ser = None
        self.running = False
        self._stop_requested = False
        self._idle_timeout_enabled = False
        self._last_data_time = None
        self._idle_timeout_s = 3.0
        self._active_stop_packet = None
        self.command_queue = queue.Queue()

    def set_idle_timeout_enabled(self, enabled):
        self._idle_timeout_enabled = bool(enabled)
        self._last_data_time = time.monotonic() if enabled else None

    def _write(self, packet):
        count = self.ser.write(packet)
        if count != len(packet):
            raise IOError(f"Partial serial write ({count}/{len(packet)} bytes)")

    def _send(self, packet):
        if packet.startswith(b"<G,"):
            # Also attempt S if a G write fails partway through.
            self._active_stop_packet = b"<S," + packet[3:]
            try:
                self._idle_timeout_s = max(3.0, 3 * int(packet.split(b",")[1]) / 1000)
            except ValueError:
                pass
        self._write(packet)
        if packet.startswith((b"<S,", b"<Z,")):
            self._active_stop_packet = None
        self.command_sent.emit(packet.decode("ascii").strip())

    def run(self):
        pending = bytearray()
        try:
            if not self.port:
                raise RuntimeError("No serial port selected")
            self.ser = serial.Serial(self.port, self.baud, timeout=0.05, write_timeout=0.5)
            self.running = True
            self.status_changed.emit(f"Connected to {self.port}")
            while not self._stop_requested:
                try:
                    packet = self.command_queue.get_nowait()
                except queue.Empty:
                    packet = None
                if packet:
                    self._send(packet)
                data = self.ser.read(max(1, min(self.ser.in_waiting, 4096)))
                pending.extend(data)
                if len(pending) > 65536:
                    raise RuntimeError("PRIM serial input exceeded the line-buffer limit")
                while b"\n" in pending:
                    line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    if not line.strip():
                        continue
                    try:
                        fields = line.decode("ascii").strip().split(",")
                        if len(fields) != 3:
                            raise ValueError("expected three fields")
                        counter, device_time, pressure = int(fields[0]), float(fields[1]), float(fields[2])
                        if not all(math.isfinite(v) for v in (device_time, pressure)):
                            raise ValueError("non-finite values")
                    except (ValueError, UnicodeError) as exc:
                        raise RuntimeError(f"Invalid PRIM data {bytes(line)!r}: {exc}") from exc
                    self._last_data_time = time.monotonic()
                    self.data_ready.emit(counter, device_time, pressure)
                if (self._idle_timeout_enabled and self._last_data_time is not None
                        and time.monotonic() - self._last_data_time > self._idle_timeout_s):
                    raise RuntimeError("PRIM stopped sending data during acquisition")
        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            # Never replay a queued G after a disconnect. Best-effort S applies
            # only to acquisition this transport started, not manual PRIM runs.
            if self.ser is not None:
                if self._active_stop_packet:
                    try:
                        self._write(self._active_stop_packet)
                    except Exception as exc:
                        self.error_occurred.emit("PRIM stop could not be confirmed: " + str(exc))
                try:
                    self.ser.close()
                except Exception as exc:
                    self.error_occurred.emit("Serial close failed: " + str(exc))
            self.ser = None
            self.running = False
            self.status_changed.emit("Disconnected")

    def send_command(self, command_str):
        if not self.running or self._stop_requested:
            return False
        self.command_queue.put(command_str.encode("ascii") + b"\n")
        return True

    def stop(self):
        self._stop_requested = True
