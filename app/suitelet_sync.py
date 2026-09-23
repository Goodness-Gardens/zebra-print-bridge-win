"""
NetSuite Suitelet Server Synchronization Module.

Allows Zebra Print Bridge to register / update its server record in NetSuite
via an external Suitelet URL.
"""

from datetime import datetime
import json
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from app import __version__
from app.utils import get_hostname, get_local_ip, get_local_mac, normalize_mac

logger = logging.getLogger(__name__)

DEFAULT_SCRIPT_ID = "customscript_lpui_sl_server_sync"
DEFAULT_DEPLOY_ID = "customdeploy_lpui_sl_server_sync"
DEFAULT_ACCOUNT_ID = "1224776-sb1"
DEFAULT_COMPID = "1224776-sb1"


def mask_url_sensitive_params(url_str: str) -> str:
    """Mask sensitive token/hash parameters ('h', 'ns-at') in logs and status responses for security."""
    if not url_str:
        return ""
    try:
        def _repl(match):
            prefix = match.group(1)
            val = match.group(2)
            if len(val) > 6:
                return f"{prefix}{val[:3]}***{val[-3:]}"
            return f"{prefix}***"

        return re.sub(r"([?&](?:h|ns-at)=)([^&#]+)", _repl, url_str)
    except Exception:
        return url_str


def build_suitelet_sync_url(
    base_url: Optional[str] = None,
    account_id: Optional[str] = DEFAULT_ACCOUNT_ID,
    compid: Optional[str] = None,
    hash_val: Optional[str] = None,
    script_id: Optional[str] = DEFAULT_SCRIPT_ID,
    deploy_id: Optional[str] = DEFAULT_DEPLOY_ID,
    mac: Optional[str] = None,
    ip: Optional[str] = None,
    port: Optional[int] = 5050,
    name: Optional[str] = None,
    url: Optional[str] = None,
    priority: Optional[int] = None,
) -> str:
    """
    Construct the full Suitelet URL with required and optional query parameters.

    Query parameters concatenated:
    - &mac= (mandatory): Physical MAC address (normalized format).
    - &ip= (optional): Current local IP.
    - &port= (optional): Bridge server port.
    - &name= (optional): Server name (URL-encoded with '+' for spaces).
    - &url= (optional): Explicit bridge URL (defaults to http://{ip}:{port}).
    - &priority= (optional): Server priority integer.
    """
    effective_account = (account_id or DEFAULT_ACCOUNT_ID).strip()
    effective_compid = (compid or effective_account).strip()
    effective_hash = (hash_val or "").strip()

    if base_url and base_url.strip():
        raw_url = base_url.strip()
        raw_url = raw_url.replace("<ACCOUNT_ID>", effective_account)
        if effective_hash:
            raw_url = raw_url.replace("<HASH>", effective_hash)
    else:
        raw_url = (
            f"https://{effective_account}.extforms.netsuite.com/app/site/hosting/scriptlet.nl"
            f"?script={script_id or DEFAULT_SCRIPT_ID}"
            f"&deploy={deploy_id or DEFAULT_DEPLOY_ID}"
            f"&compid={effective_compid}"
        )
        if effective_hash:
            raw_url += f"&h={effective_hash}"

    parts = urlsplit(raw_url)
    existing_params = dict(parse_qsl(parts.query, keep_blank_values=True))

    for k, v in list(existing_params.items()):
        if v == "<ACCOUNT_ID>":
            existing_params[k] = effective_account
        elif v == "<HASH>" and effective_hash:
            existing_params[k] = effective_hash

    if mac:
        normalized = normalize_mac(mac)
        existing_params["mac"] = normalized or mac
    if ip:
        existing_params["ip"] = str(ip).strip()
    if port:
        existing_params["port"] = str(port).strip()
    if name:
        existing_params["name"] = str(name).strip()
    if url:
        existing_params["url"] = str(url).strip()
    elif ip and port:
        existing_params["url"] = f"http://{ip}:{port}"
    if priority is not None:
        existing_params["priority"] = str(priority).strip()

    new_query = urlencode(existing_params, quote_via=quote_plus)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


