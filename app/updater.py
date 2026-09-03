"""
Auto-updater module for Zebra Print Bridge.
Checks a version.json hosted on GitHub Pages and offers to download a new installer.
"""

import glob
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from packaging.version import Version

logger = logging.getLogger(__name__)

# ---- CONFIGURE THESE BEFORE FIRST RELEASE ----
# Use the raw GitHub content URL since GitHub Pages is disabled.
VERSION_URL = "https://raw.githubusercontent.com/Goodness-Gardens/zebra-print-bridge-win/main/version.json"


def get_local_version() -> str:
    """Return the current local version string from app/__init__.py."""
    try:
        from app import __version__
        return __version__
    except ImportError:
        return "0.0.0"


def fetch_remote_version_info(url: str = VERSION_URL, timeout: int = 10) -> Optional[Dict]:
    """
    Fetch version.json from the GitHub repository raw content.
    Expected shape:
    {
        "version": "2.1.0",
        "download_url": "https://github.com/…/releases/latest/download/ZebraBridgeSetup.exe",
        "notes": "Bug fixes."
    }
    """
    try:
        import time
        cache_buster_url = f"{url}?t={int(time.time())}"
        logger.info("Fetching remote version from: %s", cache_buster_url)
        req = Request(
            cache_buster_url, 
            headers={
                "User-Agent": "ZebraPrintBridge-Updater/1.0",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            logger.info("Remote version profile retrieved successfully: v%s", data.get("version"))
            return data
    except Exception as exc:
        logger.warning("Failed to retrieve remote version profile. Type: %s, Reason: %s", type(exc).__name__, exc)
        if hasattr(exc, 'read'):
            logger.warning("Error body: %s", exc.read().decode('utf-8', errors='ignore'))
        return None


def is_update_available(remote_info: Dict) -> bool:
    """Compare remote version against local version."""
    try:
        remote_ver = Version(remote_info.get("version", "0.0.0"))
        local_ver = Version(get_local_version())
        return remote_ver > local_ver
    except Exception as exc:
        logger.warning("Version comparison failed. Reason: %s", exc)
        return False


def download_installer(
    download_url: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Optional[Path]:
    """
    Download the installer .exe to a temp directory.
    Returns the path to the downloaded file, or None on failure.
    ``progress_callback(bytes_downloaded, total_bytes)`` is called periodically.
    """
    tmp_dir_path = None
    try:
        req = Request(download_url, headers={"User-Agent": "ZebraPrintBridge-Updater/1.0"})
        with urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            # Clean up old update temp dirs before creating a new one
            _cleanup_old_update_dirs()
            tmp_dir = tempfile.mkdtemp(prefix="zbr_update_")
            tmp_dir_path = tmp_dir
            filename = download_url.split("/")[-1] or "ZebraBridgeSetup.exe"
            dest = Path(tmp_dir) / filename

            downloaded = 0
            chunk_size = 256 * 1024  # 256 KB chunks
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

            logger.info("Installer successfully downloaded to %s (Size: %d bytes).", dest, downloaded)
            return dest
    except Exception as exc:
        logger.error("Update download failed. Reason: %s", exc)
        if tmp_dir_path and os.path.exists(tmp_dir_path):
            try:
                import shutil
                shutil.rmtree(tmp_dir_path, ignore_errors=True)
                logger.info("Cleaned up incomplete update directory: %s", tmp_dir_path)
            except Exception as cleanup_exc:
                logger.error("Failed to clean up update directory: %s", cleanup_exc)
        return None


def launch_installer_and_exit(installer_path: Path):
    """Launch the downloaded installer and close the current application."""
    if sys.platform == "win32":
        logger.info("Executing update installer: %s", installer_path)
        subprocess.Popen([str(installer_path)])
        sys.exit(0)
    else:
        logger.info("Update installer downloaded to: %s. Automatic execution is only supported on Windows.", installer_path)


def check_for_updates_async(callback: Callable[[Optional[Dict]], None]):
    """
    Non-blocking update check.  Calls ``callback(remote_info)`` on completion.
    ``remote_info`` is None when no update is available or on network errors.
    """
    def _worker():
        info = fetch_remote_version_info()
        if info and is_update_available(info):
            callback(info)
        else:
            callback(None)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def _cleanup_old_update_dirs():
    """Remove leftover zbr_update_* dirs from previous update attempts."""
    try:
        tmp_root = tempfile.gettempdir()
        for d in glob.glob(os.path.join(tmp_root, "zbr_update_*")):
            if os.path.isdir(d):
                try:
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass
