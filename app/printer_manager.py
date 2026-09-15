"""
Printer Manager for Zebra printers
Handles communication via Network (TCP/IP) and local OS-installed printers
(win32print on Windows).
"""

import concurrent.futures
from datetime import datetime
import ipaddress
import json
import logging
from pathlib import Path
import platform
import re
import socket
import ssl
import subprocess
import threading
import time
from typing import List, Dict, Tuple, Optional
import urllib.request

from .utils import (
    get_local_ip,
    is_valid_ipv4,
    is_valid_mac,
    normalize_mac,
    get_mac_for_ip,
    get_ip_for_mac,
)

# Conditional import for Windows printing
_win32print = None
if platform.system() == "Windows":
    try:
        import win32print as _win32print
    except ImportError:
        pass

logger = logging.getLogger(__name__)


def _parse_ber_length(data: bytes, offset: int) -> Tuple[int, int]:
    """
    Parse ASN.1 BER length starting at data[offset].
    Returns (length, new_offset_after_length_bytes).
    """
    if offset >= len(data):
        return 0, offset
    first = data[offset]
    if (first & 0x80) == 0:
        return first, offset + 1
    num_bytes = first & 0x7F
    if num_bytes == 0 or offset + 1 + num_bytes > len(data):
        return 0, offset + 1
    val = 0
    for b in data[offset + 1 : offset + 1 + num_bytes]:
        val = (val << 8) | b
    return val, offset + 1 + num_bytes


def _build_snmp_packet(oid_list: List[int], pdu_type: int = 0xA0) -> bytes:
    """Build SNMPv1 packet for a given OID (0xA0 = GetRequest, 0xA1 = GetNextRequest)."""
    oid_bytes = bytearray([oid_list[0] * 40 + oid_list[1]])
    for val in oid_list[2:]:
        if val < 128:
            oid_bytes.append(val)
        else:
            parts = []
            while val > 0:
                parts.append(val & 0x7F)
                val >>= 7
            for i in range(len(parts) - 1, 0, -1):
                oid_bytes.append(parts[i] | 0x80)
            oid_bytes.append(parts[0])

    comm_bytes = b"public"
    varbind = b"\x30" + bytes([len(oid_bytes) + 4]) + b"\x06" + bytes([len(oid_bytes)]) + bytes(oid_bytes) + b"\x05\x00"
    varbind_list = b"\x30" + bytes([len(varbind)]) + varbind
    pdu_payload = b"\x02\x01\x01\x02\x01\x00\x02\x01\x00" + varbind_list
    pdu = bytes([pdu_type]) + bytes([len(pdu_payload)]) + pdu_payload
    msg_payload = b"\x02\x01\x00\x04" + bytes([len(comm_bytes)]) + comm_bytes + pdu
    return b"\x30" + bytes([len(msg_payload)]) + msg_payload


def _query_snmp_raw(ip: str, oid_list: List[int], timeout: float = 0.3, pdu_type: int = 0xA0) -> Optional[bytes]:
    """Query an SNMPv1 OID on UDP port 161 and return the raw byte payload of the OCTET STRING varbind."""
    try:
        pkt = _build_snmp_packet(oid_list, pdu_type=pdu_type)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(pkt, (ip, 161))
            data, _ = s.recvfrom(2048)
            pdu_idx = data.find(b"\xa2")
            if pdu_idx != -1:
                oid_idx = data.find(b"\x06", pdu_idx)
                if oid_idx != -1:
                    oid_len, val_start = _parse_ber_length(data, oid_idx + 1)
                    val_idx = val_start + oid_len
                    if val_idx < len(data) and data[val_idx] == 0x04:  # OCTET STRING
                        val_len, content_start = _parse_ber_length(data, val_idx + 1)
                        if content_start + val_len <= len(data):
                            return data[content_start : content_start + val_len]
        finally:
            s.close()
    except Exception:
        pass
    return None


def _query_snmp_string(ip: str, oid_list: List[int], timeout: float = 0.3) -> str:
    """Query an SNMPv1/v2c OID on UDP port 161 (100% passive to port 9100, zero print bytes)."""
    raw = _query_snmp_raw(ip, oid_list, timeout=timeout, pdu_type=0xA0)
    if raw:
        return raw.decode("utf-8", errors="ignore").strip()
    return ""


def _format_mac_bytes(raw_mac: bytes) -> Optional[str]:
    """Validate and format 6 raw MAC bytes into standard uppercase 'AA:BB:CC:DD:EE:FF'."""
    if not raw_mac or len(raw_mac) != 6:
        return None
    # Discard if all zeros (00:00:00:00:00:00)
    if not any(b != 0 for b in raw_mac):
        return None
    return ":".join(f"{b:02X}" for b in raw_mac)


def _query_snmp_mac(ip: str, timeout: float = 0.3) -> Optional[str]:
    """
    Query interface physical MAC address (ifPhysAddress) via SNMP UDP port 161.
    Tries:
    1. GETNEXT on 1.3.6.1.2.1.2.2.1.6 (PDU 0xA1)
    2. GET on ifIndex 1..4 (1.3.6.1.2.1.2.2.1.6.1 .. 4)
    Returns first valid 6-byte non-zero MAC formatted as 'AA:BB:CC:DD:EE:FF', or None.
    """
    if_phys_base = [1, 3, 6, 1, 2, 1, 2, 2, 1, 6]

    # 1. Try GETNEXT on base table
    raw_next = _query_snmp_raw(ip, if_phys_base, timeout=timeout, pdu_type=0xA1)
    if raw_next:
        formatted = _format_mac_bytes(raw_next)
        if formatted:
            return formatted

    # 2. Try GET on ifIndex 1 through 4
    for idx in (1, 2, 3, 4):
        raw = _query_snmp_raw(ip, if_phys_base + [idx], timeout=timeout, pdu_type=0xA0)
        if raw:
            formatted = _format_mac_bytes(raw)
            if formatted:
                return formatted

    return None


