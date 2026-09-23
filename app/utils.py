"""
Shared utility functions for Zebra Print Bridge.
Centralised here to avoid duplication across modules.
"""

import platform
import re
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple, Dict, List

HOSTNAME_LABEL_REGEX = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9_-]{0,61}[a-zA-Z0-9])?$")


def run_command(cmd: List[str], timeout: float = 5.0) -> str:
    """
    Run a system command safely and return its stdout as a string.
    - On Windows, uses creationflags=subprocess.CREATE_NO_WINDOW to avoid flashing console windows.
    - Captures bytes and decodes with errors='ignore' (resilient to localized output/accents).
    - stderr is suppressed via DEVNULL.
    - Times out after timeout seconds.
    - Returns empty string on any failure/timeout/exception.
    """
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "timeout": timeout,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        proc = subprocess.run(cmd, **kwargs)
        if proc.stdout:
            return proc.stdout.decode("utf-8", errors="ignore")
        return ""
    except Exception:
        return ""


_local_mac_cache: Dict[str, Tuple[Optional[str], float]] = {}
_local_mac_lock = threading.Lock()
LOCAL_MAC_CACHE_TTL = 60.0


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

    if is_valid_mac(value):
        return normalize_mac(value) or value, default_port

    if ":" in value and not value.endswith("]"):
        parts = value.rsplit(":", 1)
        if parts[1].isdigit():
            port = int(parts[1])
            if 1 <= port <= 65535:
                return parts[0], port

    return value, default_port


def normalize_mac(mac: str) -> Optional[str]:
    """
    Normalize MAC address to standard uppercase format: '00:07:4D:6F:C2:14'.
    Supports:
    - Colon-delimited: '00:07:4D:6F:C2:14' or '0:7:4d:6f:c2:14'
    - Dash-delimited: '00-07-4D-6F-C2-14'
    - Dot-delimited: '0007.4d6f.c214'
    - Plain 12 hex digits: '00074d6fc214'
    """
    if not mac:
        return None
    val = normalize_target(mac).strip().lower()

    if ":" in val or "-" in val:
        sep = ":" if ":" in val else "-"
        parts = val.split(sep)
        if len(parts) == 6:
            try:
                clean_parts = [f"{int(p, 16):02X}" for p in parts]
                return ":".join(clean_parts)
            except ValueError:
                return None

    if "." in val:
        parts = val.split(".")
        if len(parts) == 3 and all(len(p) == 4 for p in parts):
            clean_hex = "".join(parts)
            try:
                bytes.fromhex(clean_hex)
                return ":".join(clean_hex[i:i+2].upper() for i in range(0, 12, 2))
            except ValueError:
                return None

    clean_hex = re.sub(r"[^0-9a-fA-F]", "", val)
    if len(clean_hex) == 12:
        try:
            bytes.fromhex(clean_hex)
            return ":".join(clean_hex[i:i+2].upper() for i in range(0, 12, 2))
        except ValueError:
            return None

    return None


def is_valid_mac(mac: str) -> bool:
    """Validate if string is an Ethernet MAC address in any common format."""
    return normalize_mac(mac) is not None


