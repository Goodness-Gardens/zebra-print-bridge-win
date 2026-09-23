"""
Unit tests for NetSuite Suitelet Server Synchronization.
"""

from io import BytesIO
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from app.config import Config
from app.suitelet_sync import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_COMPID,
    DEFAULT_DEPLOY_ID,
    DEFAULT_SCRIPT_ID,
    SuiteletSyncManager,
    build_suitelet_sync_url,
    mask_url_sensitive_params,
)


def test_build_suitelet_sync_url_defaults():
    url = build_suitelet_sync_url(
        mac="62:39:a2:0d:a4:b5",
        ip="192.168.10.115",
        port=5050,
        name="Miami Yaki PC",
        priority=1,
    )

    assert "1224776-sb1.extforms.netsuite.com" in url
    assert f"script={DEFAULT_SCRIPT_ID}" in url
    assert f"deploy={DEFAULT_DEPLOY_ID}" in url
    assert "compid=1224776-sb1" in url
    assert "mac=62%3A39%3AA2%3A0D%3AA4%3AB5" in url or "mac=62:39:A2:0D:A4:B5" in url or "62%3A39%3AA2%3A0D%3AA4%3AB5" in url
    assert "ip=192.168.10.115" in url
    assert "port=5050" in url
    assert "name=Miami+Yaki+PC" in url
    assert "priority=1" in url
    assert "url=http%3A%2F%2F192.168.10.115%3A5050" in url


def test_build_suitelet_sync_url_with_placeholders():
    template = (
        "https://<ACCOUNT_ID>.extforms.netsuite.com/app/site/hosting/scriptlet.nl"
        "?script=customscript_lpui_sl_server_sync&deploy=customdeploy_lpui_sl_server_sync"
        "&compid=<ACCOUNT_ID>&h=<HASH>"
    )
    url = build_suitelet_sync_url(
        base_url=template,
        account_id="1224776-sb1",
        compid="1224776-sb1",
        hash_val="SecretHash123",
        mac="62-39-A2-0D-A4-B5",
        ip="192.168.1.11",
        port=5050,
    )

    assert "<ACCOUNT_ID>" not in url
    assert "<HASH>" not in url
    assert "1224776-sb1.extforms.netsuite.com" in url
    assert "h=SecretHash123" in url
    assert "mac=62%3A39%3AA2%3A0D%3AA4%3AB5" in url
    assert "ip=192.168.1.11" in url
    assert "port=5050" in url


def test_mask_url_sensitive_params():
    url = "https://1224776-sb1.extforms.netsuite.com/scriptlet.nl?compid=1224776-sb1&h=ABC123456XYZ&mac=AA:BB:CC:DD:EE:FF"
    masked = mask_url_sensitive_params(url)
    assert "h=ABC***XYZ" in masked
    assert "ABC123456XYZ" not in masked
    assert "mac=AA%3ABB%3ACC%3ADD%3AEE%3AFF" in masked or "mac=AA:BB:CC:DD:EE:FF" in masked


def test_suitelet_sync_manager_sync_now_success():
    cfg = Config()
    cfg.set("suitelet_sync_enabled", True)
    cfg.set("suitelet_account_id", "1224776-sb1")
    cfg.set("suitelet_sync_hash", "TestHash123")

    mock_info = {
        "ip": "192.168.1.20",
        "mac": "00:11:22:33:44:55",
        "port": 5050,
        "name": "Test Server",
        "priority": 1,
    }
    mgr = SuiteletSyncManager(config=cfg, get_server_info=lambda: mock_info)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps({"success": True, "record_id": 99}).encode("utf-8")

    with patch("app.suitelet_sync.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        res = mgr.sync_now()

    assert res["success"] is True
    assert res["status_code"] == 200
    assert res["response"] == {"success": True, "record_id": 99}
    assert res["mac"] == "00:11:22:33:44:55"
    assert mgr.get_status()["last_sync"]["success"] is True


def test_suitelet_sync_manager_sync_now_http_error():
    cfg = Config()
    cfg.set("suitelet_sync_enabled", True)
    cfg.set("suitelet_account_id", "1224776-sb1")
    cfg.set("suitelet_sync_hash", "TestHash123")

    mock_info = {
        "ip": "192.168.1.20",
        "mac": "00:11:22:33:44:55",
        "port": 5050,
    }
    mgr = SuiteletSyncManager(config=cfg, get_server_info=lambda: mock_info)

    http_error = HTTPError("http://dummy", 400, "Bad Request", hdrs=None, fp=BytesIO(b'{"error": "Invalid MAC"}'))

    with patch("app.suitelet_sync.urlopen", side_effect=http_error):
        res = mgr.sync_now()

    assert res["success"] is False
    assert res["status_code"] == 400
    assert "Bad Request" in res["message"]


@pytest.mark.anyio
async def test_server_sync_endpoint():
    import httpx
    from app.server import PrintServer

    mock_sync_result = {
        "success": True,
        "status_code": 200,
        "message": "Server synced successfully",
        "mac": "62:39:A2:0D:A4:B5",
    }
    server = PrintServer(port=8000, on_server_sync=lambda: mock_sync_result)
    transport = httpx.ASGITransport(app=server.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_post = await client.post("/api/server/sync")
        assert resp_post.status_code == 200
        assert resp_post.json() == mock_sync_result

        resp_get = await client.get("/api/server/sync")
        assert resp_get.status_code == 200
        assert resp_get.json() == mock_sync_result
