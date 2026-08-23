# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["product_app.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("templates", "templates"),
        ("static", "static"),
        ("README.md", "."),
        ("LICENSE", "."),
        ("THIRD_PARTY_NOTICES.md", "."),
    ],
    hiddenimports=[
        "engineio.async_drivers.threading",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BiliCommentReviewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    version="version_info.txt",
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="BiliCommentReviewer",
)
