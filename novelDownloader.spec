# -*- mode: python ; coding: utf-8 -*-
block_cipher = None
this_dir = r'E:\AI gravity project\novel-downloader'

a = Analysis(
    [this_dir + '\\gui_launcher.py'],
    pathex=[this_dir],
    binaries=[],
    datas=[(this_dir + '\\sites', 'sites')],
    hiddenimports=['curl_cffi'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludedimports=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='novelDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
