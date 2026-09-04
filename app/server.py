"""
FastAPI server for receiving raw printer commands.
"""

from collections import Counter
import logging
import threading
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Callable, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import __version__
from .config import get_platform_info
from .utils import (
    get_local_ip,
    get_hostname,
    is_valid_target,
    normalize_target,
    normalize_raw_command,
)

logger = logging.getLogger(__name__)


class PrintJob(BaseModel):
    """Model for JSON print job requests (supports legacy and new payloads)."""

    printer_ip: Optional[str] = None
    printer_host: Optional[str] = None  # Network hostname or IP alias
    printer_name: Optional[str] = None  # Local/USB printer name installed in the OS
    raw_command: Optional[str] = None
    zpl: Optional[str] = None  # Legacy field name used by existing clients
    source: Optional[str] = "Web API"
    id: Optional[str] = None
    dpi: Optional[int] = None
    label_size: Optional[Dict[str, float]] = None


class PrintResponse(BaseModel):
    """Response model for print requests"""

    success: bool
    job_id: Optional[str] = None
    message: Optional[str] = None
    server_hostname: Optional[str] = None
    hostname: Optional[str] = None  # Backward-compatible alias


class ConnectionCheckResponse(BaseModel):
    """Response model for printer connection checks."""

    success: bool
    printer_ip: str
    printer_type: str
    message: str
    latency_ms: Optional[float] = None
    server_hostname: Optional[str] = None
    hostname: Optional[str] = None  # Backward-compatible alias


