# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).resolve().parents[1]
APP = ROOT / "src" / "liftool.py"
VERSION_FILE = ROOT / "packaging" / "windows" / "version_info.txt"

datas = [
    (str(ROOT / "resources" / "channel_registry.json"), "resources"),
    (str(ROOT / "resources" / "protocol_registry.json"), "resources"),
]
datas += collect_data_files("liffile")

a = Analysis(
    [str(APP)],
    pathex=[str(ROOT / "src")],
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
    a.binaries,
    a.datas,
    [],
    name="lif2tiff",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    version=str(VERSION_FILE),
)
