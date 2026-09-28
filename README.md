# PRIMA

PRIMA 3.5.0 records pressure data from the Arduino-controlled PRIM
device and camera imagery through native IC4 or Micro-Manager connections. Recordings
are saved as CSV pressure logs and, when enabled, TIFF video stacks for playback
and analysis.

## Quick Start

1. Install the IC Imaging Control / camera driver package from The Imaging
   Source.
2. Install PRIMA using the Windows installer from the latest release.
3. Connect the PRIM device over USB and select its COM port.
4. Select the camera and resolution, then click **Start Camera**.
5. In the Recording panel, select **Pressure rate** and **Images**.
6. Click **Start Recording**.
7. Click **Stop Recording** when finished.
8. Use **Playback Last Recording** to review the TIFF/CSV output.

For a non-IC4 camera, choose **Micro-Manager Camera Setup…** at the end of the
Camera dropdown. Install compatible Micro-Manager adapters and the camera's
vendor drivers, then find the camera or load a saved configuration. See the
[camera setup guide](prim_app/docs/cameras.md). Supported camera/adapter pairs can
preview and record with Arduino electrical triggers; unsupported trigger
interfaces cannot start a video recording. There is no automatic software-sync
fallback. IC4 and Micro-Manager are optional at application startup.

Starting a recording clears the previous live plot. After the files and camera
are ready, PRIMA sends **Zero (`Z`) → Start (`G`)** so each recording begins with
a fresh Arduino clock and trigger counter. Zero does not change pressure
calibration. The plot uses the original device timestamps, just like the saved
CSV/TIFF data; there is no display-only time offset. The completed plot remains
available for inspection/export until the next recording or an explicit clear.

After recording with **Images: None**, restart the Arduino box and match its
FPS/Capture configuration to the next recording request. Selecting None displays
this reminder beneath Images. PRIM firmware remains unchanged; Zero resets the
public clock and trigger count and does not replace the required restart.

Changing **Preview rate** briefly pauses camera acquisition, applies the rate
through IC4, and resumes preview. Recording becomes available after a new image
arrives. A rejected rate restores the previous preview configuration. Preview
rate controls remain locked during recording, when Arduino pulses govern capture.

## Recording Controls

Set the pressure sampling rate and image capture in PRIMA before recording.
The **Pressure rate** and **Images** values are sent to the Arduino whenever
PRIMA sends start, stop, or zero commands. Both controls are locked during a run.

The command format is:

```text
<command, frame_delay_ms, capture_setting>
```

Supported commands:

| Command | Meaning |
| ------- | ------- |
| `G` | Start PRIM |
| `S` | Stop PRIM |
| `Z` | Zero PRIM |

The frame delay is calculated from the selected app frame rate:

```text
frame_delay_ms = ceil(1000 / fps)
```

Examples:

| App setting | Command value |
| ----------- | ------------- |
| 10 fps | 100 ms |
| 9 fps | 112 ms |
| 1 fps | 1000 ms |

Capture settings use the actual values expected by the Arduino:

| App label | Arduino value | Recording behavior |
| --------- | ------------- | ------------------ |
| None | 0 | CSV-only recording; no TIFF video |
| Every | 1 | Save every TIFF frame |
| 1 in 5 | 5 | Save every 5th TIFF frame |
| 1 in 10 | 10 | Save every 10th TIFF frame |
| 1 in 15 | 15 | Save every 15th TIFF frame |
| 1 in 20 | 20 | Save every 20th TIFF frame |
| 1 in 25 | 25 | Save every 25th TIFF frame |
| 1 in 30 | 30 | Save every 30th TIFF frame |

For example, with 10 fps and Capture set to **1 in 10**, PRIMA sends:

```text
<G, 100, 10>
```

PRIMA records every serial pressure row in the CSV. When Capture is not
**None**, the TIFF stack only includes the selected video frames.

## Output Files

Recordings are saved into date/fill folders:

```text
PRIMAcquisition Results/
└── YYYY-MM-DD/
    └── FillN/
        ├── recording_YYYY-MM-DD_HH-MM-SS_pressure.csv
        └── recording_YYYY-MM-DD_HH-MM-SS_video.tif
```

The default results folder is `Documents/PRIMAcquisition Results`. Set the
`PRIM_RESULTS_DIR` environment variable before launching the app to use another
location.

CSV files include recording metadata before the data table:

```csv
# recording_fps,10.0
# frame_interval_ms,100
# capture_setting,Every
# capture_setting_code,1
frameIdx,deviceTime,pressure,tiffFrame
```

The `tiffFrame` column marks which pressure rows were saved to the TIFF stack.
For Capture **Every**, every successfully matched row has a TIFF frame number.
For Capture **1 in 10**, rows associated with an actual trigger-counter increment
are marked; the first pulse depends on the firmware's retained divider phase.
For Capture **None**, the column is blank and no TIFF file is written.

Playback supports both older CSV files and the newer metadata-prefixed CSV
files.

## Windows Installation

End users should install PRIMA from the release installer:

```text
PRIMA-Setup-3.5.0.exe
```

Prerequisites:

- Windows 10 or newer, 64-bit
- IC Imaging Control / camera driver package from The Imaging Source
- PRIM Arduino device connected over USB

The installer creates a Program Files installation, Start Menu shortcut,
optional Desktop shortcut, and standard Windows uninstaller.

The camera driver is not bundled in the PRIMA installer. Install the camera
driver separately and verify the camera is visible to IC Imaging Control before
using PRIMA.

## Running From Source

Use Python 3.11 on Windows.

```bat
cd "C:\Users\Tykocki Lab - PRIM2\Documents\GitHub\PRIMAcquisition"
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r prim_app\requirements.txt
python prim_app\prim_app.py
```

## Building The Windows Installer

Install Inno Setup:

```bat
winget install -e --id JRSoftware.InnoSetup
```

Then build from the repository root:

```bat
installer\build_installer.bat
```

The installer output is:

```text
installer\output\PRIMA-Setup-3.5.0.exe
```

If the PyInstaller build succeeds but the script cannot find Inno Setup, compile
the installer directly:

```bat
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\PRIMA.iss
```

See [installer/README.md](installer/README.md) for the full packaging workflow.

## Troubleshooting

| Issue | Fix |
| ----- | --- |
| Camera not listed | Install IC Imaging Control / GenTL driver and reconnect the camera |
| Live preview is black | Confirm the camera is started, the camera driver sees the device, and the trigger/capture setup matches the experiment |
| Serial shows no data | Check the COM port, USB cable, and PRIM device power |
| TIFF has fewer frames than CSV rows | This is expected for Capture settings like `1 in 10` |
| No TIFF file is created | Capture is set to `None`, which records CSV only |
| TIFF will not open | Use ImageJ/Fiji or Python `tifffile` |
| Installer warns in Windows SmartScreen | Code signing is not configured yet |

## Developer Checks

Run focused tests from the repository root:

```bat
python -m unittest discover -s tests
```

Run a syntax check on changed modules as needed:

```bat
python -m py_compile prim_app\prim_app.py prim_app\main_window.py prim_app\recording_manager.py
```

## License

Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International

https://creativecommons.org/licenses/by-nc-sa/4.0/
