# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import PyQt6

tomato_exe = Path(SPECPATH) / 'packaging' / 'vendor' / 'tomato' / 'TomatoNovelDownloader.exe'
tomato_data = [(str(tomato_exe), 'tools/tomato')] if tomato_exe.is_file() else []
if tomato_data:
    tomato_data += [(str(tomato_exe.with_name(name)), 'tools/tomato') for name in ('LICENSE', 'NOTICE.txt')]

a = Analysis(
    ['gui_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('sites', 'sites'), ('packaging/assets/morandi/app-icon.png', 'assets/morandi')] + tomato_data,
    hiddenimports=['curl_cffi', 'browser_cookie3', 'websockets'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Qt6Core uses the unversioned ICU functions provided by Windows.  Some build
# environments expose Poppler's ICU 78 DLLs on PATH; PyInstaller can collect
# those by mistake, and they then shadow the compatible Windows ICU DLLs.
incompatible_icu = {'icuuc.dll', 'icudt78.dll'}
qt_bin = Path(PyQt6.__file__).resolve().parent / 'Qt6' / 'bin'
a.binaries = [
    entry for entry in a.binaries
    if os.path.basename(entry[0]).lower() not in incompatible_icu
    and not os.path.basename(entry[0]).lower().startswith('qt6')
]
for qt_dll in qt_bin.glob('Qt6*.dll'):
    a.binaries.append((qt_dll.name, str(qt_dll), 'BINARY'))
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='novelDownloader',
    icon='packaging/assets/morandi/app-icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory='.',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='novelDownloader',
)
