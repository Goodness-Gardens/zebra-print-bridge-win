"""
Printer Manager for Zebra printers
Handles communication via Network (TCP/IP) and local OS-installed printers
(win32print on Windows).
"""

import concurrent.futures
from collections import deque
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

from .utils import get_local_ip, is_valid_ipv4

# Conditional import for Windows printing
_win32print = None
if platform.system() == "Windows":
    try:
        import win32print as _win32print
    except ImportError:
        pass

logger = logging.getLogger(__name__)


def probe_zebra_printer(ip: str, timeout: float = 0.6) -> Optional[Dict]:
    """
    Probe a network IP for printer availability without sending any print payload.
    - Uses passive TCP connect on port 9100 (WITHOUT sending any bytes) to test if the port is open.
    - Uses reverse DNS and HTTP port 80 title check to discover the printer's friendly name/model.
    - NEVER sends commands to port 9100, ensuring printers NEVER print spurious test labels.
    """
    friendly_name = ""
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
            clean_host = host.split(".")[0].strip()
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

    return {
        "name": friendly_name or f"Zebra Printer ({ip})",
        "ip": ip,
        "port": 9100,
        "unique_id": "",
        "last_seen": datetime.now().isoformat(),
    }


def get_local_subnets() -> List[ipaddress.IPv4Network]:
    """Detect local IPv4 network subnets automatically."""
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
                if net.prefixlen >= 20 and net not in subnets:
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

    return subnets


class PrinterManager:
    """
    Manages communication with Zebra printers.
    Supports network (TCP/IP port 9100) and local OS-installed printers.
    Dynamically auto-discovers network printers, tracks live names, and persists them.
    """

    # Default Zebra network port
    DEFAULT_PORT = 9100

    # Test printer for development/debugging
    TEST_PRINTER = {
        'name': 'Zebra ZD420 (Test)',
        'type': 'test',
        'address': 'localhost',
        'port': 9100,
        'status': 'available',
        'description': 'Simulated printer for testing'
    }

    def __init__(
        self,
        include_test_printer: bool = True,
        scan_network: bool = True,
        scan_usb: bool = False,
        saved_printers: List[Dict] = None,
        printer_aliases: Dict[str, str] = None,
    ):
        self.include_test_printer = include_test_printer
        self.scan_network = scan_network
        self.saved_printers = saved_printers or []
        self.printer_aliases = {k.lower(): v for k, v in (printer_aliases or {}).items()}
        self.test_print_log: deque = deque(maxlen=100)
        self.network_timeout = 0.5

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
        from datetime import datetime

        print_job = {
            'timestamp': datetime.now().isoformat(),
            'printer': printer.get('name', 'Test Printer'),
            'zpl': zpl,
            'zpl_length': len(zpl)
        }

        self.test_print_log.append(print_job)
        logger.info("[TEST PRINTER] Processing simulated print job:")
        logger.info("  - ZPL Length: %d bytes", len(zpl))
        logger.info("  - ZPL Preview: %s...", zpl[:100])

        return True, None

    def _load_cache(self):
        """Load persistent discovered printers from disk."""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r") as f:
                    data = json.load(f)
                printers = data.get("printers", {})
                if isinstance(printers, list):
                    printers = {p["ip"]: p for p in printers if "ip" in p}
                with self._lock:
                    self._network_printers = printers
                    self._rebuild_alias_map_locked()
                logger.info("Loaded %d persistent network printers from %s", len(printers), self.cache_file)
            except Exception as e:
                logger.warning("Failed to load network printers cache: %s", e)

    def _save_cache(self):
        """Save discovered printers to disk persistently."""
        try:
            with self._lock:
                data = {
                    "updated_at": datetime.now().isoformat(),
                    "printers": dict(self._network_printers)
                }
            with open(self.cache_file, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug("Saved network printers cache to %s", self.cache_file)
        except Exception as e:
            logger.warning("Failed to save network printers cache: %s", e)

    def _rebuild_alias_map_locked(self):
        """Rebuild fast alias lookup map from discovered printers and saved_printers."""
        mapping = {}

        # 1. Discovered printers
        for ip, p in self._network_printers.items():
            name = p.get("name", "").strip()
            uid = p.get("unique_id", "").strip()
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
            if uid:
                mapping[uid.lower()] = ip

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

    def refresh_known_printers(self) -> bool:
        """
        Quickly poll all currently known printer IPs (<100ms) to detect renames or offline status.
        Returns True if any changes occurred.
        """
        known_ips = set()
        with self._lock:
            known_ips.update(self._network_printers.keys())
            for p in self.saved_printers:
                if p.get("ip"):
                    known_ips.add(p["ip"])

        if not known_ips:
            return False

        changed = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(known_ips) or 1) as ex:
            futures = {ex.submit(probe_zebra_printer, ip, 0.6): ip for ip in known_ips}
            for fut in concurrent.futures.as_completed(futures):
                ip = futures[fut]
                try:
                    res = fut.result()
                    if res:
                        with self._lock:
                            existing = self._network_printers.get(ip)
                            if not existing or existing.get("name") != res.get("name") or existing.get("unique_id") != res.get("unique_id"):
                                self._network_printers[ip] = res
                                changed = True
                except Exception:
                    pass

        if changed:
            with self._lock:
                self._rebuild_alias_map_locked()
            self._save_cache()
            logger.info("Updated printer aliases from network: %s", {p["name"]: p["ip"] for p in self._network_printers.values()})

        return changed

    def scan_subnet(self) -> Dict[str, Dict]:
        """Scan all local subnets for port 9100 Zebra printers concurrently."""
        subnets = get_local_subnets()
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

        if discovered:
            with self._lock:
                self._network_printers.update(discovered)
                self._rebuild_alias_map_locked()
            self._save_cache()
            logger.info("Discovered %d Zebra printers on network: %s", len(discovered), [p["name"] for p in discovered.values()])

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
        from .utils import is_valid_ipv4
        if is_valid_ipv4(clean_addr):
            return clean_addr

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

    def _check_port_open(self, ip: str, port: int) -> bool:
        """Quick check if port is open on given IP or hostname."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                timeout = max(self.network_timeout, 1.5)
                sock.settimeout(timeout)
                result = sock.connect_ex((ip, port))
                return result == 0
            finally:
                sock.close()
        except Exception:
            return False

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
                printers.append({
                    "name": p.get("name", "Zebra Network Printer"),
                    "type": "network",
                    "address": p.get("ip"),
                    "port": p.get("port", self.DEFAULT_PORT),
                    "status": "ready",
                    "unique_id": p.get("unique_id", ""),
                })
            # Sort by name
            printers.sort(key=lambda x: x["name"])
            return printers

    # ── Local / OS-installed printer methods ──────────────────────────

    def list_local_printers(self) -> List[Dict]:
        """List printers installed in the OS (Windows via win32print, macOS/Linux via CUPS)."""
        if platform.system() == "Windows":
            return self._list_win32_printers()
        elif platform.system() in ("Darwin", "Linux"):
            return self._list_cups_printers()
        return []

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

    # ── Test log helpers ─────────────────────────────────────────────

    def get_test_print_log(self) -> List[Dict]:
        """Get the log of simulated test prints."""
        return list(self.test_print_log)

    def clear_test_print_log(self):
        """Clear the test print log."""
        self.test_print_log.clear()
