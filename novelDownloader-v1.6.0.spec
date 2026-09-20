# -*- mode: python ; coding: utf-8 -*-

import os


a = Analysis(
    ['gui_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('sites', 'sites')],
    hiddenimports=['curl_cffi'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Keep the same Qt/Windows ICU exclusion as novelDownloader.spec. Otherwise
# libraries from a build tool on PATH can shadow the Windows ICU API.
incompatible_icu = {'icuuc.dll', 'icudt78.dll'}
a.binaries = [
    entry for entry in a.binaries
    if os.path.basename(entry[0]).lower() not in incompatible_icu
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='novelDownloader-v1.6.0',
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
)
