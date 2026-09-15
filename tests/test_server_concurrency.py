import asyncio
import time
from time import perf_counter
import pytest
import httpx

from app.server import PrintServer


@pytest.mark.anyio
async def test_server_concurrency_health_not_blocked_by_connection():
    """Verify that a slow /connection check (running in threadpool) does not block /health."""
    def slow_connection_check(target=None, printer_name=None, is_localhost=True):
        time.sleep(2.0)
        return {"success": True, "printer_ip": "192.168.1.50", "printer_type": "network"}

    server = PrintServer(port=8000, on_connection_check=slow_connection_check)
    transport = httpx.ASGITransport(app=server.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        conn_task = asyncio.create_task(client.get("/connection?printer_ip=192.168.1.50"))

        # Wait briefly to ensure /connection has begun execution
        await asyncio.sleep(0.1)

        t0 = perf_counter()
        health_resp = await client.get("/health")
        health_duration = perf_counter() - t0

        assert health_resp.status_code == 200
        assert health_resp.json() == {"healthy": True}
        assert health_duration < 0.2, f"/health took {health_duration:.3f}s, expected < 0.2s"

        conn_resp = await conn_task
        assert conn_resp.status_code == 200


@pytest.mark.anyio
async def test_server_concurrency_health_not_blocked_by_raw_print():
    """Verify that a slow /print/raw job (running via run_in_threadpool) does not block /health."""
    def slow_job_received(job_data):
        time.sleep(1.0)
        return {"success": True, "job_id": "test-123"}

    server = PrintServer(port=8000, on_job_received=slow_job_received)
    transport = httpx.ASGITransport(app=server.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print_task = asyncio.create_task(client.post("/print/raw?printer_ip=192.168.1.50", content="^XA^XZ"))

        await asyncio.sleep(0.1)

        t0 = perf_counter()
        health_resp = await client.get("/health")
        health_duration = perf_counter() - t0

        assert health_resp.status_code == 200
        assert health_resp.json() == {"healthy": True}
        assert health_duration < 0.2, f"/health took {health_duration:.3f}s, expected < 0.2s"

        print_resp = await print_task
        assert print_resp.status_code == 200
