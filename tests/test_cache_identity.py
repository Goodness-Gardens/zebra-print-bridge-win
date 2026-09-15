import json
import tempfile
from pathlib import Path
from app.printer_manager import PrinterManager, get_identity_key


def test_get_identity_key():
    assert get_identity_key(mac="00:07:4D:6F:C2:14") == "00:07:4D:6F:C2:14"
    assert get_identity_key(mac="00-07-4d-6f-c2-14") == "00:07:4D:6F:C2:14"
    assert get_identity_key(mac=None, serial="52J164400294") == "serial:52J164400294"
    assert get_identity_key(mac=None, serial=None, ip="192.168.1.50") == "ip:192.168.1.50"
    assert get_identity_key(mac=None, serial=None, ip=None) == ""


def test_v1_to_v2_cache_migration():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "network_printers.json"

        # Write v1 cache (indexed by IP)
        v1_data = {
            "updated_at": "2026-09-08T12:00:00",
            "printers": {
                "192.168.1.50": {
                    "name": "Zebra-Warehouse",
                    "hostname": "zebra-wh.local",
                    "ip": "192.168.1.50",
                    "port": 9100,
                    "unique_id": "SN123456",
                    "mac_address": "00:07:4d:6f:c2:14",
                    "last_seen": "2026-09-08T12:00:00"
                },
                "192.168.1.51": {
                    "name": "Zebra-Shipping",
                    "hostname": "zebra-ship.local",
                    "ip": "192.168.1.51",
                    "port": 9100,
                    "unique_id": "SN789012",
                    "mac_address": "",
                    "last_seen": "2026-09-08T12:00:00"
                }
            }
        }
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(v1_data, f)

        pm = PrinterManager(scan_network=False)
        pm.cache_file = cache_file
        pm._load_cache()

        # Check in-memory structure
        printers = pm._network_printers
        assert "00:07:4D:6F:C2:14" in printers
        assert "serial:SN789012" in printers

        entry1 = printers["00:07:4D:6F:C2:14"]
        assert entry1["mac"] == "00:07:4D:6F:C2:14"
        assert entry1["serial"] == "SN123456"
        assert entry1["ip"] == "192.168.1.50"
        assert entry1["mac_source"] == "arp"

        # Check migrated file on disk has version 2
        with open(cache_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved.get("version") == 2
        assert "00:07:4D:6F:C2:14" in saved["printers"]


def test_mac_rediscovered_at_new_ip_does_not_duplicate():
    pm = PrinterManager(scan_network=False)
    pm._network_printers = {}

    # 1. Add printer at 192.168.1.50
    changed, key = pm._update_or_add_printer_locked({
        "name": "Zebra GK420d",
        "mac": "00:07:4D:6F:C2:14",
        "serial": "SN123",
        "ip": "192.168.1.50",
    })
    assert changed is True
    assert key == "00:07:4D:6F:C2:14"
    assert len(pm._network_printers) == 1
    assert pm._network_printers[key]["ip"] == "192.168.1.50"

    # 2. Rediscover same MAC at 192.168.1.60
    changed, key2 = pm._update_or_add_printer_locked({
        "name": "Zebra GK420d Renamed",
        "mac": "00:07:4D:6F:C2:14",
        "serial": "SN123",
        "ip": "192.168.1.60",
    })
    assert key2 == "00:07:4D:6F:C2:14"
    # Still exactly ONE entry in cache!
    assert len(pm._network_printers) == 1
    assert pm._network_printers[key2]["ip"] == "192.168.1.60"
    assert pm._network_printers[key2]["name"] == "Zebra GK420d Renamed"


def test_ip_conflict_resolution():
    pm = PrinterManager(scan_network=False)
    pm._network_printers = {}

    # Device 1 takes IP 192.168.1.100
    pm._update_or_add_printer_locked({
        "name": "Printer A",
        "mac": "11:22:33:44:55:66",
        "ip": "192.168.1.100",
    })

    # Device 2 later takes IP 192.168.1.100
    pm._update_or_add_printer_locked({
        "name": "Printer B",
        "mac": "AA:BB:CC:DD:EE:FF",
        "ip": "192.168.1.100",
    })

    # Device 1's IP should be invalidated (set to None)
    assert pm._network_printers["11:22:33:44:55:66"]["ip"] is None
    # Device 2 has 192.168.1.100
    assert pm._network_printers["AA:BB:CC:DD:EE:FF"]["ip"] == "192.168.1.100"