class PrintServer:
    def __init__(
        self,
        port: int = 5050,
        on_job_received: Callable = None,
        on_status_request: Callable = None,
        on_connection_check: Callable = None,
        on_logs_clear: Callable = None,
        on_list_printers: Callable = None,
    ):
        self.port = port
        self.on_job_received = on_job_received
        self.on_status_request = on_status_request
        self.on_connection_check = on_connection_check
        self.on_logs_clear = on_logs_clear
        self.on_list_printers = on_list_printers
        self.is_running = False
        self.start_time = None
        self.resource_dir = Path(__file__).resolve().parent.parent / "resources"
        self.usage_lock = threading.Lock()
        self.usage_counters = Counter()
        self.route_hits = Counter()
        self.app = self._create_app()
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._local_ip: Optional[str] = None  # cached LAN IP for local-client detection

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="Zebra Print Bridge API",
            description="API for sending raw commands to network and local printers",
            version=__version__,
        )

        # Allow open access for external clients
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.middleware("http")
        async def usage_logging_middleware(request: Request, call_next):
            started_at = perf_counter()
            client_host = request.client.host if request.client else None

            try:
                response = await call_next(request)
            except Exception as exc:
                self._record_usage(
                    "http_request_error",
                    method=request.method,
                    path=request.url.path,
                    client=client_host,
                    duration_ms=round((perf_counter() - started_at) * 1000, 2),
                    error=type(exc).__name__,
                )
                raise

            with self.usage_lock:
                self.route_hits[f"{request.method} {request.url.path}"] += 1

            log_request = request.url.path not in ("/status", "/health")
            self._record_usage(
                "http_request_completed",
                log=log_request,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                client=client_host,
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )
            return response

        @app.get("/")
        async def root(request: Request):
            """Default entrypoint (HTML for browsers, JSON for clients)."""
            if self._wants_html(request):
                self._record_usage("root_dashboard_redirected")
                return RedirectResponse(url="/dashboard", status_code=307)
            self._record_usage("root_json_served")
            server_hostname = get_hostname()
            return {
                "status": "running",
                "service": "Zebra Print Bridge",
                "version": __version__,
                "server_hostname": server_hostname,
                "hostname": server_hostname,
                "mode": "raw_printing",
            }

        @app.get("/status")
        async def get_status():
            """Get current server status and queue metrics."""
            self._record_usage("status_requested", log=False)
            server_hostname = get_hostname()
            if self.on_status_request:
                status = self.on_status_request()
                status["server_hostname"] = server_hostname
                status["hostname"] = server_hostname
                status["uptime"] = self._get_uptime()
                status["usage"] = self._get_usage_snapshot()
                return status
            return {
                "status": "running",
                "server_hostname": server_hostname,
                "hostname": server_hostname,
                "uptime": self._get_uptime(),
                "usage": self._get_usage_snapshot(),
            }

        @app.get("/health")
        async def health_check():
            """Simple health check — intentionally lightweight."""
            return {"healthy": True}

        @app.get("/info")
        async def get_info():
            """Get server info including network IP for remote access."""
            self._record_usage("info_requested")
            local_ip = self._get_local_ip()
            platform_info = get_platform_info()
            server_hostname = get_hostname()

            return {
                "service": "Zebra Print Bridge",
                "version": __version__,
                "server_hostname": server_hostname,
                "hostname": server_hostname,
                "mode": "raw_printing",
                "port": self.port,
                "server_port": self.port,
                "server_ip": local_ip,
                "network_ip": local_ip,
                "server_url": f"{local_ip}:{self.port}" if local_ip else f"localhost:{self.port}",
                "server_url_host": f"{server_hostname}:{self.port}",
                "local_url": f"http://localhost:{self.port}",
                "network_url": f"http://{local_ip}:{self.port}" if local_ip else None,
                "platform": platform_info.get("platform", "Windows"),
                "uptime": self._get_uptime(),
                "required_fields": {
                    "json_print": [
                        "printer_ip (IPv4 or 'test') OR printer_name (local OS printer)",
                        "raw_command (or legacy field 'zpl')",
                    ],
                    "raw_print": ["printer_ip (query, IPv4 or 'test')", "raw body"],
                },
                "endpoints": {
                    "print": "/print (POST JSON)",
                    "print_raw": "/print/raw (POST plain text)",
                    "printers": "/printers (GET) — list OS-installed printers",
                    "connection": "/connection?printer_ip=<IPv4|hostname|test>&printer_name=<name> (GET)",
                    "status": "/status (GET)",
                    "health": "/health (GET)",
                    "info": "/info (GET)",
                    "logs_clear": "/logs/clear (POST)",
                    "dashboard": "/dashboard (GET)",
                    "test_client": "/test-client (GET)",
                },
            }

        @app.get("/connection", response_model=ConnectionCheckResponse)
        async def check_connection(
            request: Request,
            printer_ip: str = "",
            ip: Optional[str] = None,
            printer_host: Optional[str] = None,
            host: Optional[str] = None,
            printer_name: Optional[str] = None,
        ):
            """Check printer connectivity without sending a print job."""
            if not self.on_connection_check:
                raise HTTPException(
                    status_code=500, detail="Connection check handler not configured"
                )

            # If printer_name is provided, delegate to the callback directly
            if printer_name and printer_name.strip():
                self._record_usage("connection_check_requested", printer_name=printer_name.strip())
                try:
                    result = self.on_connection_check(
                        target=None,
                        printer_name=printer_name.strip(),
                        is_localhost=True
                    )
                    if not result.get("success", False):
                        raise HTTPException(
                            status_code=503,
                            detail=result.get("message", "Unable to reach printer"),
                        )
                    return ConnectionCheckResponse(
                        success=result["success"],
                        printer_ip=result.get("printer_ip", printer_name.strip()),
                        printer_type=result.get("printer_type", "local"),
                        message=result.get("message", "Connection check completed"),
                        latency_ms=result.get("latency_ms"),
                    )
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(status_code=500, detail=str(e))

            target = self._normalize_target(printer_ip or ip or printer_host or host or "")
            self._record_usage("connection_check_requested", printer_ip=target)

            client_host = request.client.host if request.client else None
            is_localhost = self._is_local_client(client_host)

            if not self._is_valid_target(target):
                # Only auto-resolve as printer name for localhost requests
                if is_localhost:
                    self._record_usage(
                        "connection_check_auto_resolve_as_name",
                        original_target=target,
                    )
                    try:
                        result = self.on_connection_check(
                            target=None,
                            printer_name=target,
                            is_localhost=True
                        )
                        if not result.get("success", False):
                            raise HTTPException(
                                status_code=503,
                                detail=result.get("message", "Unable to reach printer"),
                            )
                        server_hostname = get_hostname()
                        return ConnectionCheckResponse(
                            success=result["success"],
                            printer_ip=result.get("printer_ip", target),
                            printer_type=result.get("printer_type", "local"),
                            message=result.get("message", "Connection check completed"),
                            latency_ms=result.get("latency_ms"),
                            server_hostname=server_hostname,
                            hostname=server_hostname,
                        )
                    except HTTPException:
                        raise
                    except Exception as e:
                        raise HTTPException(status_code=500, detail=str(e))
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="Valid printer_ip or printer_host query parameter is required for remote requests (IPv4, hostname, or 'test')",
                    )

            try:
                result = self.on_connection_check(target=target, printer_name=None, is_localhost=is_localhost)
                if not result.get("success", False):
                    self._record_usage(
                        "connection_check_rejected_unreachable_target",
                        printer_ip=target,
                        message=result.get("message"),
                        printer_type=result.get("printer_type"),
                        latency_ms=result.get("latency_ms"),
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=result.get("message", "Unable to reach printer"),
                    )
                self._record_usage(
                    "connection_check_completed",
                    printer_ip=target,
                    success=result.get("success", False),
                    printer_type=result.get("printer_type"),
                    latency_ms=result.get("latency_ms"),
                )
                server_hostname = get_hostname()
                return ConnectionCheckResponse(
                    success=result.get("success", False),
                    printer_ip=result.get("printer_ip", target),
                    printer_type=result.get("printer_type", "unknown"),
                    message=result.get("message", "Connection check completed"),
                    latency_ms=result.get("latency_ms"),
                    server_hostname=server_hostname,
                    hostname=server_hostname,
                )
            except HTTPException:
                raise
            except Exception as e:
                self._record_usage(
                    "connection_check_failed",
                    printer_ip=target,
                    error=type(e).__name__,
                )
                logger.error("Error checking printer connection: %s", e)
                raise HTTPException(status_code=500, detail=str(e))

        # CORS preflight is handled automatically by CORSMiddleware

        @app.post("/print", response_model=PrintResponse)
        async def print_label(job: PrintJob, request: Request):
            """Send a raw command using JSON payload."""
            if not self.on_job_received:
                raise HTTPException(status_code=500, detail="Print handler not configured")

            printer_name = (job.printer_name or "").strip() or None
            printer_ip = self._normalize_target(job.printer_ip or job.printer_host or "") or None
            raw_command = self._normalize_raw_command(job.raw_command or job.zpl or "")

            if not printer_name and not printer_ip:
                raise HTTPException(
                    status_code=400,
                    detail="Either printer_ip (or printer_host) or printer_name is required",
                )

            if not raw_command.strip():
                raise HTTPException(status_code=400, detail="raw_command is required")

            client_host = request.client.host if request.client else None
            is_localhost = self._is_local_client(client_host)

            # Auto-detect: if printer_ip is not a valid IP/hostname and request
            # comes from localhost, treat it as a local printer name.
            if printer_ip and not self._is_valid_target(printer_ip):
                if is_localhost:
                    self._record_usage(
                        "json_print_auto_resolve_as_name",
                        original_printer_ip=printer_ip,
                    )
                    # Move the non-network value to printer_name (keep printer_ip empty)
                    if not printer_name:
                        printer_name = printer_ip
                    printer_ip = None
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid printer_ip. Use IPv4 address, hostname, or 'test'",
                    )

            self._record_usage(
                "json_print_requested",
                printer_name=printer_name,
                printer_ip=printer_ip,
                source=job.source,
                request_id=job.id,
                raw_bytes=len(raw_command),
                is_localhost=is_localhost
            )

            try:
                result = self.on_job_received(
                    {
                        "printer_name": printer_name,
                        "printer_ip": printer_ip,
                        "raw_command": raw_command,
                        "dpi": job.dpi,
                        "label_size": job.label_size,
                        "source": job.source,
                        "id": job.id,
                        "is_localhost": is_localhost,
                    }
                )
                if not result.get("success", False):
                    msg = result.get("message", "Print failed")
                    status_code = 404 if "not found" in msg.lower() else 503
                    raise HTTPException(status_code=status_code, detail=msg)
                server_hostname = get_hostname()
                return PrintResponse(
                    success=result.get("success", False),
                    job_id=result.get("job_id"),
                    message=result.get("message", "Job queued successfully"),
                    server_hostname=server_hostname,
                    hostname=server_hostname,
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.error("Error processing print job: %s", e)
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/print/raw")
        async def print_raw(request: Request):
            """Send raw command from plain text body with printer IP in query params."""
            if not self.on_job_received:
                raise HTTPException(status_code=500, detail="Print handler not configured")

            body = await request.body()
            raw_command = self._normalize_raw_command(body.decode("utf-8"))
            if not raw_command.strip():
                self._record_usage("raw_print_rejected_empty_payload")
                raise HTTPException(status_code=400, detail="Empty raw command")

            printer_ip = self._normalize_target(
                request.query_params.get("printer_ip")
                or request.query_params.get("printer_host")
                or request.query_params.get("ip")
                or request.query_params.get("host")
                or ""
            )
            source = request.query_params.get("source", "Raw API")
            request_id = request.query_params.get("id")

            client_host = request.client.host if request.client else None
            is_localhost = self._is_local_client(client_host)

            self._record_usage(
                "raw_print_requested",
                printer_ip=printer_ip,
                source=source,
                request_id=request_id,
                raw_bytes=len(raw_command),
                is_localhost=is_localhost
            )

            if not self._is_valid_target(printer_ip):
                # Only auto-resolve as printer name for localhost requests
                if is_localhost:
                    self._record_usage(
                        "raw_print_auto_resolve_as_name",
                        original_printer_ip=printer_ip,
                    )
                    try:
                        result = self.on_job_received(
                            {
                                "printer_name": printer_ip,
                                "raw_command": raw_command,
                                "source": source,
                                "id": request_id,
                                "is_localhost": True,
                            }
                        )
                        if not result.get("success", False):
                            status_code = 404 if "not found" in result.get("message", "").lower() else 503
                            raise HTTPException(
                                status_code=status_code,
                                detail=result.get("message", "Print failed"),
                            )
                        server_hostname = get_hostname()
                        return {
                            "success": result.get("success", False),
                            "job_id": result.get("job_id"),
                            "message": result.get("message", "Job queued successfully"),
                            "server_hostname": server_hostname,
                            "hostname": server_hostname,
                        }
                    except HTTPException:
                        raise
                    except Exception as e:
                        self._record_usage("raw_print_failed", printer_name=printer_ip, error=type(e).__name__)
                        logger.error("Error processing raw print job: %s", e)
                        raise HTTPException(status_code=500, detail=str(e))
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid printer_ip. Use IPv4 address, hostname, or 'test'",
                    )

            try:
                result = self.on_job_received(
                    {
                        "printer_ip": printer_ip,
                        "raw_command": raw_command,
                        "source": source,
                        "id": request_id,
                        "is_localhost": is_localhost,
                    }
                )
                if not result.get("success", False):
                    self._record_usage(
                        "raw_print_rejected_unreachable_target",
                        printer_ip=printer_ip,
                        message=result.get("message"),
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=result.get("message", "Unable to reach printer"),
                    )
                self._record_usage(
                    "raw_print_enqueued",
                    printer_ip=printer_ip,
                    success=result.get("success", False),
                    job_id=result.get("job_id"),
                )
                server_hostname = get_hostname()
                return {
                    "success": result.get("success", False),
                    "job_id": result.get("job_id"),
                    "message": result.get("message", "Job queued successfully"),
                    "server_hostname": server_hostname,
                    "hostname": server_hostname,
                }
            except HTTPException:
                raise
            except Exception as e:
                self._record_usage("raw_print_failed", printer_ip=printer_ip, error=type(e).__name__)
                logger.error("Error processing raw print job: %s", e)
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/dashboard", response_class=HTMLResponse)
        async def dashboard():
            """Dashboard alias for the test client."""
            self._record_usage("dashboard_requested")
            return self._get_test_client_html()

        @app.get("/test-client", response_class=HTMLResponse)
        async def test_client():
            """Web test client."""
            self._record_usage("test_client_requested")
            return self._get_test_client_html()

        @app.get("/logs")
        async def get_logs():
            """Get recent activity logs."""
            if self.on_status_request:
                status = self.on_status_request()
                return {
                    "recent_jobs": status.get("recent_jobs", []),
                    "pending": status.get("pending_jobs", 0),
                    "completed": status.get("completed_jobs", 0),
                    "failed": status.get("failed_jobs", 0),
                    "usage": self._get_usage_snapshot(),
                }
            return {"logs": [], "usage": self._get_usage_snapshot()}

        @app.post("/logs/clear")
        async def clear_logs():
            """Clear runtime history and truncate active log files."""
            cleared_runtime = {}
            if self.on_logs_clear:
                cleared_runtime = self.on_logs_clear() or {}

            self._clear_usage_tracking()
            cleared_log_files = self._truncate_log_files()

            return {
                "success": True,
                "message": "Logs cleared successfully",
                "cleared_log_files": cleared_log_files,
                "cleared_job_history": cleared_runtime.get("cleared_job_history", 0),
                "pending_jobs": cleared_runtime.get("pending_jobs", 0),
                "completed_jobs": cleared_runtime.get("completed_jobs", 0),
                "failed_jobs": cleared_runtime.get("failed_jobs", 0),
            }

        @app.get("/printers")
        async def list_printers():
            """List both network and local OS-installed printers."""
            self._record_usage("printers_list_requested")
            if not self.on_list_printers:
                raise HTTPException(
                    status_code=500, detail="Printer listing handler not configured"
                )
            try:
                printers = self.on_list_printers()
                server_hostname = get_hostname()
                network_printers = [p for p in printers if p.get("type") == "network"]
                local_printers = [p for p in printers if p.get("type") == "local"]
                return {
                    "server_hostname": server_hostname,
                    "hostname": server_hostname,
                    "count": len(printers),
                    "printers": printers,
                    "network_printers": network_printers,
                    "local_printers": local_printers,
                }
            except Exception as e:
                logger.error("Error listing printers: %s", e)
                raise HTTPException(status_code=500, detail=str(e))

        return app

    @staticmethod
    def _format_usage_details(details: Dict) -> str:
        parts = []
        for key, value in details.items():
            if value is None:
                continue
            text = str(value).replace("\n", "\\n")
            if len(text) > 120:
                text = f"{text[:117]}..."
            parts.append(f"{key}={text}")
        return " ".join(parts)

    def _record_usage(self, event: str, log: bool = True, **details):
        with self.usage_lock:
            self.usage_counters[event] += 1

        # We keep only aggregated counter data (reduced memory bloat)

        if log:
            details_text = self._format_usage_details(details)
            if details_text:
                logger.info("usage event=%s %s", event, details_text)
            else:
                logger.info("usage event=%s", event)

    def _get_usage_snapshot(self) -> Dict:
        with self.usage_lock:
            counters = dict(sorted(self.usage_counters.items()))
            route_hits = dict(sorted(self.route_hits.items()))

        return {
            "counters": counters,
            "route_hits": route_hits,
        }

    def _clear_usage_tracking(self) -> Dict:
        with self.usage_lock:
            self.usage_counters.clear()
            self.route_hits.clear()
        return {}

    @staticmethod
    def _truncate_log_files() -> list[str]:
        cleared_files = []
        seen_paths = set()

        for active_logger in (logging.getLogger(), logger):
            for handler in active_logger.handlers:
                base_filename = getattr(handler, "baseFilename", None)
                if not base_filename or base_filename in seen_paths:
                    continue

                seen_paths.add(base_filename)
                try:
                    handler.acquire()
                    try:
                        if getattr(handler, "stream", None):
                            handler.flush()
                            handler.stream.seek(0)
                            handler.stream.truncate(0)
                            handler.flush()
                        else:
                            Path(base_filename).write_text("", encoding="utf-8")
                    finally:
                        handler.release()
                    cleared_files.append(str(Path(base_filename)))
                except Exception as exc:
                    logger.error("Failed to clear log file %s: %s", base_filename, exc)

        return cleared_files

    @classmethod
    def _is_valid_target(cls, target: str) -> bool:
        """Accept IPv4 targets, network hostnames, optional port, and the special test destination."""
        return is_valid_target(target)

    def _is_local_client(self, client_host: Optional[str]) -> bool:
        """Check if the request originates from the local machine.

        Matches loopback addresses, hostname, AND the machine's own LAN IP so that
        a browser connecting via the network URL or hostname (e.g. http://192.168.1.20:5050 or http://my-pc:5050)
        is still treated as a local request.
        """
        if client_host in ("127.0.0.1", "::1", "localhost", None):
            return True
        # Cache the LAN IP on first call
        if self._local_ip is None:
            self._local_ip = get_local_ip() or ""
        hostname = get_hostname()
        if client_host == hostname or (hostname and client_host == hostname.split(".")[0]):
            return True
        return client_host == self._local_ip

    @staticmethod
    def _normalize_target(target: str) -> str:
        return normalize_target(target)

    @staticmethod
    def _normalize_raw_command(command: str) -> str:
        return normalize_raw_command(command)

    def _wants_html(self, request: Request) -> bool:
        """Heuristic to return HTML for browsers and JSON for API clients."""
        accept = (request.headers.get("accept") or "").lower()
        if "text/html" in accept:
            return True
        if "application/json" in accept:
            return False
        user_agent = request.headers.get("user-agent") or ""
        if "Mozilla" in user_agent:
            return True
        return False

    def _get_test_client_html(self) -> str:
        """Load the test client HTML from resources."""
        path = self.resource_dir / "test-client.html"
        try:
            content = path.read_text(encoding="utf-8")
            self._record_usage("test_client_html_loaded", bytes=len(content))
            return content
        except Exception as e:
            self._record_usage("test_client_html_load_failed", error=type(e).__name__)
            logger.error("Unable to read test client HTML: %s", e)
            return "<h1>Test client not available</h1>"

    def _get_local_ip(self) -> Optional[str]:
        """Get the local network IP address."""
        ip = get_local_ip()
        if ip:
            self._record_usage("local_ip_lookup_succeeded", ip=ip)
        else:
            self._record_usage("local_ip_lookup_failed")
        return ip

    def _get_uptime(self) -> str:
        """Get server uptime as a human-readable string."""
        if not self.start_time:
            return "Unknown"

        delta = datetime.now() - self.start_time
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")

        return " ".join(parts)

    def run(self):
        """Run the server."""
        self.is_running = True
        self.start_time = datetime.now()
        self._record_usage("server_run_invoked", port=self.port)
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            log_config=None,
        )
        self._uvicorn_server = uvicorn.Server(config)
        try:
            self._uvicorn_server.run()
        except Exception as e:
            logger.error("Server encountered a fatal error: %s", e)
        finally:
            self.is_running = False

    def shutdown(self):
        """Signal Uvicorn to exit gracefully."""
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True


if __name__ == "__main__":
    def mock_handler(job):
        print(f"Received job: {job}")
        return {"success": True, "job_id": "test-123"}


    server = PrintServer(port=5050, on_job_received=mock_handler)
    print("Starting server on http://localhost:5050")
    server.run()
