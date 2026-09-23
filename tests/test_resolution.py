import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.printer_manager import PrinterManager
from app.main import PrintBridge
from app.config import Config


class TestResolution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.pm = PrinterManager(
            scan_network=False,
            network_timeout=0.1,
            verify_identity=True,
            cache_dir=Path(self.tmp_dir.name),
        )
        self.pm._network_printers.clear()
        self.pm._alias_map.clear()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_mac_cache_hit_verified(self):
        mac = "00:11:22:33:44:55"
        self.pm._network_printers[mac] = {
            "mac": mac,
            "ip": "192.168.1.100",
            "port": 9100,
            "serial": "ZEB123",
        }

        with patch.object(self.pm, "_check_port_open", return_value=True):
            with patch.object(self.pm, "verify_device_identity", return_value=("match", mac, "ZEB123")):
                res = self.pm.resolve_target(mac=mac)
                self.assertTrue(res.reachable)
                self.assertTrue(res.verified)
                self.assertEqual(res.verification, "match")
                self.assertEqual(res.ip, "192.168.1.100")
                self.assertEqual(res.source, "cache")

    def test_mac_cache_identity_mismatch_invalidates(self):
        mac = "00:11:22:33:44:55"
        self.pm._network_printers[mac] = {
            "mac": mac,
            "ip": "192.168.1.100",
            "port": 9100,
            "serial": "ZEB123",
        }

        # IP 192.168.1.100 now belongs to a different MAC!
        with patch.object(self.pm, "_check_port_open", return_value=True):
            with patch.object(self.pm, "verify_device_identity", return_value=("mismatch", "AA:BB:CC:DD:EE:FF", "OTHER")):
                with patch("app.printer_manager.get_ip_for_mac", return_value=None):
                    res = self.pm.resolve_target(mac=mac)
                    self.assertFalse(res.reachable)
                    # Cache entry should have its ip cleared
                    self.assertIsNone(self.pm._network_printers[mac]["ip"])

    def test_mac_arp_hit_verified(self):
        mac = "00:11:22:33:44:55"
        with patch("app.printer_manager.get_ip_for_mac", return_value="192.168.1.105"):
            with patch.object(self.pm, "_check_port_open", return_value=True):
                with patch.object(self.pm, "verify_device_identity", return_value=("match", mac, "ZEB999")):
                    res = self.pm.resolve_target(mac=mac)
                    self.assertTrue(res.reachable)
                    self.assertEqual(res.ip, "192.168.1.105")
                    self.assertEqual(res.source, "arp")
                    self.assertIn(mac, self.pm._network_printers)
                    self.assertEqual(self.pm._network_printers[mac]["ip"], "192.168.1.105")

    @patch("socket.getaddrinfo")
    def test_unknown_mac_no_scan_and_no_dns(self, mock_getaddrinfo):
        mac = "00:11:22:33:44:55"
        self.pm.scan_network = False
        with patch("app.printer_manager.get_ip_for_mac", return_value=None):
            res = self.pm.resolve_target(mac=mac)
            self.assertFalse(res.reachable)
            self.assertIsNone(res.ip)
            # CRITICAL: getaddrinfo must NEVER be called with a MAC address!
            mock_getaddrinfo.assert_not_called()

    @patch("socket.getaddrinfo")
    def test_subnet_scan_serialized_max_once(self, mock_getaddrinfo):
        self.pm.scan_network = True
        mac = "00:11:22:33:44:55"

        with patch.object(self.pm, "_check_port_open", return_value=False):
            with patch.object(self.pm, "scan_subnet") as mock_scan:
                with patch("app.printer_manager.get_ip_for_mac", return_value=None):
                    res = self.pm.resolve_target(mac=mac)
                    self.assertFalse(res.reachable)
                    # scan_subnet called exactly once
                    mock_scan.assert_called_once()
                    # getaddrinfo never called with MAC
                    mock_getaddrinfo.assert_not_called()

    def test_hint_ip_verified_bypasses_scan(self):
        self.pm.scan_network = True
        mac = "00:11:22:33:44:55"
        hint_ip = "192.168.1.50"

        with patch.object(self.pm, "_check_port_open", return_value=True):
            with patch.object(self.pm, "verify_device_identity", return_value=("match", mac, "ZEBHINT")):
                with patch.object(self.pm, "scan_subnet") as mock_scan:
                    res = self.pm.resolve_target(mac=mac, ip=hint_ip)
                    self.assertTrue(res.reachable)
                    self.assertTrue(res.verified)
                    self.assertEqual(res.verification, "match")
                    self.assertEqual(res.ip, hint_ip)
                    self.assertEqual(res.source, "hint")
                    # scan_subnet was NOT called because hint verified
                    mock_scan.assert_not_called()
                    # Cache was updated with verified IP
                    self.assertIn(mac, self.pm._network_printers)
                    self.assertEqual(self.pm._network_printers[mac]["ip"], hint_ip)

    def test_hint_ip_unreachable_falls_back_to_arp(self):
        mac = "00:11:22:33:44:55"
        hint_ip = "192.168.1.50"
        arp_ip = "192.168.1.200"

        def mock_port_open(ip, port=9100, timeout=1.0):
            return ip == arp_ip

        with patch.object(self.pm, "_check_port_open", side_effect=mock_port_open):
            with patch("app.printer_manager.get_ip_for_mac", return_value=arp_ip):
                with patch.object(self.pm, "verify_device_identity", return_value=("match", mac, "ZEBARP")):
                    res = self.pm.resolve_target(mac=mac, ip=hint_ip)
                    self.assertTrue(res.reachable)
                    self.assertEqual(res.ip, arp_ip)
                    self.assertEqual(res.source, "arp")

    def test_simulated_test_target(self):
        res = self.pm.resolve_target(ip="test")
        self.assertTrue(res.reachable)
        self.assertEqual(res.ip, "test")
        self.assertEqual(res.source, "test")

    def test_verify_device_identity_tri_state(self):
        mac = "00:11:22:33:44:55"
        diff_mac = "AA:BB:CC:DD:EE:FF"

        # 1. Match state
        with patch("app.printer_manager._query_snmp_mac", return_value=mac):
            with patch("app.printer_manager._query_snmp_string", return_value="ZEB123"):
                status, det_mac, det_ser = self.pm.verify_device_identity("192.168.1.50", expected_mac=mac, expected_serial="ZEB123")
                self.assertEqual(status, "match")
                self.assertEqual(det_mac, mac)
                self.assertEqual(det_ser, "ZEB123")

        # 2. Mismatch state
        with patch("app.printer_manager._query_snmp_mac", return_value=diff_mac):
            with patch("app.printer_manager._query_snmp_string", return_value="OTHER"):
                status, det_mac, det_ser = self.pm.verify_device_identity("192.168.1.50", expected_mac=mac, expected_serial="ZEB123")
                self.assertEqual(status, "mismatch")

        # 3. Unverifiable state (no SNMP, no ARP)
        with patch("app.printer_manager._query_snmp_mac", return_value=None):
            with patch("app.printer_manager._query_snmp_string", return_value=""):
                with patch("app.printer_manager.get_mac_for_ip", return_value=""):
                    status, det_mac, det_ser = self.pm.verify_device_identity("192.168.1.50", expected_mac=mac)
                    self.assertEqual(status, "unverifiable")
                    self.assertIsNone(det_mac)

    def test_unverifiable_identity_fail_open_by_default(self):
        mac = "00:11:22:33:44:55"
        self.pm._network_printers[mac] = {
            "mac": mac,
            "ip": "192.168.1.100",
            "port": 9100,
            "serial": "ZEB123",
        }
        with patch.object(self.pm, "_check_port_open", return_value=True):
            with patch.object(self.pm, "verify_device_identity", return_value=("unverifiable", None, None)):
                res = self.pm.resolve_target(mac=mac)
                self.assertTrue(res.reachable)
                self.assertFalse(res.verified)
                self.assertEqual(res.verification, "unverifiable")
                self.assertEqual(res.ip, "192.168.1.100")


