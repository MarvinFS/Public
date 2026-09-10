# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src\\main.py'],
    pathex=['src'],
    binaries=[],
    datas=[('resources', 'resources')],
    hiddenimports=[
        'config', 'data_collector', 'tray', 'models', 'oauth_usage',
        'openai_usage', 'claude_check', 'codex_log_parser',
        'codex_pricing', 'currency', 'icons', 'log_parser', 'pricing',
        'snapshot_cache', 'model_catalog', 'ui_window',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'numpy.core', 'numpy.random', 'numpy.f2py', 'pytest', 'pytest_cov', '_pytest'],
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
    icon=['resources\\icons\\app_icon.ico'],
)
