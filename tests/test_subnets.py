import ipaddress
import pytest
import httpx
from app.printer_manager import parse_windows_ipconfig, get_available_subnets_info, get_local_subnets
from app.server import PrintServer


def test_parse_windows_ipconfig_english():
    sample = """
Windows IP Configuration

Ethernet adapter Ethernet 2:
   Connection-specific DNS Suffix  . : local
   Description . . . . . . . . . . . : Realtek PCIe GbE Family Controller
   Physical Address. . . . . . . . . : 00-07-4D-6F-C2-14
   DHCP Enabled. . . . . . . . . . . : Yes
   IPv4 Address. . . . . . . . . . . : 192.168.1.150(Preferred)
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 192.168.1.1

Wireless LAN adapter Wi-Fi:
   Connection-specific DNS Suffix  . :
   IPv4 Address. . . . . . . . . . . : 10.0.5.23(Preferred)
   Subnet Mask . . . . . . . . . . . : 255.255.254.0
"""
    results = parse_windows_ipconfig(sample)
    assert len(results) == 2
    assert results[0]["interface"] == "Ethernet 2"
    assert results[0]["ip"] == "192.168.1.150"
    assert results[0]["cidr"] == "192.168.1.0/24"
    assert results[0]["prefix_len"] == 24

    assert results[1]["interface"] == "Wi-Fi"
    assert results[1]["ip"] == "10.0.5.23"
    assert results[1]["cidr"] == "10.0.4.0/23"
    assert results[1]["prefix_len"] == 23


def test_parse_windows_ipconfig_spanish():
    sample = """
Configuración IP de Windows

Adaptador de Ethernet Ethernet:
   Sufijo DNS específico para la conexión. . :
   Vínculo: dirección IPv6 local. . . : fe80::...
   Dirección IPv4. . . . . . . . . . . . . . : 192.168.20.45(Preferido)
   Máscara de subred . . . . . . . . . . . . : 255.255.255.0
   Puerta de enlace predeterminada . . . . . : 192.168.20.1
"""
    results = parse_windows_ipconfig(sample)
    assert len(results) == 1
    assert results[0]["interface"] == "Ethernet"
    assert results[0]["ip"] == "192.168.20.45"
    assert results[0]["cidr"] == "192.168.20.0/24"
    assert results[0]["prefix_len"] == 24


def test_get_available_subnets_info():
    info = get_available_subnets_info(custom_subnets=["10.10.10.0/24"])
    assert "detected_subnets" in info
    assert "custom_subnets" in info
    assert "all_subnets" in info
    assert "total_subnets" in info
    assert "total_ips" in info
    assert any(c["cidr"] == "10.10.10.0/24" for c in info["custom_subnets"])
    assert "10.10.10.0/24" in info["all_subnets"]


@pytest.mark.anyio
async def test_subnets_api_endpoints():
    custom = ["192.168.100.0/24"]

    def mock_get_subnets():
        return {
            "detected_subnets": [
                {"interface": "eth0", "ip": "192.168.1.10", "netmask": "255.255.255.0", "cidr": "192.168.1.0/24", "prefix_len": 24, "hosts_count": 254}
            ],
            "custom_subnets": [{"cidr": c, "prefix_len": 24, "hosts_count": 254} for c in custom],
            "all_subnets": ["192.168.1.0/24"] + custom,
            "total_subnets": 2,
            "total_ips": 508,
        }

    def mock_scan_subnets(subnet=None, clear_cache=False):
        return {
            "scanned_subnet": subnet or "all",
            "discovered_printers": [{"name": "Zebra GK420d", "ip": "192.168.1.200", "port": 9100}],
            "count": 1,
        }

    def mock_add_custom(subnet):
        if subnet not in custom:
            custom.append(subnet)
        return mock_get_subnets()

    def mock_remove_custom(subnet):
        if subnet in custom:
            custom.remove(subnet)
        return mock_get_subnets()

    server = PrintServer(
        port=5050,
        on_get_subnets=mock_get_subnets,
        on_scan_subnets=mock_scan_subnets,
        on_add_custom_subnet=mock_add_custom,
        on_remove_custom_subnet=mock_remove_custom,
    )

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. GET /subnets
        res = await client.get("/subnets")
        assert res.status_code == 200
        data = res.json()
        assert "all_subnets" in data
        assert len(data["detected_subnets"]) == 1
        assert "192.168.100.0/24" in data["all_subnets"]

        # 2. POST /subnets/scan
        res_scan = await client.post("/subnets/scan?subnet=192.168.1.0/24")
        assert res_scan.status_code == 200
        data_scan = res_scan.json()
        assert data_scan["success"] is True
        assert data_scan["count"] == 1
        assert data_scan["discovered_printers"][0]["ip"] == "192.168.1.200"

        # 3. POST /subnets (add custom subnet)
        res_add = await client.post("/subnets", json={"subnet": "172.16.50.0/24"})
        assert res_add.status_code == 200
        data_add = res_add.json()
        assert data_add["success"] is True
        assert "172.16.50.0/24" in data_add["all_subnets"]

        # 4. Invalid subnet
        res_invalid = await client.post("/subnets", json={"subnet": "not-a-valid-subnet"})
        assert res_invalid.status_code == 400

        # 5. DELETE /subnets
        res_del = await client.delete("/subnets?subnet=172.16.50.0/24")
        assert res_del.status_code == 200
        data_del = res_del.json()
        assert data_del["success"] is True
        assert "172.16.50.0/24" not in data_del["all_subnets"]
