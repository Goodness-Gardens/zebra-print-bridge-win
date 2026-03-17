# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\S117r\\Documents\\develop\\zebra-print-bridge-win\\run_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\S117r\\Documents\\develop\\zebra-print-bridge-win\\resources', 'resources'), ('C:\\Users\\S117r\\Documents\\develop\\zebra-print-bridge-win\\app', 'app'), ('C:\\Users\\S117r\\Documents\\develop\\zebra-print-bridge-win\\version.json', '.'), ('C:\\Users\\S117r\\Documents\\develop\\zebra-print-bridge-win\\venv\\Lib\\site-packages\\customtkinter', 'customtkinter')],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on', 'uvicorn.lifespan.off', 'httptools', 'email.mime.multipart', 'email.mime.text', 'win32print', 'win32api'],
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
    name='ZebraPrintBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\S117r\\Documents\\develop\\zebra-print-bridge-win\\resources\\icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ZebraPrintBridge',
)
