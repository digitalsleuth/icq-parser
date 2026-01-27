# -*- mode: python ; coding: utf-8 -*-

__version__ = '1.4.1'

a = Analysis(
    ['icq_parser/icq_parser.py'],
    pathex=['.'],
    binaries=[],
    datas=[('icq_parser/static', 'icq_parser/static'), ('icq_parser/templates', 'icq_parser/templates')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f'icq-parser-{__version__}',
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
    version='version.txt',
    icon=['ip.ico'],
)
