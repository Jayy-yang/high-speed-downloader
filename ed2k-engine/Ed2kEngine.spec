# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/Users/Jayy_yang/Documents/deepseek-harness/default-workspace/program/high-speed-downloader/ed2k-engine/engine.py'],
    pathex=['C:/Users/Jayy_yang/Documents/deepseek-harness/default-workspace/program/high-speed-downloader/ed2k-engine/vendor'],
    binaries=[],
    datas=[],
    hiddenimports=['Crypto.Hash.MD4', 'Crypto.Hash.SHA1'],
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
    exclude_binaries=True,
    name='Ed2kEngine',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    upx=False,
    upx_exclude=[],
    name='Ed2kEngine',
)
