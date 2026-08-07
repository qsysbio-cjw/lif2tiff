# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).resolve().parents[1]
APP = ROOT / "qt_gui" / "app.py"
VERSION_FILE = ROOT / "packaging" / "windows" / "version_info.txt"
ICON_FILE = ROOT / "resources" / "branding" / "lif2tiff-icon-v2.ico"

datas = [
    (str(ROOT / "resources" / "channel_registry.json"), "resources"),
    (str(ROOT / "resources" / "protocol_registry.json"), "resources"),
    (str(ROOT / "resources" / "branding" / "lif2tiff-icon-v2.png"), "resources/branding"),
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
    version=str(VERSION_FILE),
    icon=str(ICON_FILE),
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
