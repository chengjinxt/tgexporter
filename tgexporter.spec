# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path.cwd()
datas = []
binaries = []
hiddenimports = []
sys.path.insert(0, str(ROOT / "src"))
hiddenimports += collect_submodules("tgexporter")

try:
    playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")
    datas += playwright_datas
    binaries += playwright_binaries
    hiddenimports += playwright_hiddenimports
except Exception:
    hiddenimports += ["playwright.sync_api"]


a = Analysis(
    ["scripts/pyinstaller_entry.py"],
    pathex=[str(ROOT / "src"), str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="tgexporter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
