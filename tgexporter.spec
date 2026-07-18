# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from pathlib import PurePosixPath
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path.cwd()
BROWSER_ROOT = Path.home() / "AppData" / "Local" / "ms-playwright"
datas = []
binaries = []
hiddenimports = []
sys.path.insert(0, str(ROOT / "src"))
hiddenimports += collect_submodules("tgexporter")


def collect_tree_files(source_dir: Path, target_dir: PurePosixPath):
    collected = []
    for path in source_dir.rglob("*"):
        if not path.is_file():
            continue
        relative_parent = PurePosixPath(path.relative_to(source_dir).parent.as_posix())
        collected.append((str(path), str(target_dir / relative_parent)))
    return collected


try:
    playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")
    datas += playwright_datas
    binaries += playwright_binaries
    hiddenimports += playwright_hiddenimports
except Exception:
    hiddenimports += ["playwright.sync_api"]

if BROWSER_ROOT.exists():
    for browser_path in sorted(BROWSER_ROOT.iterdir()):
        if not browser_path.is_dir() or not browser_path.name.startswith(("chromium-", "ffmpeg-")):
            continue
        datas += collect_tree_files(
            browser_path,
            PurePosixPath("playwright/driver/package/.local-browsers") / browser_path.name,
        )


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
    [],
    name="tgexporter",
    debug=False,
    exclude_binaries=True,
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="tgexporter-portable",
)
