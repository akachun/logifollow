# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for macOS LogiFollow.app (메뉴바 전용 번들)."""

from PyInstaller.utils.hooks import collect_dynamic_libs

block_cipher = None

a = Analysis(
    ["../logifollow_app.py"],
    pathex=[".."],
    binaries=collect_dynamic_libs("hid"),
    datas=[],
    hiddenimports=[
        "hid",
        "logifollow",
        "ble_transport",
        "edge_config",
        "edge_watcher",
        "tray_ui",
        "objc",
        "Foundation",
        "AppKit",
        "Quartz",
        "CoreBluetooth",
        "dispatch",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "test"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LogiFollow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="LogiFollow",
)

app = BUNDLE(
    coll,
    name="LogiFollow.app",
    icon=None,
    bundle_identifier="dev.logifollow.macos",
    info_plist={
        "CFBundleName": "LogiFollow",
        "CFBundleDisplayName": "LogiFollow",
        "CFBundleShortVersionString": "0.4.0",
        "CFBundleVersion": "4",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSBluetoothAlwaysUsageDescription": "로지텍 키보드와 마우스의 블루투스(BLE++) 채널 전환을 위해 블루투스 권한이 필요합니다.",
    },
)
