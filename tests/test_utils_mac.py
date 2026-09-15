import platform
import unittest
from unittest.mock import MagicMock, patch

from app.utils import (
    get_ip_for_mac,
    get_local_mac,
    get_mac_for_ip,
    run_command,
    _local_mac_cache,
)


IFCONFIG_MACOS = """en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	options=6460<TSO4,TSO6,CHANNEL_IO,PARTIAL_CSUM,ZERO2LARGE_CSUM>
	ether a4:83:e7:2b:49:1a
	inet6 fe80::1035:461b:9819:4e21%en0 prefixlen 64 secured scopeid 0x6
	inet 192.168.1.150 netmask 0xffffff00 broadcast 192.168.1.255
	nd6 options=201<PERFORMNUD,DAD>
	media: autoselect
	status: active
"""

IPCONFIG_EN = """Windows IP Configuration

   Host Name . . . . . . . . . . . . : DESKTOP-ABC1234
   Primary Dns Suffix  . . . . . . . : 
   Node Type . . . . . . . . . . . . : Hybrid
   IP Routing Enabled. . . . . . . . : No
   WINS Proxy Enabled. . . . . . . . : No

Ethernet adapter Ethernet:

   Connection-specific DNS Suffix  . : local
   Description . . . . . . . . . . . : Intel(R) Ethernet Connection I219-V
   Physical Address. . . . . . . . . : 00-15-5D-12-34-56
   DHCP Enabled. . . . . . . . . . . : Yes
   Autoconfiguration Enabled . . . . : Yes
   IPv4 Address. . . . . . . . . . . : 192.168.1.100(Preferred)
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
"""

IPCONFIG_ES = """Configuración IP de Windows

   Nombre de host. . . . . . . . . . : PC-OFICINA
   Sufijo DNS principal  . . . . . . : 
   Tipo de nodo. . . . . . . . . . . : híbrido
   Enrutamiento IP habilitado. . . . : no
   Proxy WINS habilitado . . . . . . : no

Adaptador de Ethernet Ethernet:

   Sufijo DNS específico para la conexión. . : 
   Descripción . . . . . . . . . . . : Realtek PCIe GbE Family Controller
   Dirección física. . . . . . . . . : 00-1A-2B-3C-4D-5E
   DHCP habilitado . . . . . . . . . : Sí
   Configuración automática habilitada : Sí
   Vínculo: dirección IPv6 local. . . : fe80::d9f:270c:a38f:7a5e%12
   Dirección IPv4. . . . . . . . . . : 192.168.1.200(Preferido)
   Máscara de subred . . . . . . . . : 255.255.255.0
"""

ARP_AN_OUTPUT = """? (192.168.1.1) at 00:50:56:c0:00:08 on en0 ifscope [ethernet]
? (192.168.1.50) at 00:07:4d:6f:c2:14 on en0 ifscope [ethernet]
? (192.168.1.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]
"""

ARP_A_WINDOWS = """Interface: 192.168.1.100 --- 0xb
  Internet Address      Physical Address      Type
  192.168.1.1           00-50-56-c0-00-08     dynamic   
  192.168.1.50          00-07-4d-6f-c2-14     dynamic   
  192.168.1.255         ff-ff-ff-ff-ff-ff     static    
"""


class TestUtilsMac(unittest.TestCase):
    def setUp(self):
        _local_mac_cache.clear()

    def tearDown(self):
        _local_mac_cache.clear()

    def test_macos_ifconfig_parsing(self):
        with patch("platform.system", return_value="Darwin"):
            with patch("app.utils.run_command", return_value=IFCONFIG_MACOS):
                mac = get_local_mac("192.168.1.150", use_cache=False)
                self.assertEqual(mac, "A4:83:E7:2B:49:1A")

    def test_windows_ipconfig_en_parsing(self):
        with patch("platform.system", return_value="Windows"):
            with patch("app.utils.run_command", return_value=IPCONFIG_EN):
                mac = get_local_mac("192.168.1.100", use_cache=False)
                self.assertEqual(mac, "00:15:5D:12:34:56")

    def test_windows_ipconfig_es_with_accents_parsing(self):
        with patch("platform.system", return_value="Windows"):
            with patch("app.utils.run_command", return_value=IPCONFIG_ES):
                mac = get_local_mac("192.168.1.200", use_cache=False)
                self.assertEqual(mac, "00:1A:2B:3C:4D:5E")

    def test_arp_an_mac_lookup(self):
        with patch("platform.system", return_value="Darwin"):
            with patch("app.utils.get_local_ip", return_value="192.168.1.99"):
                with patch("app.utils.run_command", return_value=ARP_AN_OUTPUT):
                    mac = get_mac_for_ip("192.168.1.50")
                    self.assertEqual(mac, "00:07:4D:6F:C2:14")
                    ip = get_ip_for_mac("00:07:4D:6F:C2:14")
                    self.assertEqual(ip, "192.168.1.50")

    def test_arp_a_windows_lookup(self):
        with patch("platform.system", return_value="Windows"):
            with patch("app.utils.get_local_ip", return_value="192.168.1.99"):
                with patch("app.utils.run_command", return_value=ARP_A_WINDOWS):
                    mac = get_mac_for_ip("192.168.1.50")
                    self.assertEqual(mac, "00:07:4D:6F:C2:14")
                    ip = get_ip_for_mac("00:07:4D:6F:C2:14")
                    self.assertEqual(ip, "192.168.1.50")

    def test_local_mac_cached_single_subprocess(self):
        with patch("platform.system", return_value="Darwin"):
            with patch("app.utils.run_command", return_value=IFCONFIG_MACOS) as mock_run:
                results = [get_local_mac("192.168.1.150") for _ in range(10)]
                self.assertTrue(all(r == "A4:83:E7:2B:49:1A" for r in results))
                # Only 1 run_command call executed across 10 invocations
                mock_run.assert_called_once()

    def test_uuid_getnode_multicast_bit_discarded(self):
        # Bit 40 set (multicast / pseudo-random MAC)
        multicast_node = (1 << 40) | 0x001122334455
        with patch("platform.system", return_value="UnknownOS"):
            with patch("uuid.getnode", return_value=multicast_node):
                mac = get_local_mac(use_cache=False)
                self.assertIsNone(mac)

    def test_uuid_getnode_unicast_accepted(self):
        # Bit 40 clear (unicast real hardware MAC)
        unicast_node = 0x001122334455
        with patch("platform.system", return_value="UnknownOS"):
            with patch("uuid.getnode", return_value=unicast_node):
                mac = get_local_mac(use_cache=False)
                self.assertEqual(mac, "00:11:22:33:44:55")
