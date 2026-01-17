# -*- mode: python ; coding: utf-8 -*-

import os
spec_dir = os.path.dirname(os.path.abspath(SPEC))
project_dir = os.path.dirname(spec_dir)
src_dir = os.path.join(project_dir, 'src')
resources_dir = os.path.join(project_dir, 'resources')

block_cipher = None

a = Analysis(
    [os.path.join(src_dir, 'main.py')],
    pathex=[src_dir],
    binaries=[],
    datas=[(resources_dir, 'resources')],
    hiddenimports=['PIL._tkinter_finder'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'numpy',
        'scipy',
        'pandas',
        'matplotlib',
        'PyYAML',
        'yaml',
        'pytest',
        'unittest',
        'doctest',
        'pydoc',
        'xmlrpc',
        'multiprocessing',
    ],
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
    name='ClaudeBar',
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
    icon=os.path.join(resources_dir, 'icons', 'app_icon.ico'),
)
