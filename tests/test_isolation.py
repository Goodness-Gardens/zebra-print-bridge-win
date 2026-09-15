from pathlib import Path
from tests.conftest import (
    REAL_CACHE_FILE,
    _INITIAL_EXISTS,
    _INITIAL_MTIME,
    _INITIAL_CONTENT,
)


def test_real_config_dir_not_modified_by_suite():
    """Verify that running the test suite never modifies the real user cache file."""
    if _INITIAL_EXISTS:
        assert REAL_CACHE_FILE.exists(), "Real cache file was deleted"
        assert REAL_CACHE_FILE.stat().st_mtime == _INITIAL_MTIME, "Real cache file mtime was modified"
        assert REAL_CACHE_FILE.read_bytes() == _INITIAL_CONTENT, "Real cache file content was modified"
    else:
        assert not REAL_CACHE_FILE.exists(), "Real cache file was unexpectedly created"
