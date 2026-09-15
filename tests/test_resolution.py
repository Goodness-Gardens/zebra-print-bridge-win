import unittest
from unittest.mock import MagicMock, patch

from app.printer_manager import PrinterManager
from app.main import PrintBridge
from app.config import Config


class TestResolution(unittest.TestCase):
    def setUp(self):
        self.pm = PrinterManager(
            scan_network=False,
            network_timeout=0.1,
            verify_identity=True,
        )
        self.pm._network_printers.clear()
        self.pm._alias_map.clear()

    def test_mac_cache_hit_verified(self):
        mac = "00:11:22:33:44:55"
        self.pm._network_printers[mac] = {
            "mac": mac,
            "ip": "192.168.1.100",
            "port": 9100,
            "serial": "ZEB123",
        }

        with patch.object(self.pm, "_check_port_open", return_value=True):
            with patch.object(self.pm, "verify_device_identity", return_value=(True, mac, "ZEB123")):
                res = self.pm.resolve_target(mac=mac)
                self.assertTrue(res.reachable)
                self.assertTrue(res.verified)
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
            with patch.object(self.pm, "verify_device_identity", return_value=(False, "AA:BB:CC:DD:EE:FF", "OTHER")):
                with patch("app.printer_manager.get_ip_for_mac", return_value=None):
                    res = self.pm.resolve_target(mac=mac)
                    self.assertFalse(res.reachable)
                    # Cache entry should have its ip cleared
                    self.assertIsNone(self.pm._network_printers[mac]["ip"])

    def test_mac_arp_hit_verified(self):
        mac = "00:11:22:33:44:55"
        with patch("app.printer_manager.get_ip_for_mac", return_value="192.168.1.105"):
            with patch.object(self.pm, "_check_port_open", return_value=True):
                with patch.object(self.pm, "verify_device_identity", return_value=(True, mac, "ZEB999")):
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
            with patch.object(self.pm, "verify_device_identity", return_value=(True, mac, "ZEBHINT")):
                with patch.object(self.pm, "scan_subnet") as mock_scan:
                    res = self.pm.resolve_target(mac=mac, ip=hint_ip)
                    self.assertTrue(res.reachable)
                    self.assertTrue(res.verified)
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
                with patch.object(self.pm, "verify_device_identity", return_value=(True, mac, "ZEBARP")):
                    res = self.pm.resolve_target(mac=mac, ip=hint_ip)
                    self.assertTrue(res.reachable)
                    self.assertEqual(res.ip, arp_ip)
                    self.assertEqual(res.source, "arp")

    def test_simulated_test_target(self):
        res = self.pm.resolve_target(ip="test")
        self.assertTrue(res.reachable)
        self.assertEqual(res.ip, "test")
        self.assertEqual(res.source, "test")


class TestPrintBridgeResolution(unittest.TestCase):
    def setUp(self):
        config = MagicMock(spec=Config)
        config.saved_printers = []
        config.get.return_value = {}
        config.custom_subnets = []
        config.verify_identity = True
        config.port = 5050
        config.printer_ip = "127.0.0.1"
        config.printer_port = 9100

        with patch("app.main.PrintServer"):
            self.bridge = PrintBridge(config=config)
        self.bridge.printer_manager.scan_network = False

    def tearDown(self):
        self.bridge.stop()

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
