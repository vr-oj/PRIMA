"""Reject known competing PRIMA applications before opening the camera."""
import csv
import io
import os
import subprocess
import sys


def require_no_other_prima():
    if sys.platform != "win32":
        return
    tasklist = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tasklist.exe")
    result = subprocess.run([tasklist, "/FI", "IMAGENAME eq PRIMA.exe", "/FO", "CSV", "/NH"],
                            capture_output=True, text=True, check=True, timeout=5,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    # A PyInstaller one-file application has its own PRIMA bootstrap parent.
    own = {os.getpid(), os.getppid()}
    others = [int(row[1]) for row in csv.reader(io.StringIO(result.stdout))
              if len(row) > 1 and row[0].lower() == "prima.exe" and int(row[1]) not in own]
    if others:
        raise RuntimeError("Close the other PRIMA application before connecting the camera. "
                           "Only one application should control acquisition (processes "
                           + ", ".join(map(str, others)) + ").")
