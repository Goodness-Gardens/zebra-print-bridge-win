#!/usr/bin/env python3
"""
Build script for Zebra Print Bridge – creates a standalone .exe via PyInstaller.

Usage:
    python build.py          # creates dist/ZebraPrintBridge/ZebraPrintBridge.exe
    python build.py --onefile # creates dist/ZebraPrintBridge.exe (single file)
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "ZebraPrintBridge"
ENTRY = ROOT / "run_gui.py"
ICON = ROOT / "resources" / "icon.png"
RESOURCES = ROOT / "resources"

def get_version() -> str:
    """Read version from app/__init__.py as the single source of truth"""
    init_path = ROOT / "app" / "__init__.py"
    try:
        with open(init_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('__version__'):
                    return ast.literal_eval(line.split('=')[1].strip())
    except Exception as e:
        print(f"Warning: Could not read version from {init_path}: {e}")
    return "2.0.0"

def sync_versions(version: str):
    """Update version.json and installer.iss with the current version"""
    # 1. Update version.json (preserve existing fields like download_url, notes)
    version_file = ROOT / "version.json"
    try:
        with open(version_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data['version'] = version
        with open(version_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
            f.write('\n')
        print(f"[OK] Updated version.json to {version}")
    except Exception as e:
        print(f"Warning: Could not update version.json: {e}")

    # 2. Update installer.iss
    iss_file = ROOT / "installer.iss"
    try:
        with open(iss_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Replace #define MyAppVersion "..."
        new_content = re.sub(
            r'(#define\s+MyAppVersion\s+)"[^"]+"',
            rf'\1"{version}"',
            content
        )
        
        with open(iss_file, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"[OK] Updated installer.iss to {version}")
    except Exception as e:
        print(f"Warning: Could not update installer.iss: {e}")


# CustomTkinter data path (required for PyInstaller to bundle its assets)
try:
    import customtkinter
    CTK_PATH = Path(customtkinter.__file__).parent
except ImportError:
    CTK_PATH = None


def build(one_file: bool = False):
    version = get_version()
    print(f"Detected version: {version}")
    sync_versions(version)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--windowed",                    # no console window
        "--icon", str(ROOT / "resources" / "icon.ico"), # Set application icon
        # Bundle the resources/ folder
        "--add-data", f"{RESOURCES};resources",
        # Bundle the app/ package
        "--add-data", f"{ROOT / 'app'};app",
        # Bundle version.json
        "--add-data", f"{ROOT / 'version.json'};.",
    ]

    # Bundle CustomTkinter assets
    if CTK_PATH:
        cmd += ["--add-data", f"{CTK_PATH};customtkinter"]

    # Hidden imports that PyInstaller might miss
    cmd += [
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols",
        "--hidden-import", "uvicorn.protocols.http",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "uvicorn.lifespan.off",
        "--hidden-import", "httptools",
        "--hidden-import", "email.mime.multipart",
        "--hidden-import", "email.mime.text",
        # win32print for local/USB printer support (Windows-only)
        "--hidden-import", "win32print",
        "--hidden-import", "win32api",
    ]

    if one_file:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")

    cmd.append(str(ENTRY))

    print(f"\n{'='*60}")
    print(f"  Building {APP_NAME}")
    print(f"  Mode: {'single file' if one_file else 'directory'}")
    print(f"{'='*60}\n")
    print(" ".join(cmd))
    print()

    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode == 0:
        if one_file:
            exe = ROOT / "dist" / f"{APP_NAME}.exe"
        else:
            exe = ROOT / "dist" / APP_NAME / f"{APP_NAME}.exe"
        print(f"\n[OK] Build succeeded!\n   -> {exe}\n")
    else:
        print("\n[FAIL] Build failed.\n")
        sys.exit(1)


if __name__ == "__main__":
    one_file = "--onefile" in sys.argv
    build(one_file)
