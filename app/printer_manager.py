"""
Printer Manager for Zebra printers
Handles communication via Network (TCP/IP) and local OS-installed printers
(win32print on Windows).
"""

import platform
import socket
import re
from collections import deque
from typing import List, Dict, Tuple, Optional
import logging

from .utils import get_local_ip

# Conditional import for Windows printing
_win32print = None
if platform.system() == "Windows":
    try:
        import win32print as _win32print
    except ImportError:
        pass

logger = logging.getLogger(__name__)


class PrinterManager:
    """
    Manages communication with Zebra printers.
    Supports network (TCP/IP port 9100) and local OS-installed printers.
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
        scan_network: bool = False,
        scan_usb: bool = False,
        saved_printers: List[Dict] = None,
    ):
        self.include_test_printer = include_test_printer
        self.saved_printers = saved_printers or []
        self.test_print_log: deque = deque(maxlen=100)
        self.network_timeout = 0.5

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

    def _send_network(self, printer: Dict, zpl: str) -> Tuple[bool, Optional[str]]:
        """Send ZPL to a network printer via TCP socket."""
        address = printer.get('address')
        port = printer.get('port', self.DEFAULT_PORT)
        logger.info("send_network target=%s:%s raw_bytes=%s", address, port, len(zpl or ''))

        if not address:
            return False, "No printer address"

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.connect((address, port))
            sock.send(zpl.encode('utf-8'))
            logger.info("ZPL payload successfully sent to network printer %s:%s.", address, port)
            return True, None
        except socket.timeout:
            return False, "Connection timeout"
        except socket.error as e:
            return False, f"Socket error: {e}"
        except Exception as e:
            return False, f"Error: {e}"
        finally:
            sock.close()

    def _send_local(self, printer: Dict, raw: str) -> Tuple[bool, Optional[str]]:
        """Send raw data to a local OS printer (Windows via win32print)."""
        return self._send_win32(printer, raw)

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
        """Quick check if port is open on given IP."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(self.network_timeout)
                result = sock.connect_ex((ip, port))
                return result == 0
            finally:
                sock.close()
        except Exception:
            return False

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
            if self._check_port_open(address, port):
                return True, f"Connected to {address}:{port}"
            return False, "Cannot connect to printer"

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

    # ── Local / OS-installed printer methods ──────────────────────────

    def list_local_printers(self) -> List[Dict]:
        """List printers installed in the OS (Windows via win32print)."""
        return self._list_win32_printers()

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
