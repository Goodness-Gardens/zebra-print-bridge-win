"""
Shared utility functions for Zebra Print Bridge.
Centralised here to avoid duplication across modules.
"""

import socket
from typing import Optional


def get_local_ip() -> Optional[str]:
    """Get the local network IP address of this machine."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None


def normalize_target(target: str) -> str:
    """Normalize printer target and remove accidental wrapping quotes."""
    value = (target or "").strip()
    while len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
    ):
        value = value[1:-1].strip()
    return value


def normalize_raw_command(command: str) -> str:
    """Normalize raw payload and remove accidental wrapping quotes."""
    value = (command or "").strip()
    while len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
    ):
        value = value[1:-1].strip()
    return value
