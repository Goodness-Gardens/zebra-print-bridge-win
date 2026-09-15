#!/usr/bin/env python3
"""
Zebra Print Bridge
Headless print server that receives raw commands and sends them directly to a printer IP.
"""

import argparse
from collections import Counter
import logging
import logging.handlers
import queue
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import Config, get_platform_info
from app.printer_manager import PrinterManager
from app.server import PrintServer
from app.utils import (
    get_hostname,
    get_local_ip,
    is_valid_mac,
    normalize_raw_command,
    normalize_target,
)


def setup_logging(log_level: str = "INFO", log_file: str = None):
    """Configure logging for the application."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        # Rotate at 5 MB, keep 3 backups (~15 MB max total)
        rotating_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handlers.append(rotating_handler)

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger(__name__)


class PrintBridge:
    """Main application for raw printing to Zebra printers."""

    def __init__(self, config: Config = None):
        self.config = config or Config()
        self.logger = logging.getLogger(__name__)

        # Printer manager handles network and local OS printers
        self.printer_manager = PrinterManager(
            scan_network=self.config.scan_network,
            network_timeout=self.config.network_timeout,
            saved_printers=self.config.saved_printers,
            printer_aliases=self.config.get("printer_aliases", {}),
            custom_subnets=self.config.custom_subnets,
            verify_identity=self.config.verify_identity,
            strict_identity=self.config.strict_identity,
            cache_dir=self.config.config_dir,
        )

        self.print_queue = queue.Queue()
        self.job_history: List[Dict] = []
        self.current_jobs: Dict[str, Dict] = {}
        self.max_history = 100
        self.runtime_lock = threading.Lock()
        self.runtime_counters = Counter()

        self.stats = {
            "pending": 0,
            "completed": 0,
            "failed": 0,
        }

        self.server = None
        self.queue_thread = None
        self.running = False
        self._jobs_lock = threading.Lock()

        signal.signal(signal.SIGINT, self._signal_handler)
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, self._signal_handler)

    @staticmethod
    def _format_runtime_details(details: Dict) -> str:
        parts = []
        for key, value in details.items():
            if value is None:
                continue
            text = str(value).replace("\n", "\\n")
            if len(text) > 120:
                text = f"{text[:117]}..."
            parts.append(f"{key}={text}")
        return " ".join(parts)

    def _record_runtime_event(self, event: str, **details):
        with self.runtime_lock:
            self.runtime_counters[event] += 1

        details_text = self._format_runtime_details(details)
        if details_text:
            self.logger.debug("runtime event=%s %s", event, details_text)
        else:
            self.logger.debug("runtime event=%s", event)

    def _get_runtime_snapshot(self) -> Dict:
        with self.runtime_lock:
            counters = dict(sorted(self.runtime_counters.items()))

        return {
            "counters": counters,
        }


    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        self.logger.info("Received shutdown signal (%s). Terminating service.", signum)
        self.stop()
        sys.exit(0)

    def start(self):
        """Start the print bridge service."""
        self._record_runtime_event("bridge_start_requested", port=self.config.port)
        self.logger.info("=" * 50)
        self.logger.info("Zebra Print Bridge - Bridge Mode")
        self.logger.info("=" * 50)

        platform_info = get_platform_info()
        self.logger.info("Platform: %s", platform_info.get("platform", "Unknown"))
        self.logger.info(
            "System: %s %s", platform_info.get("system", "Unknown"), platform_info.get("release", "")
        )
        self.logger.info("Printer Mode: Network IP or Local USB (RAW command).")

        self.logger.info("HTTP server starting on port %d.", self.config.port)
        self.server = PrintServer(
            port=self.config.port,
            on_job_received=self.on_job_received,
            on_status_request=self.get_status,
            on_connection_check=self.check_connection,
            on_logs_clear=self.clear_logs_state,
            on_list_printers=self.list_printers,
            on_refresh_printers=self.refresh_printers,
            on_clear_printer_cache=self.clear_printer_cache,
        )

        self.running = True
        self.queue_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.queue_thread.start()
        self._record_runtime_event("queue_thread_started")

        local_ip = get_local_ip()
        local_mac = get_local_mac(local_ip)
        self.logger.info("=" * 50)
        self.logger.info("Service initialized and listening for requests.")
        self.logger.info(f"  Local URL:   http://localhost:{self.config.port}")
        if local_ip:
            self.logger.info(f"  Network URL: http://{local_ip}:{self.config.port}")
        if local_mac:
            self.logger.info(f"  Network MAC: {local_mac}")
        self.logger.info(f"  Test UI:     http://{local_ip or 'localhost'}:{self.config.port}/test-client")
        self.logger.info("=" * 50)

        self.server.run()

    def stop(self):
        """Stop the print bridge service."""
        self._record_runtime_event("bridge_stop_requested")
        self.logger.info("Zebra Print Bridge service stopping.")
        self.running = False

        if self.server:
            self.server.shutdown()

        if self.queue_thread and self.queue_thread.is_alive():
            self.queue_thread.join(timeout=5)
            self._record_runtime_event("queue_thread_joined", alive=self.queue_thread.is_alive())

        if self.printer_manager:
            self.printer_manager.stop()

        self.logger.info("Zebra Print Bridge service stopped.")

    def _process_queue(self):
        """Process print jobs from the queue."""
        while self.running:
            try:
                job = self.print_queue.get(timeout=1)
            except queue.Empty:
                continue

            target_ip = job.get("printer_ip")
            target_mac = job.get("printer_mac")
            printer_name = job.get("printer_name")
            port = job.get("port", self.printer_manager.DEFAULT_PORT)
            use_local = job.get("use_local", False)
            display_target = (printer_name if use_local else target_ip) or printer_name or target_ip

            self.logger.info(
                "Processing job [%s] from '%s' to target '%s'.",
                job["id"], job["source"], display_target
            )
            self._record_runtime_event(
                "job_processing_started",
                job_id=job["id"],
                source=job["source"],
                printer_ip=target_ip,
                printer_mac=target_mac,
                printer_name=printer_name,
                raw_bytes=job["raw_length"],
            )

            if not target_ip and not printer_name and not use_local:
                self.logger.error("Job [%s] rejected: Missing printer target.", job["id"])
                self._mark_job_failed(job["id"], "printer_mac, printer_ip, or printer_name is required")
                continue

            if target_ip == "test" or printer_name == "test":
                printer = {
                    "name": "Test Printer",
                    "type": "test",
                    "address": "test",
                    "port": port,
                    "status": "simulated",
                }
            elif use_local:
                printer = {
                    "name": printer_name,
                    "type": "local",
                    "status": "ready",
                }
            else:
                printer = {
                    "name": f"Printer @ {target_ip}:{port}" if port != self.printer_manager.DEFAULT_PORT else f"Printer @ {target_ip}",
                    "type": "network",
                    "address": target_ip,
                    "original_target": target_ip,
                    "port": port,
                    "mac": target_mac,
                    "status": "direct",
                }

            self._record_runtime_event(
                "job_dispatch_attempted",
                job_id=job["id"],
                printer_type=printer.get("type"),
                printer_target=display_target,
            )
            success, error = self.printer_manager.send_zpl(printer, job["raw_command"])

            if success:
                if use_local:
                    self.logger.info("Commands successfully sent to local printer: '%s'.", printer.get("name", printer_name))
                else:
                    self.logger.info("Job [%s] completed successfully.", job["id"])
                self._mark_job_completed(job["id"])
            else:
                self.logger.error("Job [%s] failed. Reason: %s", job["id"], error)
                self._mark_job_failed(job["id"], error or "Unknown print error")

    def _mark_job_completed(self, job_id: str):
        """Mark a job as completed."""
        with self._jobs_lock:
            if job_id in self.current_jobs:
                self.current_jobs[job_id]["status"] = "completed"
                self.job_history.append(self.current_jobs[job_id])
                del self.current_jobs[job_id]

                if len(self.job_history) > self.max_history:
                    self.job_history = self.job_history[-self.max_history :]

                self.stats["completed"] += 1
                self.stats["pending"] = max(0, self.stats["pending"] - 1)
        self._record_runtime_event("job_completed", job_id=job_id)

    def _mark_job_failed(self, job_id: str, error: str):
        """Mark a job as failed."""
        with self._jobs_lock:
            if job_id in self.current_jobs:
                self.current_jobs[job_id]["status"] = "failed"
                self.current_jobs[job_id]["error"] = error
                self.job_history.append(self.current_jobs[job_id])
                del self.current_jobs[job_id]

                if len(self.job_history) > self.max_history:
                    self.job_history = self.job_history[-self.max_history :]

                self.stats["failed"] += 1
                self.stats["pending"] = max(0, self.stats["pending"] - 1)
        self._record_runtime_event("job_failed", job_id=job_id, error=error)

    def on_job_received(self, job_data: Dict) -> Dict:
        """Handle incoming print job from the server with multi-tier printer resolution:
        1. Primary Network: printer_mac (Ethernet MAC address, dynamically resolved to current IP)
        2. Secondary Network: printer_ip / printer_host (direct IPv4, hostname, or alias)
        3. Local OS Printer: printer_name (if installed in OS spooler, or resolved network alias)
        4. Fallback Local: OS Default Printer (only if no network target specified)
        """
        printer_mac = normalize_target(job_data.get("printer_mac") or "")
        printer_ip = normalize_target(
            job_data.get("printer_ip") or job_data.get("printer_host") or ""
        )
        printer_name = (job_data.get("printer_name") or "").strip()
        raw_command = normalize_raw_command(job_data.get("raw_command") or "")
        source = (job_data.get("source") or "Web").strip() or "Web"
        job_id = str(job_data.get("id") or "").strip() or datetime.now().strftime(
            "%H%M%S%f"
        )

        is_localhost = job_data.get("is_localhost", False)

        if not raw_command.strip():
            return {"success": False, "message": "raw_command is required"}

        # Auto-detect if printer_ip is actually a MAC address
        if not printer_mac and is_valid_mac(printer_ip):
            printer_mac = printer_ip
            printer_ip = ""

        # Single-pass target resolution
        resolved = self.printer_manager.resolve_target(
            mac=printer_mac or None,
            ip=printer_ip or None,
            printer_name=printer_name or None,
        )

        # If not resolved:
        if not resolved.resolved or (not resolved.ip and not resolved.use_local):
            message = resolved.message or "No valid printer target found. Specify printer_mac, printer_ip, or printer_name."
            self._record_runtime_event(
                "job_rejected_unreachable_printer",
                printer_ip=printer_ip,
                printer_mac=printer_mac,
                source=source,
                error=message,
            )
            return {"success": False, "message": message}

        is_test = (resolved.source == "test" or resolved.ip == "test")

        if not is_test and resolved.verification == "unverifiable" and getattr(self.config, "strict_identity", False):
            msg = f"Identity unverifiable for printer target '{resolved.ip or resolved.mac}' and strict_identity is enabled"
            self._record_runtime_event(
                "job_rejected_strict_identity",
                printer_ip=resolved.ip,
                printer_mac=resolved.mac,
                error=msg,
            )
            return {"success": False, "status_code": 503, "message": msg}

        if resolved.use_local:
            use_local = True
            resolved_target_ip = None
            resolved_target_mac = None
            resolved_port = self.printer_manager.DEFAULT_PORT
            final_printer_name = resolved.printer_name
            self._record_runtime_event(
                "job_resolved_local_printer",
                printer_name=final_printer_name,
                source=source,
            )
        else:
            use_local = False
            resolved_target_ip = resolved.ip
            resolved_target_mac = resolved.mac or printer_mac or None
            resolved_port = resolved.port
            final_printer_name = resolved.printer_name

            if not is_test:
                # Preflight check if not already verified during resolution
                if resolved.verified and resolved.reachable:
                    # Already checked port 9100 and identity
                    pass
                else:
                    conn_ok, conn_msg = self.printer_manager._test_network_connection(
                        resolved.ip,
                        resolved.port,
                        expected_mac=resolved.mac,
                        expected_serial=resolved.serial,
                    )
                    if not conn_ok:
                        self._record_runtime_event(
                            "job_rejected_unreachable_printer",
                            printer_ip=resolved.ip,
                            printer_mac=resolved.mac,
                            source=source,
                            error=conn_msg,
                        )
                        return {"success": False, "message": conn_msg}

        display_target = (final_printer_name if use_local else (resolved_target_mac or resolved_target_ip)) or final_printer_name or resolved_target_ip

        job = {
            "id": job_id,
            "source": source,
            "printer_ip": resolved_target_ip,
            "printer_mac": resolved_target_mac,
            "printer_name": final_printer_name,
            "port": resolved_port,
            "use_local": use_local,
            "raw_command": raw_command,
            "raw_length": len(raw_command),
            "status": "pending",
            "time": datetime.now().strftime("%H:%M:%S"),
            "timestamp": datetime.now(),
        }

        with self._jobs_lock:
            self.current_jobs[job["id"]] = job
        self.print_queue.put(job)
        with self._jobs_lock:
            self.stats["pending"] += 1

        self._record_runtime_event(
            "job_queued",
            job_id=job["id"],
            source=job["source"],
            printer_ip=job.get("printer_ip"),
            printer_mac=job.get("printer_mac"),
            printer_name=job.get("printer_name"),
            raw_bytes=job["raw_length"],
            queue_size=self.print_queue.qsize(),
        )
        self.logger.info(
            "Job [%s] received from '%s' targeting '%s' (Size: %d bytes).",
            job["id"], job["source"], display_target, job["raw_length"]
        )

        return {
            "success": True,
            "job_id": job["id"],
            "message": "Job queued successfully (Test Mode)" if is_test else "Job queued successfully",
        }

    def check_connection(
        self,
        target: Optional[str] = None,
        *,
        printer_name: Optional[str] = None,
        printer_mac: Optional[str] = None,
        is_local: bool = False,
        is_localhost: bool = False,
    ) -> Dict:
        """Check connectivity to a target printer without sending a print job."""
        started_at = perf_counter()

        # Handle explicit local request flag
        if is_local and not printer_name and target:
            printer_name = target
            target = None

        clean_target = normalize_target(target or "")
        clean_mac = normalize_target(printer_mac or "")
        clean_name = (printer_name or "").strip()

        if clean_target and is_valid_mac(clean_target):
            if not clean_mac:
                clean_mac = clean_target
            clean_target = ""

        # Single-pass target resolution
        resolved = self.printer_manager.resolve_target(
            mac=clean_mac or None,
            ip=clean_target or None,
            printer_name=clean_name or None,
        )

        self._record_runtime_event(
            "connection_check_requested",
            printer_ip=resolved.ip,
            printer_mac=resolved.mac,
            printer_name=resolved.printer_name,
        )

        if not resolved.resolved:
            latency_ms = round((perf_counter() - started_at) * 1000, 2)
            message = resolved.message or "printer_ip, printer_mac, or printer_name is required"
            self._record_runtime_event(
                "connection_check_completed",
                printer_ip="",
                printer_mac=resolved.mac,
                printer_type="unknown",
                success=False,
                latency_ms=latency_ms,
            )
            return {
                "success": False,
                "printer_ip": "",
                "printer_mac": resolved.mac,
                "printer_type": "unknown",
                "message": message,
                "latency_ms": latency_ms,
            }

        if resolved.source == "test" or resolved.ip == "test":
            latency_ms = round((perf_counter() - started_at) * 1000, 2)
            self._record_runtime_event(
                "connection_check_completed",
                printer_name="test",
                printer_type="test",
                success=True,
                latency_ms=latency_ms,
            )
            return {
                "success": True,
                "printer_ip": "test",
                "printer_type": "test",
                "message": "Test printer ready (simulated)",
                "latency_ms": latency_ms,
            }

        if resolved.use_local:
            success, message = self.printer_manager.test_local_connection(resolved.printer_name)
            latency_ms = round((perf_counter() - started_at) * 1000, 2)
            self._record_runtime_event(
                "connection_check_completed",
                printer_name=resolved.printer_name,
                printer_type="local",
                success=success,
                latency_ms=latency_ms,
            )
            return {
                "success": success,
                "printer_ip": resolved.printer_name,
                "printer_mac": None,
                "printer_type": "local",
                "message": message,
                "latency_ms": latency_ms,
            }

        if resolved.ip:
            if resolved.source in ("hint", "cache", "arp"):
                latency_ms = round((perf_counter() - started_at) * 1000, 2)
                success = resolved.reachable and not (getattr(self.config, "strict_identity", False) and resolved.verification == "unverifiable")
                self._record_runtime_event(
                    "connection_check_completed",
                    printer_ip=resolved.ip,
                    printer_mac=resolved.mac,
                    printer_type="network",
                    success=success,
                    latency_ms=latency_ms,
                )
                return {
                    "success": success,
                    "printer_ip": resolved.ip,
                    "printer_mac": resolved.mac,
                    "printer_type": "network",
                    "message": resolved.message or f"Connected to {resolved.ip}:{resolved.port}",
                    "latency_ms": latency_ms,
                    "identity": resolved.verification,
                }

            # Only call _test_network_connection when resolution did not verify (manual, alias, dns, mdns)
            success, message = self.printer_manager._test_network_connection(
                resolved.ip,
                resolved.port,
                expected_mac=resolved.mac,
                expected_serial=resolved.serial,
            )
            latency_ms = round((perf_counter() - started_at) * 1000, 2)
            self._record_runtime_event(
                "connection_check_completed",
                printer_ip=resolved.ip,
                printer_mac=resolved.mac,
                printer_type="network",
                success=success,
                latency_ms=latency_ms,
            )
            return {
                "success": success,
                "printer_ip": resolved.ip,
                "printer_mac": resolved.mac,
                "printer_type": "network",
                "message": message,
                "latency_ms": latency_ms,
                "identity": getattr(resolved, "verification", None),
            }

        # Resolution failed
        latency_ms = round((perf_counter() - started_at) * 1000, 2)
        message = resolved.message or "printer_ip, printer_mac, or printer_name is required"
        self._record_runtime_event(
            "connection_check_completed",
            printer_ip="",
            printer_mac=resolved.mac,
            printer_type="unknown",
            success=False,
            latency_ms=latency_ms,
        )
        return {
            "success": False,
            "printer_ip": "",
            "printer_mac": resolved.mac,
            "printer_type": "unknown",
            "message": message,
            "latency_ms": latency_ms,
        }

    def list_network_printers(self) -> List[Dict]:
        """List dynamically discovered Zebra network printers."""
        return self.printer_manager.list_network_printers()

    def list_local_printers(self) -> List[Dict]:
        """List local / OS-installed printers."""
        return self.printer_manager.list_local_printers()

    def list_printers(self) -> List[Dict]:
        """List both dynamically discovered network Zebra printers and local/USB printers."""
        return self.list_network_printers() + self.list_local_printers()

    def refresh_printers(self, clear_cache: bool = True) -> List[Dict]:
        """Clear cache and scan current subnet for fresh printers."""
        self._record_runtime_event("printers_refresh_requested", clear_cache=clear_cache)
        self.printer_manager.scan_subnet(clear_cache=clear_cache)
        return self.list_printers()

    def clear_printer_cache(self) -> Dict:
        """Clear persistent printer cache."""
        self._record_runtime_event("printer_cache_cleared")
        self.printer_manager.clear_cache()
        return {"success": True, "message": "Printer cache cleared successfully"}

    def get_status(self) -> Dict:
        """Get current status for API."""
        with self._jobs_lock:
            all_jobs = list(self.current_jobs.values()) + self.job_history[-20:]
            stats_copy = dict(self.stats)
        all_jobs.sort(key=lambda x: x["timestamp"], reverse=True)
        runtime_snapshot = self._get_runtime_snapshot()

        recent_jobs = [
            {
                "id": j["id"],
                "source": j["source"],
                "printer_ip": j.get("printer_ip"),
                "status": j["status"],
                "time": j["time"],
                "error": j.get("error"),
            }
            for j in all_jobs[:20]
        ]

        server_hostname = get_hostname()
        return {
            "server_running": self.running,
            "mode": "raw_printing",
            "server_hostname": server_hostname,
            "hostname": server_hostname,
            "server_mac": None,
            "pending_jobs": stats_copy["pending"],
            "completed_jobs": stats_copy["completed"],
            "failed_jobs": stats_copy["failed"],
            "recent_jobs": recent_jobs,
            "runtime_counters": runtime_snapshot["counters"],
        }

    def clear_logs_state(self) -> Dict:
        """Clear runtime history and completed/failed job history."""
        with self.runtime_lock:
            self.runtime_counters.clear()

        with self._jobs_lock:
            cleared_job_history = len(self.job_history)
            cleared_completed = self.stats["completed"]
            cleared_failed = self.stats["failed"]

            self.job_history.clear()
            self.stats["completed"] = 0
            self.stats["failed"] = 0
            self.stats["pending"] = len(self.current_jobs)

        return {
            "cleared_job_history": cleared_job_history,
            "cleared_completed_jobs": cleared_completed,
            "cleared_failed_jobs": cleared_failed,
            "pending_jobs": self.stats["pending"],
            "completed_jobs": self.stats["completed"],
            "failed_jobs": self.stats["failed"],
        }


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Zebra Print Bridge - RAW mode"
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=5050,
        help="HTTP server port (default: 5050)",
    )
    parser.add_argument(
        "-c", "--config", type=str, help="Path to configuration directory"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument("--log-file", type=str, help="Path to log file")

    args = parser.parse_args()

    logger = setup_logging(args.log_level, args.log_file)

    config = Config(args.config)
    if args.port and args.port != config.port:
        config.port = args.port

    bridge = PrintBridge(config)

    try:
        bridge.start()
    except KeyboardInterrupt:
        logger.info("Service interrupted by user.")
        bridge.stop()
    except Exception as e:
        logger.error("Service encountered a fatal error: %s", e)
        bridge.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
