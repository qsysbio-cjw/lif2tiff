# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).resolve().parents[1]
APP = ROOT / "qt_gui" / "app.py"

datas = [
    (str(ROOT / "resources" / "channel_registry.json"), "resources"),
    (str(ROOT / "resources" / "protocol_registry.json"), "resources"),
    (str(ROOT / "qt_gui" / "cellvis_384_stage_calibration.json"), "."),
]
datas += collect_data_files("liffile")

a = Analysis(
    [str(APP)],
    pathex=[str(ROOT / "src"), str(ROOT / "qt_gui"), str(ROOT / "viewer")],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("liffile"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=0,
)
# TIFF I/O is handled by tifffile. The Qt TIFF plugin in the Linux wheel
# requires legacy libtiff.so.5 on current Ubuntu and is not used by the app.
a.binaries = [
    entry
    for entry in a.binaries
    if not entry[0].replace("\\", "/").endswith(
        "PySide6/Qt/plugins/imageformats/libqtiff.so"
    )
]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LIF2TIFF-GUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LIF2TIFF-GUI",
)
