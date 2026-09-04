"""
Shared utility functions for Zebra Print Bridge.
Centralised here to avoid duplication across modules.
"""

import re
import socket
from typing import Optional, Tuple

HOSTNAME_LABEL_REGEX = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9_-]{0,61}[a-zA-Z0-9])?$")


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


def get_hostname() -> str:
    """Get the local network hostname of this machine."""
    try:
        return socket.gethostname()
    except Exception:
        return "localhost"


def is_valid_ipv4(ip: str) -> bool:
    """Validate if a string is a standard IPv4 address."""
    parts = (ip or "").strip().split(".")
    if len(parts) != 4:
        return False
    if not all(part.isdigit() for part in parts):
        return False
    return all(0 <= int(part) <= 255 for part in parts)


def is_valid_hostname(hostname: str) -> bool:
    """Validate if a string is a syntactically valid hostname or local network name (RFC 1123 / mDNS)."""
    name = (hostname or "").strip()
    if not name or len(name) > 253:
        return False
    if name.endswith("."):
        name = name[:-1]
    if not name:
        return False
    labels = name.split(".")
    return all(
        len(label) > 0
        and len(label) <= 63
        and bool(HOSTNAME_LABEL_REGEX.match(label))
        for label in labels
    )


def parse_target_address_port(target: str, default_port: int = 9100) -> Tuple[str, int]:
    """Extract host address and optional port from a target string."""
    value = normalize_target(target)
    if not value:
        return "", default_port

    if ":" in value and not value.endswith("]"):
        parts = value.rsplit(":", 1)
        if parts[1].isdigit():
            port = int(parts[1])
            if 1 <= port <= 65535:
                return parts[0], port

    return value, default_port


def is_valid_target(target: str) -> bool:
    """Accept IPv4 targets, network hostnames, optional port, and the special 'test' destination."""
    value = normalize_target(target)
    if not value:
        return False
    if value.lower() == "test":
        return True

    host, _ = parse_target_address_port(value)
    return is_valid_ipv4(host) or is_valid_hostname(host)


def strip_wrapping_quotes(value: str) -> str:
    """Remove accidental surrounding single or double quotes from a string."""
    val = (value or "").strip()
    while len(val) >= 2 and (
        (val[0] == '"' and val[-1] == '"')
        or (val[0] == "'" and val[-1] == "'")
    ):
        val = val[1:-1].strip()
    return val


def normalize_target(target: str) -> str:
    """Normalize printer target and remove accidental wrapping quotes."""
    return strip_wrapping_quotes(target)


def normalize_raw_command(command: str) -> str:
    """Normalize raw payload and remove accidental wrapping quotes."""
    return strip_wrapping_quotes(command)