class TestPrintBridgeResolution(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        config = MagicMock(spec=Config)
        config.saved_printers = []
        config.get.return_value = {}
        config.custom_subnets = []
        config.verify_identity = True
        config.scan_network = False
        config.network_timeout = 0.1
        config.log_level = "INFO"
        config.strict_identity = False
        config.config_dir = Path(self.tmp_dir.name)
        config.port = 5050
        config.printer_ip = "127.0.0.1"
        config.printer_port = 9100

        with patch("app.main.PrintServer"):
            self.bridge = PrintBridge(config=config)

    def tearDown(self):
        self.bridge.stop()
        self.tmp_dir.cleanup()

    def test_on_job_received_test_mode(self):
        job_data = {
            "printer_ip": "test",
            "raw_command": "^XA^FDHello^FS^XZ",
            "source": "unit-test",
        }
        resp = self.bridge.on_job_received(job_data)
        self.assertTrue(resp["success"])
        self.assertIn("Test Mode", resp["message"])

        # Check queued job
        job = self.bridge.print_queue.get_nowait()
        self.assertEqual(job["printer_ip"], "test")
        self.assertEqual(job["raw_command"], "^XA^FDHello^FS^XZ")

    def test_on_job_received_network_preflight_success(self):
        job_data = {
            "printer_ip": "192.168.1.80",
            "raw_command": "^XA^FDHello^FS^XZ",
            "source": "unit-test",
        }
        with patch.object(self.bridge.printer_manager, "_test_network_connection", return_value=(True, "Connected")):
            resp = self.bridge.on_job_received(job_data)
            self.assertTrue(resp["success"])
            job = self.bridge.print_queue.get_nowait()
            self.assertEqual(job["printer_ip"], "192.168.1.80")
            self.assertFalse(job["use_local"])

    def test_on_job_received_unreachable_fails(self):
        job_data = {
            "printer_ip": "192.168.1.80",
            "raw_command": "^XA^FDHello^FS^XZ",
            "source": "unit-test",
        }
        with patch.object(self.bridge.printer_manager, "_test_network_connection", return_value=(False, "Cannot connect to printer")):
            resp = self.bridge.on_job_received(job_data)
            self.assertFalse(resp["success"])
            self.assertIn("Cannot connect", resp["message"])
            self.assertTrue(self.bridge.print_queue.empty())

    def test_check_connection_test(self):
        res = self.bridge.check_connection(target="test")
        self.assertTrue(res["success"])
        self.assertEqual(res["printer_type"], "test")
        self.assertIn("Test printer ready", res["message"])

    def test_check_connection_cached_mac_single_verification(self):
        mac = "00:11:22:33:44:55"
        self.bridge.printer_manager._network_printers[mac] = {
            "mac": mac,
            "ip": "192.168.1.100",
            "port": 9100,
            "serial": "ZEB123",
        }
        with patch.object(self.bridge.printer_manager, "_check_port_open", return_value=True) as mock_port:
            with patch.object(self.bridge.printer_manager, "verify_device_identity", return_value=("match", mac, "ZEB123")) as mock_verify:
                res = self.bridge.check_connection(target=mac)
                self.assertTrue(res["success"])
                self.assertEqual(res["printer_ip"], "192.168.1.100")
                mock_port.assert_called_once()
                mock_verify.assert_called_once()



    def test_strict_identity_rejects_unverifiable_job(self):
        self.bridge.config.strict_identity = True
        job_data = {
            "printer_ip": "192.168.1.80",
            "raw_command": "^XA^FDHello^FS^XZ",
            "source": "unit-test",
        }
        with patch.object(self.bridge.printer_manager, "_check_port_open", return_value=True):
            with patch.object(self.bridge.printer_manager, "verify_device_identity", return_value=("unverifiable", None, None)):
                resp = self.bridge.on_job_received(job_data)
                self.assertFalse(resp["success"])
                self.assertEqual(resp.get("status_code"), 503)
                self.assertIn("strict_identity", resp["message"])

    def test_connection_endpoint_returns_unverifiable_identity(self):
        mac = "00:11:22:33:44:55"
        self.bridge.printer_manager._network_printers[mac] = {
            "mac": mac,
            "ip": "192.168.1.100",
            "port": 9100,
            "serial": "ZEB123",
        }
        with patch.object(self.bridge.printer_manager, "_check_port_open", return_value=True):
            with patch.object(self.bridge.printer_manager, "verify_device_identity", return_value=("unverifiable", None, None)):
                res = self.bridge.check_connection(target=mac)
                self.assertTrue(res["success"])
                self.assertEqual(res.get("identity"), "unverifiable")

    def test_unresolvable_hostname_single_scan_and_max_three_dns(self):
        self.bridge.printer_manager.scan_network = True
        job_data = {
            "printer_ip": "zebra-ghost-printer",
            "raw_command": "^XA^FDHello^FS^XZ",
            "source": "unit-test",
        }
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("Name or service not known")) as mock_dns:
            with patch.object(self.bridge.printer_manager, "scan_subnet") as mock_scan:
                resp = self.bridge.on_job_received(job_data)
                self.assertFalse(resp["success"])
                self.assertIn("Cannot resolve", resp["message"])
                # Exactly 1 scan_subnet call throughout entire on_job_received
                mock_scan.assert_called_once()
                # At most 3 getaddrinfo calls (name, .local, .localdomain)
                self.assertEqual(mock_dns.call_count, 3)
                calls = [c[0][0] for c in mock_dns.call_args_list]
                self.assertEqual(calls, ["zebra-ghost-printer", "zebra-ghost-printer.local", "zebra-ghost-printer.localdomain"])

    def test_check_connection_unresolvable_hostname(self):
        self.bridge.printer_manager.scan_network = False
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("Name or service not known")):
            res = self.bridge.check_connection(target="zebra-ghost-printer")
            self.assertFalse(res["success"])
            self.assertIn("Cannot resolve", res["message"])

    def test_discover_broadcast_stub(self):
        # Disabled by default
        self.assertFalse(self.bridge.printer_manager.discovery_broadcast)
        self.assertEqual(self.bridge.printer_manager.discover_broadcast(), [])
        # When enabled, stub safely returns empty list
        self.bridge.printer_manager.discovery_broadcast = True
        self.assertEqual(self.bridge.printer_manager.discover_broadcast(), [])


