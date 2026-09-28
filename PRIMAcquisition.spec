# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for PRIMAcquisition.

This spec collects the application icons and stylesheet so the GUI has a
consistent, polished look across all platforms.
"""

import os
import importlib.util

from PyInstaller.utils.hooks import collect_all

source_script = os.path.join("prim_app", "prim_app.py")
icon_file = os.path.join("prim_app", "ui", "icons", "PRIM.ico")
camera_datas, camera_binaries, camera_hiddenimports, camera_excludes = [], [], [], []
for bridge in ("imagingcontrol4", "pymmcore"):
    if importlib.util.find_spec(bridge) is not None:
        datas, binaries, imports = collect_all(bridge)
        camera_datas.extend(datas)
        camera_binaries.extend(binaries)
        camera_hiddenimports.extend([bridge, *imports])
    else:
        camera_excludes.append(bridge)

data_files = [
    (os.path.join("prim_app", "ui", "icons", "*"), os.path.join("prim_app", "ui", "icons")),
    (os.path.join("prim_app", "ui", "style.qss"), os.path.join("prim_app", "ui")),
    (os.path.join("prim_app", "docs", "*"), os.path.join("prim_app", "docs")),
]
data_files += camera_datas

a = Analysis(
    [source_script],
    pathex=[],
    binaries=camera_binaries,
    datas=data_files,
    hiddenimports=camera_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join("prim_app", "hooks", "mm_worker_bootstrap.py")],
    excludes=camera_excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PRIMA",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PRIMA",
)