class SuiteletSyncManager:
    """Manages periodic and on-demand synchronization with NetSuite Suitelet."""

    def __init__(self, config=None, get_server_info: Optional[Callable[[], Dict[str, Any]]] = None):
        self.config = config
        self.get_server_info = get_server_info
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._timer_thread: Optional[threading.Thread] = None
        self.last_sync_result: Optional[Dict[str, Any]] = None

    def is_configured(self) -> bool:
        """Check if minimum configuration exists to perform sync."""
        if not self.config:
            return False
        url = (self.config.get("suitelet_sync_url") or "").strip()
        hash_val = (self.config.get("suitelet_sync_hash") or "").strip()
        return bool(url or hash_val)

    def is_enabled(self) -> bool:
        """Check if sync is enabled in configuration."""
        if not self.config:
            return False
        # If explicitly enabled, or if URL/hash is set and suitelet_sync_enabled is not False
        enabled = self.config.get("suitelet_sync_enabled")
        if enabled is not None:
            return bool(enabled)
        return self.is_configured()

    def get_base_url(self) -> str:
        """Return configured base URL or default template."""
        if self.config:
            configured_url = (self.config.get("suitelet_sync_url") or "").strip()
            if configured_url:
                return configured_url
            account_id = (self.config.get("suitelet_account_id") or DEFAULT_ACCOUNT_ID).strip()
            compid = (self.config.get("suitelet_compid") or account_id).strip()
            hash_val = (self.config.get("suitelet_sync_hash") or "").strip()
            script_id = (self.config.get("suitelet_script_id") or DEFAULT_SCRIPT_ID).strip()
            deploy_id = (self.config.get("suitelet_deploy_id") or DEFAULT_DEPLOY_ID).strip()
            return build_suitelet_sync_url(
                account_id=account_id,
                compid=compid,
                hash_val=hash_val,
                script_id=script_id,
                deploy_id=deploy_id,
            )
        return ""

    def sync_now(
        self,
        ip: Optional[str] = None,
        mac: Optional[str] = None,
        port: Optional[int] = None,
        name: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Execute sync call to the Suitelet synchronously and return the result.
        """
        if not self.is_configured() and not (self.config and self.config.get("suitelet_sync_url")):
            res = {
                "success": False,
                "message": "Suitelet sync is not configured (missing URL or hash)",
                "status_code": None,
                "timestamp": datetime.now().isoformat(),
            }
            with self._lock:
                self.last_sync_result = res
            return res

        # Resolve server details
        info = self.get_server_info() if self.get_server_info else {}
        effective_ip = ip or info.get("ip") or get_local_ip()
        effective_mac = mac or info.get("mac") or get_local_mac(effective_ip)
        effective_port = port or info.get("port") or (self.config.port if self.config else 5050)
        
        cfg_name = self.config.get("server_name") if self.config else None
        effective_name = name or info.get("name") or cfg_name or get_hostname()

        cfg_priority = self.config.get("server_priority") if self.config else None
        effective_priority = priority if priority is not None else info.get("priority", cfg_priority)

        if not effective_mac:
            res = {
                "success": False,
                "message": "Physical MAC address could not be resolved (required for Suitelet sync)",
                "status_code": None,
                "timestamp": datetime.now().isoformat(),
            }
            with self._lock:
                self.last_sync_result = res
            logger.warning("NetSuite sync aborted: %s", res["message"])
            return res

        base_url = (self.config.get("suitelet_sync_url") or "").strip() if self.config else ""
        account_id = (self.config.get("suitelet_account_id") or DEFAULT_ACCOUNT_ID).strip() if self.config else DEFAULT_ACCOUNT_ID
        compid = (self.config.get("suitelet_compid") or account_id).strip() if self.config else account_id
        hash_val = (self.config.get("suitelet_sync_hash") or "").strip() if self.config else ""
        script_id = (self.config.get("suitelet_script_id") or DEFAULT_SCRIPT_ID).strip() if self.config else DEFAULT_SCRIPT_ID
        deploy_id = (self.config.get("suitelet_deploy_id") or DEFAULT_DEPLOY_ID).strip() if self.config else DEFAULT_DEPLOY_ID

        full_url = build_suitelet_sync_url(
            base_url=base_url,
            account_id=account_id,
            compid=compid,
            hash_val=hash_val,
            script_id=script_id,
            deploy_id=deploy_id,
            mac=effective_mac,
            ip=effective_ip,
            port=effective_port,
            name=effective_name,
            priority=effective_priority,
        )

        masked_url = mask_url_sensitive_params(full_url)
        logger.info("Executing NetSuite Suitelet server sync -> %s", masked_url)

        req = Request(
            full_url,
            headers={
                "User-Agent": f"ZebraPrintBridge/{__version__}",
                "Accept": "application/json, text/plain, */*",
            },
        )

        started_at = time.perf_counter()
        try:
            with urlopen(req, timeout=12) as response:
                status_code = getattr(response, "status", None) or response.getcode()
                raw_bytes = response.read(65536)  # Cap reading at 64 KB
                duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                text = raw_bytes.decode("utf-8", errors="replace").strip()

                try:
                    payload = json.loads(text)
                except Exception:
                    payload = text

                success = 200 <= status_code < 300
                res = {
                    "success": success,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                    "message": "Server synced successfully with NetSuite Suitelet" if success else f"Suitelet returned HTTP {status_code}",
                    "response": payload,
                    "mac": normalize_mac(effective_mac) or effective_mac,
                    "ip": effective_ip,
                    "port": effective_port,
                    "name": effective_name,
                    "url": masked_url,
                    "timestamp": datetime.now().isoformat(),
                }
                with self._lock:
                    self.last_sync_result = res

                if success:
                    logger.info("NetSuite Suitelet sync OK (HTTP %d in %sms): %s", status_code, duration_ms, text[:150])
                else:
                    logger.warning("NetSuite Suitelet sync non-2xx status (HTTP %d): %s", status_code, text[:150])
                return res

        except HTTPError as http_err:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            body = ""
            try:
                body = http_err.read().decode("utf-8", errors="replace")[:250]
            except Exception:
                pass
            res = {
                "success": False,
                "status_code": http_err.code,
                "duration_ms": duration_ms,
                "message": f"NetSuite Suitelet HTTP error {http_err.code}: {http_err.reason}",
                "response": body,
                "mac": normalize_mac(effective_mac) or effective_mac,
                "url": masked_url,
                "timestamp": datetime.now().isoformat(),
            }
            with self._lock:
                self.last_sync_result = res
            logger.warning("NetSuite Suitelet HTTP error %s: %s (Body: %s)", http_err.code, http_err.reason, body)
            return res

        except URLError as url_err:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            res = {
                "success": False,
                "status_code": None,
                "duration_ms": duration_ms,
                "message": f"Network error connecting to NetSuite Suitelet: {url_err.reason}",
                "url": masked_url,
                "timestamp": datetime.now().isoformat(),
            }
            with self._lock:
                self.last_sync_result = res
            logger.warning("NetSuite Suitelet connection failed: %s", url_err.reason)
            return res

        except Exception as exc:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            res = {
                "success": False,
                "status_code": None,
                "duration_ms": duration_ms,
                "message": f"Unexpected error during NetSuite Suitelet sync: {exc}",
                "url": masked_url,
                "timestamp": datetime.now().isoformat(),
            }
            with self._lock:
                self.last_sync_result = res
            logger.exception("Unexpected error syncing to NetSuite: %s", exc)
            return res

    def start(
        self,
        ip: Optional[str] = None,
        mac: Optional[str] = None,
        port: Optional[int] = None,
    ):
        """
        Start sync operations (initial sync in background + optional periodic timer).
        """
        if not self.is_enabled():
            logger.info("NetSuite Suitelet sync is disabled or unconfigured.")
            return

        self._stop_event.clear()

        # 1. Trigger initial sync in background thread so startup isn't blocked
        def _initial_worker():
            # Small delay to ensure server network socket is bound
            time.sleep(1.0)
            if not self._stop_event.is_set():
                self.sync_now(ip=ip, mac=mac, port=port)

        threading.Thread(target=_initial_worker, daemon=True, name="SuiteletInitialSync").start()

        # 2. Check if periodic sync / heartbeat is configured
        interval = 0
        if self.config:
            interval = int(self.config.get("suitelet_sync_interval_seconds", 0) or 0)

        if interval > 0:
            logger.info("NetSuite Suitelet periodic heartbeat enabled (every %d seconds)", interval)

            def _loop_worker():
                while not self._stop_event.wait(interval):
                    try:
                        self.sync_now(ip=ip, mac=mac, port=port)
                    except Exception as err:
                        logger.warning("Error in Suitelet sync loop: %s", err)

            self._timer_thread = threading.Thread(target=_loop_worker, daemon=True, name="SuiteletSyncLoop")
            self._timer_thread.start()

    def stop(self):
        """Stop any running background sync timer."""
        self._stop_event.set()
        if self._timer_thread and self._timer_thread.is_alive():
            self._timer_thread.join(timeout=3)
            self._timer_thread = None

    def get_status(self) -> Dict[str, Any]:
        """Return the current sync status for API and UI consumers."""
        with self._lock:
            last_result = dict(self.last_sync_result) if self.last_sync_result else None

        return {
            "enabled": self.is_enabled(),
            "configured": self.is_configured(),
            "account_id": (self.config.get("suitelet_account_id") or DEFAULT_ACCOUNT_ID) if self.config else DEFAULT_ACCOUNT_ID,
            "compid": (self.config.get("suitelet_compid") or DEFAULT_COMPID) if self.config else DEFAULT_COMPID,
            "interval_seconds": (self.config.get("suitelet_sync_interval_seconds") or 0) if self.config else 0,
            "server_name": (self.config.get("server_name") or get_hostname()) if self.config else get_hostname(),
            "server_priority": self.config.get("server_priority") if self.config else None,
            "base_url": mask_url_sensitive_params(self.get_base_url()),
            "last_sync": last_result,
        }
