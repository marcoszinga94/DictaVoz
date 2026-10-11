# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec para generar DictaVoz.exe (Windows, onefile).

Uso (en Windows, desde la raíz del proyecto):
    pyinstaller --noconfirm packaging/windows/dictavoz_win.spec

La variable de entorno WHISPER_DIR puede apuntar a la carpeta con
whisper-cli.exe (se empaqueta junto al .exe).
"""
import os

from PyInstaller.utils.hooks import collect_submodules

# Carpeta raíz del proyecto (donde está el paquete dictavoz/).
PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
SCRIPT = os.path.join(SPECPATH, "dictavoz_win.py")
ICON = os.path.join(PROJECT_ROOT, "icons", "dictavoz.ico")

WHISPER_DIR = os.environ.get("WHISPER_DIR", "")

binaries = []
if WHISPER_DIR:
    cli = os.path.join(WHISPER_DIR, "whisper-cli.exe")
    if os.path.isfile(cli):
        binaries.append((cli, "whisper"))
        print("Empaquetando whisper-cli.exe desde", cli)

a = Analysis(
    [SCRIPT],
    pathex=[PROJECT_ROOT, SPECPATH],
    binaries=binaries,
    datas=[],
    hiddenimports=collect_submodules("dictavoz"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["gtk", "gi", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DictaVoz",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=os.path.join(SPECPATH, "DictaVoz_version_info.txt"),
)