def _resolve_local_mac(ip: Optional[str] = None) -> Optional[str]:
    """Inner resolution for local machine MAC address using system tools."""
    if not ip:
        ip = get_local_ip()

    system = platform.system()

    # 1. macOS / Linux: Try ifconfig
    if system in ("Darwin", "Linux"):
        try:
            out = run_command(["ifconfig"])
            blocks = re.split(r"\n(?=[a-zA-Z0-9_-]+:)", out)
            matched_mac = None
            first_active_mac = None
            for b in blocks:
                m_mac = re.search(
                    r"(?:ether|HWaddr)\s+([0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2})",
                    b,
                )
                if not m_mac:
                    continue
                cand_mac = normalize_mac(m_mac.group(1))
                if not cand_mac:
                    continue
                m_ip = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", b)
                if m_ip:
                    cand_ip = m_ip.group(1)
                    if not cand_ip.startswith("127."):
                        if not first_active_mac:
                            first_active_mac = cand_mac
                        if ip and cand_ip == ip:
                            matched_mac = cand_mac
                            break
            if matched_mac:
                return matched_mac
            if first_active_mac:
                return first_active_mac
        except Exception:
            pass

    # 2. Linux: Try /sys/class/net and ip route / ip addr
    if system == "Linux":
        try:
            if ip:
                out = run_command(["ip", "-o", "addr", "show"])
                for line in out.splitlines():
                    if ip in line:
                        parts = line.split()
                        if len(parts) >= 2:
                            dev = parts[1]
                            addr_path = Path(f"/sys/class/net/{dev}/address")
                            if addr_path.exists():
                                norm = normalize_mac(addr_path.read_text().strip())
                                if norm:
                                    return norm
        except Exception:
            pass

    # 3. Windows: Try ipconfig /all
    if system == "Windows":
        try:
            out = run_command(["ipconfig", "/all"])
            blocks = re.split(r"\n(?=[^\s].*?:)", out)
            matched_mac = None
            first_active_mac = None
            for b in blocks:
                m_mac = re.search(r"([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})", b)
                if not m_mac:
                    m_mac = re.search(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", b)
                if not m_mac:
                    continue
                cand_mac = normalize_mac(m_mac.group(1))
                if not cand_mac:
                    continue
                m_ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", b)
                if m_ip:
                    cand_ip = m_ip.group(1)
                    if not cand_ip.startswith("127."):
                        if not first_active_mac:
                            first_active_mac = cand_mac
                        if ip and cand_ip == ip:
                            matched_mac = cand_mac
                            break
            if matched_mac:
                return matched_mac
            if first_active_mac:
                return first_active_mac
        except Exception:
            pass

    # 4. Fallback: uuid.getnode()
    try:
        node = uuid.getnode()
        # Discard if multicast bit is set (bit 40), indicating a pseudo-random MAC
        if (node >> 40) & 1 == 1:
            return None
        mac_hex = f"{node:012X}"
        if len(mac_hex) == 12:
            return normalize_mac(mac_hex)
    except Exception:
        pass

    return None


def get_local_mac(ip: Optional[str] = None, use_cache: bool = True) -> Optional[str]:
    """
    Get the MAC address of the local machine, preferably matching the provided IP
    (or default network IP) for the active network interface.
    Cached in memory with a 60-second TTL.
    """
    now = time.time()
    cache_key = ip or ""
    if use_cache:
        with _local_mac_lock:
            if cache_key in _local_mac_cache:
                cached_mac, ts = _local_mac_cache[cache_key]
                if now - ts < LOCAL_MAC_CACHE_TTL:
                    return cached_mac

    mac = _resolve_local_mac(ip)

    if use_cache:
        with _local_mac_lock:
            _local_mac_cache[cache_key] = (mac, now)

    return mac


def get_mac_for_ip(ip: str) -> str:
    """Look up the MAC address for a given IP in the OS ARP cache or local machine."""
    if not ip or not is_valid_ipv4(ip):
        return ""
    if ip == get_local_ip() or ip.startswith("127."):
        local_mac = get_local_mac(ip if not ip.startswith("127.") else None)
        if local_mac:
            return local_mac
    cmd = ["arp", "-an"] if platform.system() in ("Darwin", "Linux") else ["arp", "-a"]
    try:
        out = run_command(cmd)
        for line in out.splitlines():
            if ip in line:
                m = re.search(r"([0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2})", line)
                if m:
                    norm = normalize_mac(m.group(1))
                    if norm:
                        return norm
    except Exception:
        pass
    return ""


def get_ip_for_mac(mac: str) -> str:
    """Look up the IP address for a given MAC in the OS ARP cache or local machine."""
    target_mac = normalize_mac(mac)
    if not target_mac:
        return ""
    local_mac = get_local_mac()
    if local_mac and local_mac == target_mac:
        local_ip = get_local_ip()
        if local_ip:
            return local_ip
    cmd = ["arp", "-an"] if platform.system() in ("Darwin", "Linux") else ["arp", "-a"]
    try:
        out = run_command(cmd)
        for line in out.splitlines():
            m_ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            m_mac = re.search(r"([0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2}[:-][0-9a-fA-F]{1,2})", line)
            if m_ip and m_mac:
                cand_mac = normalize_mac(m_mac.group(1))
                if cand_mac and cand_mac == target_mac:
                    return m_ip.group(1)
    except Exception:
        pass
    return ""


def is_valid_target(target: str) -> bool:
    """Accept IPv4 targets, network hostnames, MAC addresses, optional port, and the special 'test' destination."""
    value = normalize_target(target)
    if not value:
        return False
    if value.lower() == "test":
        return True
    if is_valid_mac(value):
        return True

    host, _ = parse_target_address_port(value)
    return is_valid_ipv4(host) or is_valid_hostname(host) or is_valid_mac(host)


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

