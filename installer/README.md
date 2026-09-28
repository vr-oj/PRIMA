# Windows Installer

This folder contains the Windows installer workflow for PRIMA. The repository is
named PRIMAcquisition, but the installed app is named PRIMA.

## Prerequisites

- Python 3.11 64-bit
- Inno Setup 6
- IC Imaging Control / camera driver installed on the target machine
- A working PyInstaller build environment for this repository

Install Inno Setup from a terminal:

```bat
winget install -e --id JRSoftware.InnoSetup
```

## Build

From the repository root:

```bat
installer\build_installer.bat
```

The script builds the PyInstaller app first, then creates:

```text
installer\output\PRIMA-Setup-3.5.0.exe
```

The installer includes the PyInstaller app from:

```text
dist\PRIMA\PRIMA.exe
```

The camera driver is not bundled. Install IC Imaging Control separately on the
machine that will run PRIMA.

## Manual Build

If you prefer to run each step manually, build the app first:

```bat
python -m PyInstaller PRIMAcquisition.spec --clean --noconfirm
```

Then compile the installer:

```bat
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\PRIMA.iss
```

If that path does not exist, locate the compiler with:

```bat
where /r "%LOCALAPPDATA%" ISCC.exe
where /r "%ProgramFiles(x86)%" ISCC.exe
where /r "%ProgramFiles%" ISCC.exe
```

## Installer Behavior

- Installs to `Program Files\PRIMA`
- Creates a Start Menu shortcut
- Offers an optional Desktop shortcut
- Adds a standard Windows uninstaller
- Uses the PRIM application icon
- Produces a versioned installer filename

## Code Signing

The installer is usable without signing, but Windows SmartScreen may warn users.
For a more official release, sign
`installer\output\PRIMA-Setup-3.5.0.exe` with a Windows code-signing
certificate.
