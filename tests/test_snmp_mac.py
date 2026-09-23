from unittest.mock import patch, MagicMock
from app.printer_manager import (
    _format_mac_bytes,
    _parse_ber_length,
    _build_snmp_packet,
    _query_snmp_mac,
    probe_zebra_printer,
)


def test_format_mac_bytes():
    # Valid 6 bytes
    raw = bytes([0x00, 0x07, 0x4D, 0x6F, 0xC2, 0x14])
    assert _format_mac_bytes(raw) == "00:07:4D:6F:C2:14"

    # All zeros should be discarded (e.g. unconfigured or loopback interface)
    all_zeros = bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    assert _format_mac_bytes(all_zeros) is None

    # Invalid length (< 6 or > 6)
    assert _format_mac_bytes(bytes([0x00, 0x07, 0x4D])) is None
    assert _format_mac_bytes(bytes([0x00] * 8)) is None
    assert _format_mac_bytes(b"") is None


def test_parse_ber_length():
    # Short form (< 128)
    length, offset = _parse_ber_length(bytes([0x06, 0xAA]), 0)
    assert length == 6
    assert offset == 1

    # Long form 1 byte
    length, offset = _parse_ber_length(bytes([0x81, 0x85]), 0)
    assert length == 133
    assert offset == 2

    # Long form 2 bytes
    length, offset = _parse_ber_length(bytes([0x82, 0x01, 0x05]), 0)
    assert length == 261
    assert offset == 3


def test_snmp_packet_builder():
    pkt_get = _build_snmp_packet([1, 3, 6, 1, 2, 1, 2, 2, 1, 6], pdu_type=0xA0)
    assert b"\xa0" in pkt_get
    assert b"public" in pkt_get

    pkt_getnext = _build_snmp_packet([1, 3, 6, 1, 2, 1, 2, 2, 1, 6], pdu_type=0xA1)
    assert b"\xa1" in pkt_getnext
    assert b"public" in pkt_getnext


def test_query_snmp_mac_mock():
    # Mock socket response returning GetResponse PDU (0xA2) with 6-byte PhysAddress
    mac_bytes = bytes([0x00, 0x11, 0x22, 0x33, 0x44, 0x55])
    # Build simulated response packet
    # OID: 1.3.6.1.2.1.2.2.1.6.1 (10 bytes OID payload)
    oid_bytes = bytes([43, 6, 1, 2, 1, 2, 2, 1, 6, 1])
    simulated_resp = (
        b"\x30\x29"  # Sequence len 41
        b"\x02\x01\x00"  # SNMP version 1
        b"\x04\x06public"  # Community
        b"\xa2\x1c"  # GetResponse PDU len 28
        b"\x02\x01\x01"  # Request ID
        b"\x02\x01\x00"  # Error status 0
        b"\x02\x01\x00"  # Error index 0
        b"\x30\x11"  # VarbindList len 17
        b"\x30\x0f"  # Varbind len 15
        b"\x06\x0a" + oid_bytes +  # OID tag 0x06, len 10
        b"\x04\x06" + mac_bytes  # OCTET STRING tag 0x04, len 6
    )

    with patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.recvfrom.return_value = (simulated_resp, ("192.168.1.50", 161))

        mac = _query_snmp_mac("192.168.1.50")
        assert mac == "00:11:22:33:44:55"


def test_probe_zebra_printer_snmp_priority():
    # Test that SNMP has priority over ARP in probe_zebra_printer
    with patch("socket.socket") as mock_sock_cls, \
         patch("urllib.request.urlopen") as mock_urlopen, \
         patch("app.printer_manager._query_snmp_mac") as mock_snmp_mac, \
         patch("app.printer_manager.get_mac_for_ip") as mock_arp_mac, \
         patch("app.printer_manager._query_snmp_string") as mock_snmp_str:

        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.connect_ex.return_value = 0  # Port 9100 open
        mock_urlopen.side_effect = Exception("HTTP disabled in test")

        mock_snmp_mac.return_value = "00:07:4D:AA:BB:CC"
        mock_arp_mac.return_value = "11:22:33:44:55:66"  # ARP has different/stale MAC
        mock_snmp_str.return_value = "SER123456"

        res = probe_zebra_printer("192.168.1.50")
        assert res is not None
        assert res["mac"] == "00:07:4D:AA:BB:CC"
        assert res["mac_source"] == "snmp"
        # Verify INVARIANT: connect_ex was called on 9100, but NEVER send or sendall
        mock_sock.connect_ex.assert_called_with(("192.168.1.50", 9100))
        assert not mock_sock.send.called
        assert not mock_sock.sendall.called


def test_probe_zebra_printer_arp_fallback():
    # Test fallback to ARP when SNMP returns None
    with patch("socket.socket") as mock_sock_cls, \
         patch("urllib.request.urlopen") as mock_urlopen, \
         patch("app.printer_manager._query_snmp_mac") as mock_snmp_mac, \
         patch("app.printer_manager.get_mac_for_ip") as mock_arp_mac:

        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.connect_ex.return_value = 0  # Port 9100 open
        mock_urlopen.side_effect = Exception("HTTP disabled in test")

        mock_snmp_mac.return_value = None  # SNMP fails or filtered across network
        mock_arp_mac.return_value = "11:22:33:44:55:66"  # ARP found locally

        res = probe_zebra_printer("192.168.1.50")
        assert res is not None
        assert res["mac"] == "11:22:33:44:55:66"
        assert res["mac_source"] == "arp"
        assert not mock_sock.send.called
        assert not mock_sock.sendall.called


def test_snmp_timeout_aborts_after_first_query_and_caches():
    import socket
    from app.printer_manager import (
        PrinterManager,
        _clear_no_snmp_cache,
        _is_ip_no_snmp,
    )

    _clear_no_snmp_cache()
    test_ip = "192.168.1.77"
    assert not _is_ip_no_snmp(test_ip)

    pm = PrinterManager(scan_network=False)

    with patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.recvfrom.side_effect = socket.timeout("Timed out")

        with patch("app.printer_manager.get_mac_for_ip", return_value=None):
            pm.verify_device_identity(test_ip, expected_mac="00:11:22:33:44:55")

        # INVARIANT: Exactly one SNMP UDP query was attempted
        assert mock_sock.sendto.call_count == 1
        # IP must now be cached as no-snmp
        assert _is_ip_no_snmp(test_ip)

        # Subsequent verification within TTL makes 0 queries
        mock_sock.sendto.reset_mock()
        with patch("app.printer_manager.get_mac_for_ip", return_value=None):
            pm.verify_device_identity(test_ip, expected_mac="00:11:22:33:44:55")
        assert mock_sock.sendto.call_count == 0

    _clear_no_snmp_cache()