def get_identity_key(mac: Optional[str] = None, serial: Optional[str] = None, ip: Optional[str] = None) -> str:
    """
    Generate cache identity key:
    1. Normalized MAC ('AA:BB:CC:DD:EE:FF') if present
    2. 'serial:<serial>' if serial present
    3. 'ip:<ip>' if only IP present
    """
    norm_mac = normalize_mac(mac) if mac else None
    if norm_mac:
        return norm_mac
    clean_serial = (serial or "").strip()
    if clean_serial:
        return f"serial:{clean_serial}"
    clean_ip = (ip or "").strip()
    if clean_ip:
        return f"ip:{clean_ip}"
    return ""


def probe_zebra_printer(ip: str, timeout: float = 0.6) -> Optional[Dict]:
    """
    Probe a network IP for printer availability without sending any print payload.
    - Uses passive TCP connect on port 9100 (WITHOUT sending any bytes) to test if the port is open.
    - Resolves MAC address via ARP cache.
    - Uses reverse DNS, HTTP title, and SNMP MIB queries to discover friendly name and serial number.
    - NEVER sends commands to port 9100, ensuring printers NEVER print spurious test labels.
    """
    friendly_name = ""
    hostname = ""
    port_open = False

    # 1. Passive TCP check on port 9100:
    # CRITICAL: Never send any bytes (send/sendall) to port 9100!
    # Non-Zebra or non-SGD printers treat port 9100 data as raw print jobs.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex((ip, 9100))
        s.close()
        port_open = (result == 0)
    except Exception:
        port_open = False

    if not port_open:
        return None

    # 2. Reverse DNS lookup (harmless PTR query, completely safe)
    try:
        host, _, _ = socket.gethostbyaddr(ip)
        if host:
            hostname = host.strip()
            clean_host = hostname.split(".")[0].strip()
            if clean_host:
                friendly_name = clean_host
    except Exception:
        pass

    # 3. Fallback to HTTP title on port 80 (read-only web traffic, completely safe)
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(f"http://{ip}/", headers={"User-Agent": "ZebraPrintBridge/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            html = r.read(2048).decode("utf-8", errors="ignore")
            m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
            if m:
                title = m.group(1).strip()
                clean = title.split(" - ")[0].strip()
                if clean and "setup" not in clean.lower():
                    friendly_name = clean
    except Exception:
        pass

    # 4. Resolve MAC address: SNMP ifPhysAddress > OS ARP table
    mac_address = ""
    mac_source = None

    # Priority 1: SNMP ifPhysAddress (UDP 161, works across routers & VPNs)
    snmp_mac = _query_snmp_mac(ip, timeout=0.3)
    if snmp_mac:
        mac_address = snmp_mac
        mac_source = "snmp"
    else:
        # Priority 2: OS ARP table (local subnet fallback)
        arp_mac = get_mac_for_ip(ip)
        if arp_mac:
            mac_address = arp_mac
            mac_source = "arp"

    # 5. Fallback to SNMP sysName / Zebra Enterprise MIB on UDP port 161
    unique_id = ""
    if not friendly_name:
        # Zebra enterprise friendly name: 1.3.6.1.4.1.10642.1.4.0
        snmp_name = _query_snmp_string(ip, [1, 3, 6, 1, 4, 1, 10642, 1, 4, 0], timeout=0.3)
        if snmp_name:
            friendly_name = snmp_name
        else:
            # Standard MIB-2 sysName.0: 1.3.6.1.2.1.1.5.0
            snmp_sys = _query_snmp_string(ip, [1, 3, 6, 1, 2, 1, 1, 5, 0], timeout=0.3)
            if snmp_sys:
                friendly_name = snmp_sys

    # Serial number via Zebra Enterprise MIB: 1.3.6.1.4.1.10642.1.9.0
    snmp_serial = _query_snmp_string(ip, [1, 3, 6, 1, 4, 1, 10642, 1, 9, 0], timeout=0.3)
    if snmp_serial:
        unique_id = snmp_serial

    return {
        "name": friendly_name or (hostname.split(".")[0] if hostname else f"Zebra Printer ({ip})"),
        "hostname": hostname,
        "ip": ip,
        "port": 9100,
        "mac": mac_address or None,
        "mac_address": mac_address or "",
        "serial": unique_id or None,
        "unique_id": unique_id or "",
        "mac_source": mac_source,
        "last_seen": datetime.now().isoformat(),
    }


def get_local_subnets(custom_subnets: List[str] = None) -> List[ipaddress.IPv4Network]:
    """Detect local IPv4 network subnets automatically and merge custom/VPN subnets."""
    subnets = []
    # Try ifconfig on macOS / Linux
    try:
        out = subprocess.check_output(["ifconfig"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9a-fA-F]+)", line)
            if m:
                ip = m.group(1)
                if ip.startswith("127."):
                    continue
                hex_val = int(m.group(2), 16)
                mask_str = socket.inet_ntoa(hex_val.to_bytes(4, "big"))
                net = ipaddress.IPv4Network(f"{ip}/{mask_str}", strict=False)
                if 20 <= net.prefixlen <= 30 and net not in subnets:
                    subnets.append(net)
    except Exception:
        pass

    # Fallback to local machine IP with /24
    if not subnets:
        try:
            local_ip = get_local_ip()
            if local_ip:
                subnets.append(ipaddress.IPv4Network(f"{local_ip}/24", strict=False))
        except Exception:
            pass

    # Add custom / VPN subnets (e.g. 192.168.0.0/22 for corporate VPN)
    for s in (custom_subnets or []):
        if not s:
            continue
        try:
            net = ipaddress.IPv4Network(str(s).strip(), strict=False)
            already_covered = any(net.subnet_of(existing) for existing in subnets)
            if not already_covered:
                subnets = [existing for existing in subnets if not existing.subnet_of(net)]
                subnets.append(net)
        except Exception as e:
            logger.warning("Invalid custom subnet '%s': %s", s, e)

    return subnets


class PrinterManager:
    """
    Manages communication with Zebra printers.
    Supports network (TCP/IP port 9100) and local OS-installed printers.
    Dynamically auto-discovers network printers, tracks live names, and persists them.
    """

    # Default Zebra network port
    DEFAULT_PORT = 9100

    def __init__(
        self,
        scan_network: bool = True,
        network_timeout: float = 0.5,
        saved_printers: List[Dict] = None,
        printer_aliases: Dict[str, str] = None,
        custom_subnets: List[str] = None,
    ):
        self.scan_network = scan_network
        self.network_timeout = network_timeout
        self.saved_printers = saved_printers or []
        self.printer_aliases = {k.lower(): v for k, v in (printer_aliases or {}).items()}
        self.custom_subnets = custom_subnets or ["192.168.0.0/22"]

        # Persistent printer cache directory and file
        self.cache_dir = Path.home() / ".config" / "zebra-print-bridge"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "network_printers.json"

        self._lock = threading.Lock()
        self._network_printers: Dict[str, Dict] = {}  # ip -> printer info dict
        self._alias_map: Dict[str, str] = {}          # lowercase alias -> ip

        # Load persisted cache immediately on startup
        self._load_cache()

        # Start continuous background discovery and sync thread (completely passive/safe)
        self._scanner_running = False
        self._scanner_thread = None
        if self.scan_network:
            self._scanner_running = True
            self._scanner_thread = threading.Thread(target=self._background_scanner_loop, daemon=True)
            self._scanner_thread.start()

    # ── Sending ──────────────────────────────────────────────────────

    def send_zpl(self, printer: Dict, zpl: str) -> Tuple[bool, Optional[str]]:
        """
        Send ZPL/raw commands to a printer.
        Returns (success, error_message).
        """
        if not printer:
            return False, "No printer specified"

        printer_type = printer.get('type', '')
        logger.info(
            "send_zpl printer_type=%s target=%s raw_bytes=%s",
            printer_type,
            printer.get('address') or printer.get('name'),
            len(zpl or ''),
        )

        if printer_type == 'test':
            return self._send_test(printer, zpl)
        elif printer_type == 'network':
            return self._send_network(printer, zpl)
        elif printer_type == 'local':
            return self._send_local(printer, zpl)
        else:
            return False, f"Unknown printer type: {printer_type}"

    def _send_test(self, printer: Dict, zpl: str) -> Tuple[bool, Optional[str]]:
        """Simulate sending ZPL to a test printer."""
        logger.info("[TEST PRINTER] Processing simulated print job:")
        logger.info("  - Target: %s", printer.get('name', 'Test Printer'))
        logger.info("  - ZPL Length: %d bytes", len(zpl))
        logger.info("  - ZPL Preview: %s...", zpl[:100])
        return True, None

    def _load_cache(self):
        """Load persistent discovered printers from disk with automatic v1 to v2 migration."""
        if not self.cache_file.exists():
            return

        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            printers = {}
            needs_save = False

            if data.get("version") == 2 and isinstance(data.get("printers"), dict):
                for k, p in data["printers"].items():
                    mac = normalize_mac(p.get("mac") or p.get("mac_address"))
                    serial = (p.get("serial") or p.get("unique_id") or "").strip() or None
                    ip = p.get("ip")
                    key = get_identity_key(mac, serial, ip) or k
                    printers[key] = {
                        "mac": mac,
                        "serial": serial,
                        "ip": ip,
                        "port": int(p.get("port", self.DEFAULT_PORT)),
                        "name": p.get("name", ""),
                        "hostname": p.get("hostname", ""),
                        "last_seen": p.get("last_seen") or datetime.now().isoformat(),
                        "mac_source": p.get("mac_source"),
                        "mac_address": mac or "",
                        "unique_id": serial or "",
                    }
            else:
                # Automatic migration from v1 (unversioned, keyed by IP or list)
                raw_printers = data.get("printers", {})
                if isinstance(raw_printers, list):
                    items = raw_printers
                elif isinstance(raw_printers, dict):
                    items = list(raw_printers.values())
                else:
                    items = []

                for p in items:
                    mac = normalize_mac(p.get("mac_address") or p.get("mac"))
                    serial = (p.get("unique_id") or p.get("serial") or "").strip() or None
                    ip = p.get("ip")
                    key = get_identity_key(mac, serial, ip)
                    if not key:
                        continue
                    printers[key] = {
                        "mac": mac,
                        "serial": serial,
                        "ip": ip,
                        "port": int(p.get("port", self.DEFAULT_PORT)),
                        "name": p.get("name", ""),
                        "hostname": p.get("hostname", ""),
                        "last_seen": p.get("last_seen") or datetime.now().isoformat(),
                        "mac_source": "arp" if mac else None,
                        "mac_address": mac or "",
                        "unique_id": serial or "",
                    }
                needs_save = True
                logger.info("Migrated %d printers from cache v1 to v2 identity format", len(printers))

            with self._lock:
                self._network_printers = printers
                self._rebuild_alias_map_locked()

            if needs_save and printers:
                self._save_cache()

            logger.info("Loaded %d persistent network printers from %s (v2)", len(printers), self.cache_file)
        except Exception as e:
            logger.warning("Failed to load network printers cache: %s", e)

    def _save_cache(self):
        """Save discovered printers to disk persistently in v2 format."""
        try:
            with self._lock:
                data = {
                    "version": 2,
                    "updated_at": datetime.now().isoformat(),
                    "printers": dict(self._network_printers),
                }
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.debug("Saved network printers cache (v2) to %s", self.cache_file)
        except Exception as e:
            logger.warning("Failed to save network printers cache: %s", e)

    def _update_or_add_printer_locked(self, p: Dict) -> Tuple[bool, str]:
        """
        Add or update a printer entry in self._network_printers indexed by identity.
        Returns (changed: bool, identity_key: str).
        - Never creates duplicates for the same MAC or serial.
        - If another device held the new IP, its IP is cleared.
        """
        mac = normalize_mac(p.get("mac") or p.get("mac_address"))
        serial = (p.get("serial") or p.get("unique_id") or "").strip() or None
        ip = p.get("ip")
        port = int(p.get("port", self.DEFAULT_PORT))
        name = (p.get("name") or "").strip()
        hostname = (p.get("hostname") or "").strip()
        last_seen = p.get("last_seen") or datetime.now().isoformat()
        mac_source = p.get("mac_source")

        # 1. Look for existing entry matching MAC, then serial, then IP-only key
        matched_key = None
        if mac and mac in self._network_printers:
            matched_key = mac
        elif serial:
            for k, existing in self._network_printers.items():
                if existing.get("serial") and existing["serial"] == serial:
                    matched_key = k
                    break
        if not matched_key and ip:
            ip_key = f"ip:{ip}"
            if ip_key in self._network_printers:
                matched_key = ip_key

        # 2. Detect IP conflict: another entry currently has this IP
        if ip:
            stale_keys = []
            for k, existing in list(self._network_printers.items()):
                if k != matched_key and existing.get("ip") == ip:
                    logger.warning(
                        "IP %s was reassigned: moving from device '%s' to '%s'",
                        ip, k, mac or (f"serial:{serial}" if serial else ip)
                    )
                    if k.startswith("ip:"):
                        stale_keys.append(k)
                    else:
                        existing["ip"] = None
            for k in stale_keys:
                del self._network_printers[k]

        # 3. Determine canonical identity key for this device
        new_key = get_identity_key(mac, serial, ip)
        if not new_key:
            return False, ""

        # 4. If matched under an older/weaker key (e.g. was serial:123 or ip:1.2.3.4, and now we have MAC)
        existing_entry = self._network_printers.get(matched_key) if matched_key else None
        if matched_key and matched_key != new_key:
            del self._network_printers[matched_key]
            if existing_entry:
                if not name and existing_entry.get("name"):
                    name = existing_entry["name"]
                if not hostname and existing_entry.get("hostname"):
                    hostname = existing_entry["hostname"]
                if not serial and existing_entry.get("serial"):
                    serial = existing_entry["serial"]

        entry = {
            "mac": mac or (existing_entry.get("mac") if existing_entry else None),
            "serial": serial or (existing_entry.get("serial") if existing_entry else None),
            "ip": ip,
            "port": port,
            "name": name or (existing_entry.get("name") if existing_entry else "") or (f"Zebra Printer ({ip})" if ip else "Zebra Printer"),
            "hostname": hostname or (existing_entry.get("hostname") if existing_entry else ""),
            "last_seen": last_seen,
            "mac_source": mac_source or (existing_entry.get("mac_source") if existing_entry else None),
            "mac_address": mac or (existing_entry.get("mac_address") if existing_entry else "") or "",
            "unique_id": serial or (existing_entry.get("unique_id") if existing_entry else "") or "",
        }

        changed = True
        if existing_entry:
            if (existing_entry.get("ip") == entry["ip"] and
                existing_entry.get("name") == entry["name"] and
                existing_entry.get("serial") == entry["serial"] and
                existing_entry.get("mac") == entry["mac"] and
                matched_key == new_key):
                changed = False

        self._network_printers[new_key] = entry
        return changed, new_key

    def _rebuild_alias_map_locked(self):
        """Rebuild fast alias lookup map from discovered printers and saved_printers."""
        mapping = {}

        # 1. Discovered printers (only entries with active IP)
        for key, p in self._network_printers.items():
            ip = p.get("ip")
            if not ip:
                continue

            name = (p.get("name") or "").strip()
            host = (p.get("hostname") or "").strip()
            serial = (p.get("serial") or p.get("unique_id") or "").strip()
            mac = (p.get("mac") or p.get("mac_address") or "").strip()

            if name:
                name_lower = name.lower()
                mapping[name_lower] = ip
                if name_lower.startswith("nh-"):
                    clean = name_lower[3:]
                    if clean not in mapping:
                        mapping[clean] = ip
                else:
                    prefixed = f"nh-{name_lower}"
                    if prefixed not in mapping:
                        mapping[prefixed] = ip

            if host:
                host_lower = host.lower()
                mapping[host_lower] = ip
                clean_host = host_lower.split(".")[0]
                if clean_host not in mapping:
                    mapping[clean_host] = ip

            if serial:
                mapping[serial.lower()] = ip
                mapping[f"serial:{serial.lower()}"] = ip

            if mac:
                norm_mac = normalize_mac(mac)
                if norm_mac:
                    norm_lower = norm_mac.lower()
                    mapping[norm_lower] = ip
                    mapping[norm_lower.replace(":", "")] = ip
                    mapping[norm_lower.replace(":", "-")] = ip

        # 2. User-defined saved_printers from config
        for p in self.saved_printers:
            name = p.get("name", "").strip()
            ip = p.get("ip", "").strip()
            if name and ip:
                mapping[name.lower()] = ip

        # 3. User-defined aliases from config
        for k, v in self.printer_aliases.items():
            mapping[k.lower()] = v

        self._alias_map = mapping

    def clear_cache(self):
        """Clear discovered network printers cache in memory and delete the cache file on disk."""
        with self._lock:
            self._network_printers.clear()
            self._rebuild_alias_map_locked()
        try:
            if self.cache_file.exists():
                self.cache_file.unlink()
                logger.info("Deleted network printer cache file: %s", self.cache_file)
        except Exception as e:
            logger.warning("Failed to delete network printer cache file: %s", e)

    def refresh_known_printers(self, prune_unreachable: bool = True) -> bool:
        """
        Quickly poll all currently known printer IPs (<100ms) to detect renames,
        updates, and detect if an IP moved to another device or is offline.
        Returns True if any changes occurred.
        """
        known_ips = set()
        with self._lock:
            for p in self._network_printers.values():
                if p.get("ip"):
                    known_ips.add(p["ip"])
            for p in self.saved_printers:
                if p.get("ip"):
                    known_ips.add(p["ip"])

        if not known_ips:
            return False

        changed = False
        unreachable_ips = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(known_ips) or 1) as ex:
            futures = {ex.submit(probe_zebra_printer, ip, 0.6): ip for ip in known_ips}
            for fut in concurrent.futures.as_completed(futures):
                ip = futures[fut]
                try:
                    res = fut.result()
                    if res:
                        with self._lock:
                            entry_changed, _ = self._update_or_add_printer_locked(res)
                            if entry_changed:
                                changed = True
                    else:
                        unreachable_ips.add(ip)
                except Exception:
                    unreachable_ips.add(ip)

        if prune_unreachable and unreachable_ips:
            with self._lock:
                for ip in unreachable_ips:
                    stale_keys = []
                    for k, p in self._network_printers.items():
                        if p.get("ip") == ip:
                            if k.startswith("ip:"):
                                stale_keys.append(k)
                            else:
                                p["ip"] = None
                            changed = True
                    for k in stale_keys:
                        del self._network_printers[k]

        if changed:
            with self._lock:
                self._rebuild_alias_map_locked()
            if self._network_printers:
                self._save_cache()
            elif self.cache_file.exists():
                try:
                    self.cache_file.unlink()
                except Exception:
                    pass
            logger.info("Updated printer aliases from network: %s", {p.get("name", "Zebra"): p.get("ip") for p in self._network_printers.values() if p.get("ip")})

        return changed

    def scan_subnet(self, clear_cache: bool = False) -> Dict[str, Dict]:
        """Scan all local subnets for port 9100 Zebra printers concurrently."""
        if clear_cache:
            self.clear_cache()

        subnets = get_local_subnets(self.custom_subnets)
        if not subnets:
            return {}

        ips_to_scan = []
        for net in subnets:
            for host in net.hosts():
                ips_to_scan.append(str(host))

        logger.info("Scanning %d IPs across subnets %s for Zebra printers...", len(ips_to_scan), [str(s) for s in subnets])

        discovered = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=100) as ex:
            futures = {ex.submit(probe_zebra_printer, ip, 0.4): ip for ip in ips_to_scan}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    res = fut.result()
                    if res:
                        discovered[res["ip"]] = res
                except Exception:
                    pass

        with self._lock:
            if clear_cache:
                self._network_printers.clear()
            for p in discovered.values():
                self._update_or_add_printer_locked(p)
            self._rebuild_alias_map_locked()

        if self._network_printers:
            self._save_cache()
            logger.info("Discovered %d Zebra printers on network: %s", len(discovered), [p.get("name") for p in discovered.values()])
        elif clear_cache and self.cache_file.exists():
            try:
                self.cache_file.unlink()
            except Exception:
                pass

        return discovered

    def stop(self):
        """Stop any background scanner threads cleanly."""
        self._scanner_running = False
        if self._scanner_thread and self._scanner_thread.is_alive():
            try:
                self._scanner_thread.join(timeout=1.0)
            except Exception:
                pass

    def _background_scanner_loop(self):
        """Background thread keeping printer names and IPs synced in real time (100% passive & harmless)."""
        time.sleep(1.0)
        if not self._scanner_running:
            return

        has_known = False
        with self._lock:
            has_known = bool(self._network_printers)

        if has_known:
            self.refresh_known_printers()
        else:
            self.scan_subnet()

        iteration = 0
        while self._scanner_running:
            time.sleep(30.0)  # poll every 30 seconds
            iteration += 1
            if not self._scanner_running:
                break
            try:
                # Fast poll of known IPs (takes 50ms, 100% passive TCP check on port 9100)
                self.refresh_known_printers()

                # Subnet scan every 15 minutes (30 iterations * 30s)
                if iteration % 30 == 0:
                    self.scan_subnet()
            except Exception as e:
                logger.debug("Background scanner iteration error: %s", e)

    def resolve_network_address(self, address: str) -> str:
        """
        Resolve a network printer name/hostname to IP address.
        Zero hardcoding: checks persistent dynamic cache, standard DNS, fast poll, and subnet scan.
        All network probes are 100% passive and NEVER send bytes to port 9100.
        """
        if not address:
            return address

        clean_addr = address.strip()
        if is_valid_ipv4(clean_addr):
            return clean_addr

        # Special handling for MAC address targeting (e.g. '00:07:4D:6F:C2:14' or '00074d6fc214')
        if is_valid_mac(clean_addr):
            norm_mac = normalize_mac(clean_addr)
            if norm_mac:
                # 1. Check in-memory alias map
                with self._lock:
                    if norm_mac.lower() in self._alias_map:
                        resolved = self._alias_map[norm_mac.lower()]
                        logger.info("Resolved printer MAC '%s' -> %s (dynamic cache)", norm_mac, resolved)
                        return resolved

                # 2. Check OS ARP table directly (instant, safe)
                arp_ip = get_ip_for_mac(norm_mac)
                if arp_ip:
                    logger.info("Resolved printer MAC '%s' -> %s (OS ARP table)", norm_mac, arp_ip)
                    return arp_ip

                # 3. Refresh known printers if scan_network is enabled
                if self.scan_network:
                    self.refresh_known_printers()
                    arp_ip = get_ip_for_mac(norm_mac)
                    if arp_ip:
                        return arp_ip
                    with self._lock:
                        if norm_mac.lower() in self._alias_map:
                            return self._alias_map[norm_mac.lower()]

        clean_lower = clean_addr.lower()

        # 1. In-memory lookup from discovered & saved printers / aliases
        with self._lock:
            if clean_lower in self._alias_map:
                resolved = self._alias_map[clean_lower]
                logger.info("Resolved printer alias '%s' -> %s (dynamic cache)", clean_addr, resolved)
                return resolved

        # 2. Standard DNS resolution
        try:
            socket.getaddrinfo(clean_addr, None)
            return clean_addr
        except socket.gaierror:
            pass

        # 3. Try mDNS suffixes (.local, .localdomain)
        if not clean_addr.endswith(".local") and not clean_addr.endswith(".localdomain"):
            for suffix in (".local", ".localdomain"):
                candidate = f"{clean_addr}{suffix}"
                try:
                    socket.getaddrinfo(candidate, None)
                    logger.info("Resolved printer hostname '%s' -> '%s' via DNS search", clean_addr, candidate)
                    return candidate
                except socket.gaierror:
                    pass

        # 4. If still not found and scan_network is enabled, check network printers safely
        if self.scan_network:
            logger.info("Printer alias '%s' not in cache. Refreshing known network printers...", clean_addr)
            self.refresh_known_printers()
            with self._lock:
                if clean_lower in self._alias_map:
                    resolved = self._alias_map[clean_lower]
                    logger.info("Resolved printer alias '%s' -> %s after fast poll", clean_addr, resolved)
                    return resolved

            logger.info("Printer alias '%s' still unknown. Scanning local subnet (passive)...", clean_addr)
            self.scan_subnet()
            with self._lock:
                if clean_lower in self._alias_map:
                    resolved = self._alias_map[clean_lower]
                    logger.info("Resolved printer alias '%s' -> %s after subnet scan", clean_addr, resolved)
                    return resolved

        # Return original address (let socket connection report specific connectivity or gaierror)
        return clean_addr

    def _send_network(self, printer: Dict, zpl: str) -> Tuple[bool, Optional[str]]:
        """Send ZPL to a network printer via TCP socket."""
        address = printer.get('address')
        port = printer.get('port', self.DEFAULT_PORT)
        resolved_address = self.resolve_network_address(address) if address else address
        logger.info(
            "send_network target=%s (resolved=%s):%s raw_bytes=%s",
            address,
            resolved_address,
            port,
            len(zpl or ''),
        )

        if not resolved_address:
            return False, "No printer address"

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.connect((resolved_address, port))
            sock.send(zpl.encode('utf-8'))
            logger.info("ZPL payload successfully sent to network printer %s:%s.", resolved_address, port)
            return True, None
        except socket.gaierror as e:
            logger.error("Cannot resolve hostname '%s': %s", address, e)
            return False, f"Cannot resolve hostname '{address}': {e}"
        except socket.timeout:
            return False, "Connection timeout"
        except socket.error as e:
            return False, f"Socket error: {e}"
        except Exception as e:
            return False, f"Error: {e}"
        finally:
            sock.close()

    def _send_local(self, printer: Dict, raw: str) -> Tuple[bool, Optional[str]]:
        """Send raw data to a local OS printer (Windows via win32print, macOS/Linux via CUPS)."""
        if platform.system() == "Windows":
            return self._send_win32(printer, raw)
        elif platform.system() in ("Darwin", "Linux"):
            return self._send_cups(printer, raw)
        return False, f"Local printing not supported on {platform.system()}"

    @staticmethod
    def _send_cups(printer: Dict, raw: str) -> Tuple[bool, Optional[str]]:
        """Send raw data to a local/USB printer on macOS or Linux via CUPS (lp -o raw)."""
        printer_name = printer.get("name", "")
        logger.info("send_cups printer_name=%s raw_bytes=%s", printer_name, len(raw or ""))

        if not printer_name:
            return False, "No printer name"

        try:
            proc = subprocess.run(
                ["lp", "-d", printer_name, "-o", "raw"],
                input=raw.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
            if proc.returncode == 0:
                logger.info("Raw payload successfully sent to CUPS printer: '%s'.", printer_name)
                return True, None
            else:
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                logger.error("Failed to send payload to CUPS printer '%s': %s", printer_name, err)
                return False, f"CUPS print error: {err}"
        except Exception as e:
            logger.error("Exception sending payload to CUPS printer '%s': %s", printer_name, e)
            return False, f"CUPS print error: {e}"

    @staticmethod
    def _send_win32(printer: Dict, raw: str) -> Tuple[bool, Optional[str]]:
        """Send raw data to a Windows printer via win32print API."""
        if _win32print is None:
            return False, "win32print not available — pywin32 is not installed"

        printer_name = printer.get("name", "")
        logger.info("send_win32 printer_name=%s raw_bytes=%s", printer_name, len(raw or ""))

        if not printer_name:
            return False, "No printer name"

        try:
            hPrinter = _win32print.OpenPrinter(printer_name)
            try:
                _win32print.StartDocPrinter(hPrinter, 1, ("ZPL Label", None, "RAW"))
                _win32print.StartPagePrinter(hPrinter)
                _win32print.WritePrinter(hPrinter, raw.encode("utf-8"))
                _win32print.EndPagePrinter(hPrinter)
                _win32print.EndDocPrinter(hPrinter)
            finally:
                _win32print.ClosePrinter(hPrinter)

            logger.info("Raw payload successfully sent to local printer: '%s'.", printer_name)
            return True, None

        except Exception as e:
            logger.error("Failed to send payload to local printer '%s'. Reason: %s", printer_name, e)
            return False, f"Win32 print error: {e}"

    # ── Connection testing ───────────────────────────────────────────

    def _test_network_connection(self, address: str, port: int) -> Tuple[bool, str]:
        """Test TCP connection to a network printer by IP or hostname with descriptive error reporting."""
        if not address:
            return False, "No printer address"

        resolved_address = self.resolve_network_address(address)
        timeout = max(self.network_timeout, 1.5)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                result = sock.connect_ex((resolved_address, port))
                display = f"{address} ({resolved_address}:{port})" if resolved_address != address else f"{address}:{port}"
                if result == 0:
                    return True, f"Connected to {display}"
                return False, f"Cannot connect to printer at {display}"
            finally:
                sock.close()
        except socket.gaierror as e:
            return False, f"Cannot resolve hostname '{address}': {e}"
        except socket.timeout:
            return False, f"Connection timeout reaching {address}:{port}"
        except Exception as e:
            return False, f"Error connecting to {address}:{port}: {e}"

    def test_connection(self, printer: Dict) -> Tuple[bool, str]:
        """Test connection to a printer."""
        logger.info(
            "test_connection printer_type=%s target=%s",
            printer.get('type'),
            printer.get('address') or printer.get('name'),
        )
        if printer.get('type') == 'test':
            return True, "Test printer ready (simulated)"

        elif printer.get('type') == 'network':
            address = printer.get('address')
            port = printer.get('port', self.DEFAULT_PORT)
            return self._test_network_connection(address, port)

        return False, "Unknown printer type"

    def test_local_connection(self, name: str) -> Tuple[bool, str]:
        """Test whether a local printer exists and is available."""
        printer = self.find_local_printer(name)
        if not printer:
            return False, f"Printer '{name}' not found in OS"

        status = printer.get("status", "unknown")
        if status in ("ready", "available", "idle"):
            return True, f"Printer '{printer['name']}' is {status}"
        if status == "paused":
            return False, f"Printer '{printer['name']}' is paused"
        if status in ("error", "offline"):
            return False, f"Printer '{printer['name']}' is {status}"

        # Unknown status — still exists, so cautiously say OK
        return True, f"Printer '{printer['name']}' found (status: {status})"

    # ── Network printer discovery methods ─────────────────────────────

    def list_network_printers(self) -> List[Dict]:
        """List all dynamically discovered Zebra network printers."""
        with self._lock:
            printers = []
            for p in self._network_printers.values():
                ip = p.get("ip")
                mac = p.get("mac") or p.get("mac_address", "")
                serial = p.get("serial") or p.get("unique_id", "")
                printers.append({
                    "name": p.get("name", "Zebra Network Printer"),
                    "type": "network",
                    "hostname": p.get("hostname", ""),
                    "address": ip,
                    "ip": ip,
                    "port": p.get("port", self.DEFAULT_PORT),
                    "status": "ready" if ip else "offline",
                    "unique_id": serial,
                    "serial": serial,
                    "mac_address": mac,
                    "mac": mac,
                    "mac_source": p.get("mac_source"),
                    "last_seen": p.get("last_seen"),
                })
            # Sort by name
            printers.sort(key=lambda x: x["name"])
            return printers

    # ── Local / OS-installed printer methods ──────────────────────────

    def get_default_printer_name(self) -> Optional[str]:
        """Get the name of the default printer configured in the OS."""
        if platform.system() == "Windows" and _win32print is not None:
            try:
                name = _win32print.GetDefaultPrinter()
                if name:
                    return name.strip()
            except Exception as e:
                logger.debug("Failed to get Windows default printer name: %s", e)
        elif platform.system() in ("Darwin", "Linux"):
            try:
                proc = subprocess.run(
                    ["lpstat", "-d"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    match = re.search(r"system default destination:\s*(.+)", proc.stdout)
                    if match:
                        return match.group(1).strip()
            except Exception as e:
                logger.debug("Failed to get CUPS default printer: %s", e)
        return None

    def get_default_local_printer(self) -> Optional[Dict]:
        """Get the default printer configured in the OS as a printer dict."""
        default_name = self.get_default_printer_name()
        if default_name:
            printer = self.find_local_printer(default_name)
            if printer:
                printer_copy = dict(printer)
                printer_copy["is_default"] = True
                return printer_copy
            return {
                "name": default_name,
                "type": "local",
                "driver": "",
                "port": "",
                "status": "ready",
                "status_code": 0,
                "comment": "Default OS Printer",
                "location": "",
                "is_default": True,
            }

        # Fallback if no explicit default returned: pick first local printer if available
        printers = self.list_local_printers()
        if printers:
            printer_copy = dict(printers[0])
            printer_copy["is_default"] = True
            return printer_copy
        return None

    def list_local_printers(self) -> List[Dict]:
        """List printers installed in the OS (Windows via win32print, macOS/Linux via CUPS)."""
        if platform.system() == "Windows":
            printers = self._list_win32_printers()
        elif platform.system() in ("Darwin", "Linux"):
            printers = self._list_cups_printers()
        else:
            printers = []

        default_name = self.get_default_printer_name()
        default_lower = default_name.lower() if default_name else None

        for p in printers:
            p["is_default"] = (p["name"].lower() == default_lower) if default_lower else False

        return printers

    @staticmethod
    def _list_cups_printers() -> List[Dict]:
        """List printers registered in CUPS (macOS / Linux)."""
        printers_map = {}
        try:
            # Parse printer statuses from lpstat -p
            p_proc = subprocess.run(
                ["lpstat", "-p"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            if p_proc.returncode == 0:
                for line in p_proc.stdout.splitlines():
                    m = re.match(r"^printer\s+(\S+)\s+(.+)", line.strip())
                    if m:
                        name = m.group(1)
                        state_text = m.group(2).lower()
                        if "idle" in state_text:
                            status = "ready"
                        elif "paused" in state_text:
                            status = "paused"
                        elif "disabled" in state_text:
                            status = "offline"
                        else:
                            status = "available"
                        printers_map[name] = {
                            "name": name,
                            "type": "local",
                            "driver": "CUPS",
                            "port": "CUPS",
                            "status": status,
                            "status_code": 0,
                            "comment": line.strip(),
                            "location": "",
                        }

            # Supplement with available printer destinations from lpstat -e
            e_proc = subprocess.run(
                ["lpstat", "-e"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            if e_proc.returncode == 0:
                for dest in e_proc.stdout.splitlines():
                    dest = dest.strip()
                    if dest and dest not in printers_map:
                        printers_map[dest] = {
                            "name": dest,
                            "type": "local",
                            "driver": "CUPS",
                            "port": "CUPS",
                            "status": "available",
                            "status_code": 0,
                            "comment": "CUPS Destination",
                            "location": "",
                        }
        except Exception as e:
            logger.error("Failed to list CUPS printers. Reason: %s", e)

        return list(printers_map.values())

    @staticmethod
    def _list_win32_printers() -> List[Dict]:
        """List printers registered in Windows via win32print."""
        if _win32print is None:
            logger.warning("Local printing disabled: 'win32print' module is unavailable.")
            return []

        printers = []
        try:
            flags = _win32print.PRINTER_ENUM_LOCAL | _win32print.PRINTER_ENUM_CONNECTIONS
            raw_list = _win32print.EnumPrinters(flags, None, 2)
            for info in raw_list:
                name = info["pPrinterName"]
                # Status bitmask: 0 means ready
                status_code = info.get("Status", 0)
                if status_code == 0:
                    status = "ready"
                elif status_code & 0x00000001:  # PRINTER_STATUS_PAUSED
                    status = "paused"
                elif status_code & 0x00000002:  # PRINTER_STATUS_ERROR
                    status = "error"
                elif status_code & 0x00000004:  # pending deletion
                    status = "pending_deletion"
                elif status_code & 0x00000400:  # offline
                    status = "offline"
                else:
                    status = "available"

                printers.append({
                    "name": name,
                    "type": "local",
                    "driver": info.get("pDriverName", ""),
                    "port": info.get("pPortName", ""),
                    "status": status,
                    "status_code": status_code,
                    "comment": info.get("pComment", ""),
                    "location": info.get("pLocation", ""),
                })
        except Exception as e:
            logger.error("Failed to list local printers. Reason: %s", e)

        return printers

    def find_local_printer(self, name: str) -> Optional[Dict]:
        """Find a local printer by exact or partial name match (case-insensitive)."""
        if not name:
            return None

        name_lower = name.strip().lower()
        printers = self.list_local_printers()

        # Exact match first
        for p in printers:
            if p["name"].lower() == name_lower:
                return p

        # Partial / substring match
        for p in printers:
            if name_lower in p["name"].lower():
                return p

        return None


