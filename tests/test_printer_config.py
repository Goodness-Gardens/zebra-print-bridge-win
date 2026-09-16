import pytest
import httpx
from app.printer_manager import PRINTER_CONFIG_SCHEMA
from app.server import PrintServer


def test_printer_config_schema_structure():
    assert "options" in PRINTER_CONFIG_SCHEMA
    options = PRINTER_CONFIG_SCHEMA["options"]
    assert "print_method" in options
    assert "print_width" in options
    assert "label_length" in options
    assert "media_type" in options
    assert "print_mode" in options
    assert "speed" in options
    assert "darkness" in options
    assert "resolution_dpi" in options

    assert "direct thermal" in options["print_method"]["choices"]
    assert "thermal transfer" in options["print_method"]["choices"]
    assert "gap/notch" in options["media_type"]["choices"]
    assert "save_to_flash" in options


def test_set_printer_sgd_config_features(monkeypatch):
    from unittest.mock import MagicMock
    from app.printer_manager import set_printer_sgd_config

    sent_data = []

    mock_sock = MagicMock()
    mock_sock.recv.return_value = b'"applied_val"\r\n'

    def fake_sendall(data):
        sent_data.append(data)

    mock_sock.sendall = fake_sendall

    monkeypatch.setattr(
        "app.printer_manager.connect_smart_socket",
        lambda ip, port, timeout: mock_sock,
    )

    settings = {
        "print_method": "direct thermal",
        "print_width": 609,
        "device.friendly_name": "NEW_NAME",
        "raw_command": "~JC",
        "save_to_flash": True,
    }

    success, applied, err = set_printer_sgd_config("192.168.1.150", 9100, settings)
    assert success is True
    assert err is None
    assert applied["print_method"] == "applied_val"
    assert applied["device.friendly_name"] == "applied_val"
    assert applied["raw_command"] == '"applied_val"'
    assert applied["save_to_flash"] is True

    # Check that ^XA^JUS^XZ was sent
    assert any(b"^XA^JUS^XZ" in d for d in sent_data)
    # Check that raw command ~JC was sent
    assert any(b"~JC" in d for d in sent_data)
    # Check that device.friendly_name SGD command was sent
    assert any(b'device.friendly_name' in d for d in sent_data)


@pytest.mark.anyio
async def test_printer_config_endpoints():
    mock_config = {
        "model": "ZT230",
        "friendly_name": "NH-LSHIP1",
        "serial_number": "52J164400301",
        "firmware": "V72.19.15Z",
        "status": "PRINTER READY",
        "config": {
            "dpi": 203,
            "print_method": "direct thermal",
            "print_width_dots": 609,
            "print_width_inches": 3.0,
            "label_length_dots": 431,
            "label_length_inches": 2.12,
            "media_type": "gap/notch",
            "print_mode": "tear off",
            "speed_ips": 6.0,
            "darkness": 30.0,
        },
    }

    def mock_get_config(target):
        if target == "unknown":
            raise ValueError("Printer not found")
        return {
            "success": True,
            "target": target,
            "resolved_ip": "192.168.1.150",
            "port": 9100,
            "printer_type": "network",
            **mock_config,
        }

    def mock_set_config(target, settings):
        if target == "unknown":
            raise ValueError("Printer not found")
        return {
            "success": True,
            "message": f"Configuration applied to '{target}'",
            "target": target,
            "resolved_ip": "192.168.1.150",
            "port": 9100,
            "applied_settings": settings,
            "current_config": {**mock_config["config"], **settings},
        }

    server = PrintServer(
        port=5050,
        on_get_config_schema=lambda: PRINTER_CONFIG_SCHEMA,
        on_get_printer_config=mock_get_config,
        on_set_printer_config=mock_set_config,
    )

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. GET /printers/config/schema
        res_schema = await client.get("/printers/config/schema")
        assert res_schema.status_code == 200
        schema_data = res_schema.json()
        assert "options" in schema_data
        assert "print_method" in schema_data["options"]

        # 2. GET /printers/NH-LSHIP1/config
        res_get = await client.get("/printers/NH-LSHIP1/config")
        assert res_get.status_code == 200
        data_get = res_get.json()
        assert data_get["success"] is True
        assert data_get["config"]["print_method"] == "direct thermal"
        assert data_get["config"]["dpi"] == 203

        # 3. GET /printers/config?target=NH-LSHIP1
        res_get_query = await client.get("/printers/config?target=NH-LSHIP1")
        assert res_get_query.status_code == 200
        assert res_get_query.json()["model"] == "ZT230"

        # 4. POST /printers/NH-LSHIP1/config (update settings)
        res_set = await client.post(
            "/printers/NH-LSHIP1/config",
            json={"darkness": 25.0, "print_method": "direct thermal"},
        )
        assert res_set.status_code == 200
        data_set = res_set.json()
        assert data_set["success"] is True
        assert data_set["applied_settings"]["darkness"] == 25.0

        # 5. Unknown printer -> 404
        res_404 = await client.get("/printers/unknown/config")
        assert res_404.status_code == 404
