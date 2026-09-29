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

# Pillow's wheel carries a decoder for every format it supports. ClaudeBar opens
# only PNG, so the AVIF, WebP and colour-management plugins are dead weight
# (8.5 MB, most of it AVIF). Pillow imports a plugin only when a file of that
# format is opened, so nothing here ever loads them. The Linux build drops the
# same three.
_UNUSED_BY_THIS_APP = ("PIL\\_avif.", "PIL\\_webp.", "PIL\\_imagingcms.")
a.binaries = [entry for entry in a.binaries
              if not any(marker in entry[0] for marker in _UNUSED_BY_THIS_APP)]

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
