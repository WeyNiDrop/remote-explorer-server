# -*- mode: python ; coding: utf-8 -*-

import os
import sys


APP_NAME = 'Remote Explorer Server'
EXECUTABLE_NAME = 'remote-explorer-server'
APP_VERSION = '0.1.2'
BUNDLE_IDENTIFIER = 'com.remoteexplorer.server'

is_macos = sys.platform == 'darwin'
is_windows = sys.platform.startswith('win')

target_arch = os.environ.get('PYINSTALLER_TARGET_ARCH') if is_macos else None
macos_min_version = os.environ.get('MACOSX_DEPLOYMENT_TARGET', '11.0')
windows_icon_path = 'assets/remote-explorer.ico' if is_windows else None
macos_icon_path = 'assets/remote-explorer.icns' if is_macos else None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'aiortc',
        'av',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'remote_explorer_server.browser',
        'remote_explorer_server.webrtc',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if is_macos:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=EXECUTABLE_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=target_arch,
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
        name=EXECUTABLE_NAME,
    )
    app = BUNDLE(
        coll,
        name=f'{APP_NAME}.app',
        icon=macos_icon_path,
        bundle_identifier=BUNDLE_IDENTIFIER,
        info_plist={
            'CFBundleName': APP_NAME,
            'CFBundleDisplayName': APP_NAME,
            'CFBundleShortVersionString': APP_VERSION,
            'CFBundleVersion': APP_VERSION,
            'LSApplicationCategoryType': 'public.app-category.utilities',
            'LSMinimumSystemVersion': macos_min_version,
            'NSHighResolutionCapable': True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=EXECUTABLE_NAME,
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
        icon=windows_icon_path,
    )
