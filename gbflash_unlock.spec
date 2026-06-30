# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

block_cipher = None
root = Path.cwd()
libusb = Path("/opt/homebrew/lib/libusb-1.0.dylib")
extra_binaries = [(str(libusb), ".")] if libusb.exists() else []

a = Analysis(
    ["gbflash_unlock_app.py"],
    pathex=[str(root)],
    binaries=extra_binaries,
    datas=[
        ("assets/gbflash_unlock_logo.svg", "assets"),
        ("chisp_flasher/data/chipdb.yaml", "chisp_flasher/data"),
    ],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "serial",
        "serial.tools.list_ports",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="gbflash-unlock",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="gbflash-unlock",
)
app = BUNDLE(
    coll,
    name="GBFlash Unlock.app",
    icon=None,
    bundle_identifier="dev.gbflash.unlock",
)
