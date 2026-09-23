from pathlib import Path
import pytest

# Record real user home and config directory BEFORE any test runs or patches
REAL_HOME = Path.home()
REAL_CONFIG_DIR = REAL_HOME / ".config" / "zebra-print-bridge"
REAL_CACHE_FILE = REAL_CONFIG_DIR / "network_printers.json"

_INITIAL_EXISTS = REAL_CACHE_FILE.exists()
_INITIAL_MTIME = REAL_CACHE_FILE.stat().st_mtime if _INITIAL_EXISTS else None
_INITIAL_CONTENT = REAL_CACHE_FILE.read_bytes() if _INITIAL_EXISTS else None


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch, tmp_path):
    """
    Isolate test runs from user's real home directory and config files.
    Redirects HOME, USERPROFILE, and patches Path.home() to a temporary directory.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield
