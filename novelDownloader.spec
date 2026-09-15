# -*- mode: python ; coding: utf-8 -*-

import os


a = Analysis(
    ['gui_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('sites', 'sites')],
    hiddenimports=['curl_cffi', 'browser_cookie3'],
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
a.binaries = [
    entry for entry in a.binaries
    if os.path.basename(entry[0]).lower() not in incompatible_icu
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='novelDownloader',
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
    contents_directory='.',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='novelDownloader',
)
