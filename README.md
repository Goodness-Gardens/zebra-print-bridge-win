# Zebra Print Bridge

A lightweight desktop application that receives ZPL II (or any raw printer language) commands from
web applications and forwards them to Zebra printers over TCP/IP or to printers installed in the operating system.

> **Mode: Network & Local RAW command.**
> Every print request carries a `raw_command` plus a target: `printer_mac` (recommended), `printer_ip` / `printer_host`, or `printer_name`.
> If no target is given, the job goes to the OS default printer.
> Network printers are discovered passively and remembered by device identity (MAC / serial), so DHCP address changes do not break existing integrations.

## Features

- **Desktop GUI**: Light-themed control panel (CustomTkinter) with server controls, live activity log, and a Devices & Printers view
- **Embedded HTTP Server**: FastAPI + Uvicorn on port 5050 (configurable), CORS open for browser clients
- **MAC-based Printer Resolution**: Resolves `printer_mac` through hint IP → identity cache → OS ARP table → subnet scan; immune to DHCP IP changes
- **Identity Verification**: Confirms that the device answering at an IP is the expected printer via SNMP (`ifPhysAddress`, Zebra serial) and ARP. Tri-state result (`match` / `mismatch` / `unverifiable`) with optional strict mode
- **Passive Network Discovery**: Background scanner probes port 9100 with a TCP connect only (never sends bytes), enriches names via reverse DNS, HTTP title and SNMP, and persists the results
- **Persistent Printer Cache (v2)**: Indexed by device identity, with automatic migration from the v1 cache
- **Subnet Management**: Auto-detects interface subnets on Windows, macOS and Linux; custom/VPN subnets via config or API
- **Local OS Printers**: Sends RAW jobs to printers in the Windows spooler (win32print) or CUPS (macOS/Linux); OS default printer fallback
- **Hostname & Alias Resolution**: DNS, mDNS (`.local`), discovered names/hostnames/serials, and user-defined aliases
- **Web Test Client**: Built-in browser UI at `/dashboard`
- **Queued Processing** with connection preflight: unreachable printers are rejected before enqueueing
- **NetSuite Suitelet Server Synchronization**: Automatically registers or updates the server record (MAC, IP, port, name, priority) via an external NetSuite Suitelet on startup, on demand, or through periodic heartbeat
- **Auto-Updater**: Checks `version.json` on GitHub and offers one-click download and install
- **Automated Releases**: GitHub Actions builds the PyInstaller bundle, compiles the Inno Setup installer, tags and publishes the release

## Requirements

### For Windows Installer (End Users)

- Windows 10/11 (64-bit)
- Zebra printer reachable over TCP/IP (port 9100) and/or installed in Windows

### For Development

- Python 3.9+ (CI builds with Python 3.12)
- Runtime dependencies in `requirements.txt` (`pywin32` is installed on Windows only)
- Test dependencies in `requirements-dev.txt` (`pytest`, `httpx`)

## Installation

### Option A: Windows Installer (Recommended for End Users)

1. Download the latest `ZebraBridgeSetup_<version>.exe` from [Releases](https://github.com/Goodness-Gardens/zebra-print-bridge-win/releases).
2. Run the installer and follow the wizard (optional tasks: desktop icon, run at Windows startup).
3. Launch **Zebra Print Bridge** from the Start Menu or Desktop shortcut.

### Option B: Source Code (Development)

```bash
# Clone the repository
git clone https://github.com/Goodness-Gardens/zebra-print-bridge-win.git
cd zebra-print-bridge-win

# Create virtual environment and install dependencies
python -m venv venv
.\venv\Scripts\activate            # Windows
# source venv/bin/activate         # macOS / Linux
pip install -r requirements.txt
pip install -r requirements-dev.txt   # optional, for running the tests
```

### Running the Tests

```bash
python -m pytest -q
```

The suite is isolated from the real user config directory (see `tests/conftest.py`), mocks all network and SNMP calls, and never sends anything to a physical printer.

## Usage

### Desktop GUI

```bash
# From source (Windows)
.\venv\Scripts\python.exe run_gui.py
# macOS / Linux
python run_gui.py
```

Or launch from the Start Menu if installed via the Windows installer.

The GUI:
- **Starts the HTTP server automatically** on launch. The header has a **Stop / Start** toggle and a **Dashboard** button that opens the web test client.
- **Status cards**: Status, Network IP, Port, Completed Jobs.
- **Activity Log** tab (default view): live log stream with a **Clear** button.
- **Devices & Printers** tab:
  - *Network Printers*: name, Online/Offline badge, MAC address with its source (`snmp`, `arp`, `hint`), hostname, `IP:port`, and **Copy MAC** / **Copy IP** / **Test** buttons. Printers whose IP is currently unknown are shown as Offline.
  - *Local System Printers*: OS-installed printers with driver, port, status and default flag, plus **Copy Name** / **Test** buttons.
  - **Scan Network** / **Refresh** clear the printer cache and re-scan every detected and custom subnet.
- **Update banner** when a new version is available, and a **Check for updates** link in the footer. The footer also shows platform, host MAC and version.
- Only one GUI instance can run at a time on Windows (a warning is shown if it is already running).

### Command Line (Headless Mode)

```bash
python app/main.py
```

Options:

```
-p, --port        HTTP server port (default: 5050; the value is persisted to config.json)
-c, --config      Path to configuration directory
--log-level       DEBUG | INFO | WARNING | ERROR (default: INFO)
--log-file        Path to log file (rotates at 5 MB, keeps 3 backups)
```

### Configuration

The application stores its config in `~/.config/zebra-print-bridge/config.json` (or `<custom_dir>/config.json` via `--config`).
The file is created with defaults on first run:

```json
{
  "port": 5050,
  "scan_network": true,
  "network_timeout": 0.5,
  "log_level": "INFO",
  "saved_printers": [],
  "printer_aliases": {},
  "custom_subnets": ["192.168.0.0/22"],
  "verify_identity": true,
  "strict_identity": false,
  "discovery_broadcast": false
}
```

| Field                 | Default              | Description |
|-----------------------|----------------------|-------------|
| `port`                | `5050`               | HTTP server port. |
| `scan_network`        | `true`               | Enables the background discovery thread and on-demand subnet scans during target resolution. |
| `network_timeout`     | `0.5`                | Seconds for the passive port-9100 checks used during resolution (capped at 1 s). Explicit connection tests use at least 1.5 s. |
| `log_level`           | `"INFO"`             | Log level used by the GUI. The CLI uses `--log-level` instead. |
| `saved_printers`      | `[]`                 | Static entries `{"name": "...", "ip": "..."}`. Added to the alias map and polled by the background scanner. |
| `printer_aliases`     | `{}`                 | User-defined names, e.g. `{"warehouse": "192.168.1.50"}`. Usable in `printer_ip` / `printer_host`. |
| `custom_subnets`      | `["192.168.0.0/22"]` | Extra CIDR ranges (VPN, other VLANs) merged with the auto-detected subnets. A custom subnet that contains a detected one replaces it. Editable via the `/subnets` API. |
| `verify_identity`     | `true`               | Verify MAC/serial via SNMP and ARP whenever a `printer_mac` is resolved or a cached IP is reused. |
| `strict_identity`     | `false`              | When `true`, jobs and connection checks whose identity is `unverifiable` are rejected with HTTP 503. |
| `discovery_broadcast` | `false`              | Reserved for Zebra UDP 4201 broadcast discovery. Currently a no-op stub. |

### Persistent Printer Cache (v2)

Discovered network printers are persisted to `~/.config/zebra-print-bridge/network_printers.json` (or `<config_dir>/network_printers.json`).
The cache is indexed by device identity so that printer mappings survive DHCP IP changes:

```json
{
  "version": 2,
  "updated_at": "2026-09-15T12:00:00.000000",
  "printers": {
    "00:07:4D:6F:C2:14": {
      "mac": "00:07:4D:6F:C2:14",
      "serial": "ZEB123456",
      "ip": "192.168.1.150",
      "port": 9100,
      "name": "Zebra ZD420",
      "hostname": "zd420.local",
      "last_seen": "2026-09-15T12:00:00.000000",
      "mac_source": "snmp",
      "mac_address": "00:07:4D:6F:C2:14",
      "unique_id": "ZEB123456"
    }
  }
}
```

- **Identity key**: the normalized MAC; if no MAC could be obtained, `serial:<serial>`; if neither, `ip:<ip>`.
- **No duplicates**: a device rediscovered at a new IP updates its existing entry. If another entry held that IP, its IP is cleared and it shows as *Offline* until seen again.
- **Identity mismatch**: when the device answering at a cached IP is not the expected printer, the cached IP is invalidated automatically.
- **Migration**: an unversioned v1 cache (keyed by IP) is converted to v2 on the first load.
- **Background scanner**: polls known IPs every 30 seconds and runs a full subnet scan every 15 minutes (and at startup when the cache is empty).
- **Clearing**: `POST /printers/clear`, `GET /printers?refresh=true`, or **Scan Network** in the GUI.

## Building & Distribution

### Version Bump

`__version__` in `app/__init__.py` is the single source of truth. `build.py` copies it into `version.json` and `installer.iss`, and the release workflow reads it to name the tag and the installer.

### Build the Executable (PyInstaller)

```bash
python build.py            # one-dir build -> dist/ZebraPrintBridge/ZebraPrintBridge.exe
python build.py --onefile  # single file   -> dist/ZebraPrintBridge.exe
```

### Create the Windows Installer (Inno Setup, manual)

1. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php) (free).
2. Run `python build.py` first so that `dist/ZebraPrintBridge/` exists.
3. Open `installer.iss` in Inno Setup Compiler and click **Build → Compile** (or run `ISCC.exe installer.iss`).
4. The installer is generated in `installer_output/ZebraBridgeSetup_<version>.exe`.

### Automated Release (GitHub Actions)

`.github/workflows/release.yml` runs on every push to `main` (changes that only touch `version.json` are ignored) and can also be triggered manually:

1. Reads the version from `app/__init__.py`. If the tag `v<version>` already exists the job stops.
2. Builds the executable with PyInstaller on `windows-latest` (Python 3.12) and compiles the Inno Setup installer.
3. Creates the tag `v<version>` and a GitHub Release with `ZebraBridgeSetup_<version>.exe` attached.
4. Updates `version` and `download_url` in `version.json` and commits the change back to `main`.

To publish a new version: bump `__version__`, optionally edit `notes` in `version.json`, and merge to `main`.

### Auto-Update Flow

The desktop app fetches `version.json` from the raw content of the `main` branch on startup (and on demand via the footer link), compares it with the local version, and shows a banner when a newer release exists. **Download** fetches the installer to a temporary directory and launches it (Windows only).

`version.json` format:

```json
{
  "version": "1.3.0",
  "download_url": "https://github.com/Goodness-Gardens/zebra-print-bridge-win/releases/download/v1.3.0/ZebraBridgeSetup_1.3.0.exe",
  "notes": "Added NetSuite Suitelet server synchronization with background heartbeat and on-demand sync."
}
```

## API Reference

The server listens on `http://<host-ip>:5050` by default and binds to all interfaces.
Interactive OpenAPI docs are available at `/docs` and `/redoc`.
Most JSON responses include `server_hostname` (and the legacy alias `hostname`) identifying the machine running the bridge.

### Printer Targets and Resolution

`POST /print`, `POST /print/raw` and `GET /connection` all accept the same targets and resolve them in a single pass in this order:

| Priority | Target | Resolution |
|----------|--------|------------|
| 0 | `"test"` (in `printer_ip` or `printer_name`) | Simulated printer. Nothing is sent. |
| 1 | `printer_mac` | Hint IP (when `printer_ip` is also given) → identity cache → OS ARP table → subnet scan (if `scan_network`). Fails with HTTP 503 `Printer with MAC ... not found on network`. Never triggers DNS. |
| 2 | `printer_ip` / `printer_host` | IPv4 with optional `:port` (`192.168.1.50:9100`) → discovered name, hostname or serial, or config alias → DNS → mDNS (`.local`, `.localdomain`) → subnet scan to refresh aliases. |
| 3 | `printer_name` | OS-installed printer (case-insensitive exact match, then substring). If not found it is checked against network aliases, then falls back to the OS default printer. |
| 4 | *(no target)* | OS default printer. HTTP 400 if the OS has no printers. |

Notes:
- Accepted MAC formats: `00:07:4D:6F:C2:14`, `00-07-4D-6F-C2-14`, `0007.4d6f.c214`, `00074d6fc214`. A MAC passed in `printer_ip` is detected automatically and treated as `printer_mac`.
- A `printer_ip` value that is not a valid IPv4 address or hostname is treated as `printer_name`.
- Surrounding quotes in targets and payloads are stripped automatically.

**Identity verification** (`verify_identity: true`): whenever a MAC is resolved through a hint IP, the cache, or ARP, the bridge asks the device for its MAC (`ifPhysAddress`) and Zebra serial over SNMP (UDP 161) and falls back to the ARP table. The result is one of:

- `match`: the device identity matches. Resolution is instant and the cache is refreshed.
- `mismatch`: another device answers at that IP. The cached IP is invalidated and resolution continues (hint → cache → ARP → scan).
- `unverifiable`: the device does not answer SNMP/ARP. By default the job proceeds with a warning (fail-open). With `strict_identity: true` it is rejected with HTTP 503.

Hosts that do not answer SNMP are remembered for 10 minutes so later requests do not pay the timeout again.

### Endpoint Summary

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/`                 | Redirects browsers to `/dashboard`; JSON status for API clients |
| GET  | `/health`           | Lightweight health check |
| GET  | `/info`             | Server info: network address, MAC, supported targets, endpoint list |
| GET  | `/status`           | Queue metrics, recent jobs, runtime counters, usage |
| GET  | `/logs`             | Recent jobs and counters |
| POST | `/logs/clear`       | Truncate log files and reset runtime history |
| GET  | `/connection`       | Preflight check for a printer target |
| GET  | `/printers`         | Discovered network printers + OS printers (`?refresh=true` re-scans) |
| POST | `/printers/refresh` | Clear cache, re-scan, return fresh list |
| POST | `/printers/clear`   | Clear the persistent printer cache |
| GET  | `/printers/config/schema` | Complete schema of configurable options via SGD |
| GET  | `/printers/{target}/config` | Get live hardware configuration (margins, method, width, etc.) |
| POST | `/printers/{target}/config` | Configure hardware settings & margins without printing |
| GET  | `/subnets`          | Detected and custom subnets (`?scan=true` also scans) |
| GET/POST | `/subnets/scan` | Scan all subnets or one CIDR for Zebra printers |
| POST | `/subnets`          | Add a custom subnet to the config |
| DELETE | `/subnets`        | Remove a custom subnet from the config |
| POST | `/print`            | Enqueue a print job (JSON) |
| POST | `/print/raw`        | Enqueue a print job (plain-text body, target in query string) |
| GET  | `/dashboard`, `/test-client` | Built-in web test client |

### Endpoints

#### `GET /`

For browsers (`Accept: text/html` or a Mozilla user agent), redirects to `/dashboard` with HTTP 307. For API clients, returns JSON status.

```json
{
  "status": "running",
  "service": "Zebra Print Bridge",
  "version": "1.3.0",
  "server_hostname": "PRINT-PC",
  "hostname": "PRINT-PC",
  "mode": "raw_printing"
}
```

#### `GET /health`

```json
{
  "healthy": true
}
```

#### `GET /info`

Server info including network address, MAC and the accepted targets.

```json
{
  "service": "Zebra Print Bridge",
  "version": "1.3.0",
  "server_hostname": "PRINT-PC",
  "hostname": "PRINT-PC",
  "mode": "raw_printing",
  "port": 5050,
  "server_port": 5050,
  "server_ip": "192.168.1.100",
  "network_ip": "192.168.1.100",
  "server_mac": "A4:5E:60:11:22:33",
  "network_mac": "A4:5E:60:11:22:33",
  "mac_address": "A4:5E:60:11:22:33",
  "mac": "A4:5E:60:11:22:33",
  "server_url": "192.168.1.100:5050",
  "server_url_host": "PRINT-PC:5050",
  "local_url": "http://localhost:5050",
  "network_url": "http://192.168.1.100:5050",
  "platform": "Windows AMD64",
  "uptime": "2h 15m 30s",
  "required_fields": {
    "json_print": [
      "raw_command (or legacy field 'zpl')",
      "printer_mac (Ethernet MAC address, Priority 1: resolves dynamically to current IP, immune to DHCP changes)",
      "printer_ip / printer_host (Priority 2: IPv4 address, hostname, or IP hint when combined with printer_mac)",
      "printer_name (Priority 3: local OS spooler printer)",
      "OS default printer (Priority 4: fallback when no target specified)"
    ],
    "raw_print": ["printer_mac, printer_ip or printer_name (query: MAC, IPv4, hostname, or 'test')", "raw body"]
  },
  "supported_targets": {
    "printer_mac": "Ethernet MAC address (e.g. '00:07:4D:6F:C2:14') - dynamic ARP/cache IP resolution (Priority 1)",
    "printer_ip": "Direct IPv4 address (e.g. '192.168.1.150'), simulated 'test', or IP hint with printer_mac (Priority 2)",
    "printer_host": "Network DNS hostname or alias (e.g. 'NH-LSHIP1') (Priority 2)",
    "printer_name": "Local OS printer installed in spooler (Priority 3)",
    "default_os_printer": "Default printer configured in OS spooler (Priority 4)"
  },
  "identity_verification": {
    "verify_identity": true,
    "strict_identity": false,
    "states": ["match", "mismatch", "unverifiable"]
  },
  "endpoints": {
    "print": "/print (POST JSON, supports printer_mac, printer_ip, printer_host, printer_name)",
    "print_raw": "/print/raw?printer_mac=<MAC>&printer_ip=<IP>&printer_name=<name> (POST plain text)",
    "printers": "/printers (GET, accepts ?refresh=true) — list discovered network printers and OS-installed printers",
    "printers_refresh": "/printers/refresh (POST) — clear cache and re-scan network printers",
    "printers_clear": "/printers/clear (POST) — clear printer cache",
    "subnets": "/subnets (GET, accepts ?scan=true) — list detected network interfaces and subnets",
    "subnets_scan": "/subnets/scan (GET/POST, ?subnet=<CIDR>&clear_cache=<bool>) — scan subnets for Zebra printers",
    "subnets_add": "/subnets (POST JSON: {\"subnet\": \"<CIDR>\"}) — add custom subnet to config",
    "subnets_delete": "/subnets?subnet=<CIDR> (DELETE) — remove custom subnet from config",
    "connection": "/connection?printer_mac=<MAC>&printer_ip=<IPv4|hostname|test>&printer_name=<name> (GET)",
    "status": "/status (GET)",
    "health": "/health (GET)",
    "info": "/info (GET)",
    "logs_clear": "/logs/clear (POST)",
    "dashboard": "/dashboard (GET)",
    "test_client": "/test-client (GET)"
  }
}
```

#### `GET /status`

Queue metrics, the 20 most recent jobs (pending first), runtime counters and per-route usage.

```json
{
  "server_running": true,
  "mode": "raw_printing",
  "server_hostname": "PRINT-PC",
  "hostname": "PRINT-PC",
  "server_mac": "A4:5E:60:11:22:33",
  "pending_jobs": 0,
  "completed_jobs": 15,
  "failed_jobs": 1,
  "uptime": "2h 15m 30s",
  "recent_jobs": [
    {
      "id": "143201123456",
      "source": "Web App",
      "printer_ip": "192.168.1.150",
      "status": "completed",
      "time": "14:32:01",
      "error": null
    }
  ],
  "runtime_counters": {
    "job_completed": 15,
    "job_queued": 16
  },
  "usage": {
    "counters": {
      "json_print_requested": 16
    },
    "route_hits": {
      "POST /print": 16
    }
  }
}
```

`recent_jobs[].printer_ip` is the resolved IP for network jobs and `null` for local OS printer jobs. `status` is `pending`, `completed` or `failed`; `error` carries the failure reason.

#### `GET /logs`

```json
{
  "recent_jobs": [],
  "pending": 0,
  "completed": 15,
  "failed": 1,
  "usage": { "counters": {}, "route_hits": {} }
}
```

#### `POST /logs/clear`

Truncates the active log file(s), clears usage counters and completed/failed job history. Pending jobs are preserved.

```json
{
  "success": true,
  "message": "Logs cleared successfully",
  "cleared_log_files": ["C:\\Users\\me\\bridge.log"],
  "cleared_job_history": 16,
  "pending_jobs": 0,
  "completed_jobs": 0,
  "failed_jobs": 0
}
```

#### `GET /connection`

Checks printer connectivity without sending any print job. Uses the same resolution as `/print`.

**Query parameters:**

| Parameter      | Aliases                      | Description |
|----------------|------------------------------|-------------|
| `printer_mac`  | `mac`                        | Ethernet MAC address |
| `printer_ip`   | `ip`, `printer_host`, `host` | IPv4 (optional `:port`), hostname, alias, or `"test"`. Also accepts a MAC. Used as a hint when combined with `printer_mac`. |
| `printer_name` |                              | Local OS printer name |

```bash
curl "http://192.168.1.100:5050/connection?printer_mac=00:07:4D:6F:C2:14"
curl "http://192.168.1.100:5050/connection?printer_mac=00:07:4D:6F:C2:14&printer_ip=192.168.1.150"
curl "http://192.168.1.100:5050/connection?printer_ip=192.168.1.150"
curl "http://192.168.1.100:5050/connection?printer_ip=zebra-printer.local"
curl "http://192.168.1.100:5050/connection?printer_name=ZDesigner%20ZD420"
```

**Response:**

```json
{
  "success": true,
  "printer_ip": "192.168.1.150",
  "printer_mac": "00:07:4D:6F:C2:14",
  "printer_type": "network",
  "message": "Resolved MAC '00:07:4D:6F:C2:14' from cache -> 192.168.1.150",
  "latency_ms": 8.42,
  "identity": "match",
  "server_hostname": "PRINT-PC",
  "hostname": "PRINT-PC"
}
```

- `printer_type`: `network`, `local`, `test` or `unknown`.
- `identity`: `match`, `mismatch`, `unverifiable`, or `null` for local and test printers.
- For local printers `printer_ip` contains the printer name.

**Error response (HTTP 503):**

```json
{
  "detail": "Cannot connect to printer at 192.168.1.150:9100"
}
```

Other typical `detail` values: `Printer with MAC 00:07:4D:6F:C2:14 not found on network`, `Cannot resolve network address 'zebra-01'`, `Printer 'X' not found in OS`, `Identity verification failed: device at 192.168.1.150 (00:11:22:33:44:55) does not match expected 00:07:4D:6F:C2:14`.

#### `GET /printers`

Lists discovered network printers and OS-installed printers. Pass `?refresh=true` to clear the cache and re-scan first.

```json
{
  "server_hostname": "PRINT-PC",
  "hostname": "PRINT-PC",
  "count": 2,
  "printers": ["...network_printers followed by local_printers..."],
  "network_printers": [
    {
      "name": "Zebra ZD420",
      "type": "network",
      "hostname": "zd420.local",
      "address": "192.168.1.150",
      "ip": "192.168.1.150",
      "port": 9100,
      "status": "ready",
      "unique_id": "ZEB123456",
      "serial": "ZEB123456",
      "mac_address": "00:07:4D:6F:C2:14",
      "mac": "00:07:4D:6F:C2:14",
      "mac_source": "snmp",
      "last_seen": "2026-09-15T12:00:00.000000"
    }
  ],
  "local_printers": [
    {
      "name": "ZDesigner ZD420-203dpi ZPL",
      "type": "local",
      "driver": "ZDesigner ZD420-203dpi ZPL",
      "port": "USB001",
      "status": "ready",
      "status_code": 0,
      "comment": "",
      "location": "",
      "is_default": true
    }
  ]
}
```

- Network `status` is `ready` when the printer has a known IP and `offline` when its IP is currently unknown (e.g. after a DHCP change not yet re-discovered). `mac_source` is `snmp`, `arp`, `hint` or `null`.
- Local `status` is `ready`, `paused`, `error`, `pending_deletion`, `offline` or `available`. On macOS/Linux `driver` and `port` are `CUPS`.

#### `POST /printers/refresh`

Clears the printer cache, re-scans all subnets and returns the same payload as `GET /printers` plus:

```json
{
  "success": true,
  "message": "Printer cache cleared and network re-scanned"
}
```

#### `POST /printers/clear`

Clears the in-memory cache and deletes `network_printers.json`.

```json
{
  "success": true,
  "message": "Printer cache cleared successfully"
}
```

#### `GET /printers/config/schema`

Returns the complete schema of all supported Zebra printer configuration settings (print method, media type, print mode, print width, label length, darkness, speed, margins: top, left, bottom, right) with descriptions, units, and validation boundaries.

#### `GET /printers/{target}/config`

Queries live SGD configuration from a network Zebra printer (identified by IP, MAC, hostname, or alias), including printable width, length, resolution, and current margin positions (`label_top`, `left_position`, `tear_off`).

#### `POST /printers/{target}/config`

Applies hardware configuration and margin offsets directly to a Zebra printer via SGD without requiring a print job.

**Margin parameters supported:**
- `top_margin` / `label_top` (`int`, dots): vertical image offset via `zpl.label_top` (positive shifts down, negative shifts up).
- `left_margin` / `left_position` (`int`, dots): horizontal image offset via `zpl.left_position` (positive shifts right, negative shifts left).
- `bottom_margin` / `tear_off` (`int`, dots): bottom / tear-off resting position via `ezpl.tear_off`.
- `right_margin` (`int`, dots): right margin offset (adjusts `ezpl.print_width = width - right_margin`).
- `margins` (`object`): composite dictionary with `{"top": 10, "left": 15, "bottom": 0, "right": 0}`.
- `save_to_flash` (`bool`): persist configuration permanently to non-volatile EEPROM memory (`^JUS`).

Example request:
```json
{
  "top_margin": 10,
  "left_margin": 15,
  "bottom_margin": 0,
  "right_margin": 0,
  "save_to_flash": true
}
```

#### `GET /subnets`

Returns the auto-detected interface subnets, the custom subnets from the config, and the merged list used for scanning. Pass `?scan=true` (optionally `&clear_cache=true`) to also run a scan; the result is added under `scan_result`.

```json
{
  "server_hostname": "PRINT-PC",
  "hostname": "PRINT-PC",
  "detected_subnets": [
    {
      "interface": "Ethernet",
      "ip": "192.168.1.20",
      "netmask": "255.255.255.0",
      "cidr": "192.168.1.0/24",
      "prefix_len": 24,
      "hosts_count": 254
    }
  ],
  "custom_subnets": [
    { "cidr": "192.168.0.0/22", "prefix_len": 22, "hosts_count": 1022 }
  ],
  "all_subnets": ["192.168.0.0/22"],
  "total_subnets": 1,
  "total_ips": 1022
}
```

Detection uses PowerShell `Get-NetIPAddress` (fallback `ipconfig /all`) on Windows and `ifconfig` (fallback `ip addr`) on macOS/Linux. Loopback and link-local addresses are ignored; point-to-point VPN interfaces (`utun`, `tun`, `ppp`, `wg`) are added as a `/24`.

#### `GET|POST /subnets/scan`

Scans every subnet in `all_subnets`, or only `?subnet=<CIDR>`. `?clear_cache=true` empties the cache before scanning. The scan is a passive TCP connect to port 9100 on every host (100 concurrent probes).

```bash
curl -X POST "http://192.168.1.100:5050/subnets/scan?subnet=192.168.1.0/24"
```

```json
{
  "success": true,
  "message": "Subnet scan completed for 192.168.1.0/24",
  "server_hostname": "PRINT-PC",
  "hostname": "PRINT-PC",
  "scanned_subnet": "192.168.1.0/24",
  "discovered_printers": [
    {
      "name": "Zebra ZD420",
      "hostname": "zd420.local",
      "ip": "192.168.1.150",
      "port": 9100,
      "mac": "00:07:4D:6F:C2:14",
      "mac_address": "00:07:4D:6F:C2:14",
      "serial": "ZEB123456",
      "unique_id": "ZEB123456",
      "mac_source": "snmp",
      "last_seen": "2026-09-15T12:00:00.000000"
    }
  ],
  "count": 1
}
```

#### `POST /subnets`

Adds a custom subnet to `config.json` and to the running scanner. Accepts a JSON body `{"subnet": "10.0.1.0/24"}` or `?subnet=10.0.1.0/24`. Returns HTTP 400 when the value is missing or not a valid IPv4 CIDR.

```bash
curl -X POST http://192.168.1.100:5050/subnets \
  -H "Content-Type: application/json" \
  -d '{"subnet": "10.0.1.0/24"}'
```

```json
{
  "success": true,
  "message": "Custom subnet '10.0.1.0/24' added successfully",
  "added_subnet": "10.0.1.0/24",
  "detected_subnets": ["..."],
  "custom_subnets": ["..."],
  "all_subnets": ["192.168.0.0/22", "10.0.1.0/24"],
  "total_subnets": 2,
  "total_ips": 1276
}
```

#### `DELETE /subnets?subnet=<CIDR>`

Removes a custom subnet from the config. Returns the same subnet payload with `"removed_subnet"` instead of `"added_subnet"`.

#### `GET /dashboard` and `GET /test-client`

Built-in web test client. Open it in a browser to list printers, run connection checks and send test jobs interactively.

```
http://192.168.1.100:5050/dashboard
```

#### `POST /api/server/sync` and `GET /api/server/sync`

Manually trigger server registration/synchronization with NetSuite Suitelet (`customscript_lpui_sl_server_sync`).
Assembles local MAC, IP, port, server name, priority, and calls the configured Suitelet URL.

```bash
curl -X POST http://localhost:5050/api/server/sync
```

```json
{
  "success": true,
  "status_code": 200,
  "duration_ms": 145.2,
  "message": "Server synced successfully with NetSuite Suitelet",
  "mac": "62:39:A2:0D:A4:B5",
  "ip": "192.168.1.11",
  "port": 5050,
  "name": "Miami Yaki PC"
}
```

---

#### `POST /print`

Send a print job as JSON. The request is answered after the target has been resolved and the preflight connection check has passed. The job is then sent asynchronously by the queue worker; the final outcome is visible in `GET /status`.

**Request:**

```bash
curl -X POST http://192.168.1.100:5050/print \
  -H "Content-Type: application/json" \
  -d '{
    "printer_ip": "192.168.1.150",
    "raw_command": "^XA^FO50,50^A0N,50,50^FDHello World^FS^XZ",
    "source": "My Web App"
  }'
```

**Parameters:**

| Parameter      | Type   | Required | Description |
|----------------|--------|----------|-------------|
| `printer_mac`  | string | No       | **Priority 1 (recommended)**: Ethernet MAC address. Resolved dynamically; immune to DHCP IP changes. |
| `printer_ip`   | string | No       | **Priority 2**: IPv4 (optional `:port`), hostname, alias, or `"test"`. When sent together with `printer_mac` it acts as an instant hint. Alias: `printer_host`. |
| `printer_name` | string | No       | **Priority 3**: Name of a printer installed in the OS spooler (case-insensitive, partial match allowed). |
| `raw_command`  | string | Yes      | Raw ZPL/EPL/etc. command string. Legacy alias: `zpl`. |
| `source`       | string | No       | Source label for tracking (default: `"Web API"`). |
| `id`           | string | No       | Custom job ID (default: time-based `HHMMSSffffff`). |
| `dpi`          | int    | No       | Printer resolution (informational only). |
| `label_size`   | object | No       | Label dimensions `{width, height}` (informational only). |

If no target is specified, the job is routed to the OS default printer.

**Example: printing via MAC address with IP hint:**

```bash
curl -X POST http://192.168.1.100:5050/print \
  -H "Content-Type: application/json" \
  -d '{
    "printer_mac": "00:07:4D:6F:C2:14",
    "printer_ip": "192.168.1.150",
    "raw_command": "^XA^FO50,50^A0N,50,50^FDHello World^FS^XZ",
    "source": "Warehouse App"
  }'
```

**Response:**

```json
{
  "success": true,
  "job_id": "143201123456",
  "message": "Job queued successfully",
  "server_hostname": "PRINT-PC",
  "hostname": "PRINT-PC"
}
```

For the `"test"` target the message is `Job queued successfully (Test Mode)`.

**Error responses:**

| HTTP | `detail` (examples) | Cause |
|------|---------------------|-------|
| 400  | `raw_command is required` / `No printer target specified` | Missing payload, or no target and no OS printers |
| 404  | `Printer 'Foo' not found in OS or network` | `printer_name` not found and no default printer available |
| 503  | `Cannot connect to printer at 192.168.1.150:9100` / `Printer with MAC ... not found on network` / `Identity verification failed: ...` / `Identity unverifiable for printer target ... and strict_identity is enabled` | Preflight failed |
| 500  | *(exception text)* | Unexpected error |

#### `POST /print/raw`

Send a raw command as the plain-text body with the target in the query string.

**Request:**

```bash
curl -X POST "http://192.168.1.100:5050/print/raw?printer_mac=00:07:4D:6F:C2:14&source=MyApp" \
  -H "Content-Type: text/plain; charset=utf-8" \
  --data-binary '^XA^FO50,50^A0N,50,50^FDHello^FS^XZ'

# by hostname
curl -X POST "http://192.168.1.100:5050/print/raw?printer_ip=zebra-printer.local" \
  -H "Content-Type: text/plain; charset=utf-8" \
  --data-binary '^XA^FO50,50^A0N,50,50^FDHello^FS^XZ'
```

**Query parameters:**

| Parameter      | Aliases                      | Description |
|----------------|------------------------------|-------------|
| `printer_mac`  | `mac`                        | Ethernet MAC address (Priority 1) |
| `printer_ip`   | `printer_host`, `ip`, `host` | IPv4 (optional `:port`), hostname, alias, or `"test"`; also accepts a MAC (Priority 2) |
| `printer_name` | `name`                       | Local OS printer name (Priority 3) |
| `source`       |                              | Source label for tracking (default: `"Raw API"`) |
| `id`           |                              | Custom job ID |

**Validation:**
- The request body cannot be empty (HTTP 400 `Empty raw command`).
- Targets follow the same resolution and error codes as `POST /print` (404 not found, 503 unreachable). With no target the job goes to the OS default printer.

**Response:** same shape as `POST /print`.

---

## Web Integration Examples

### JavaScript / Fetch API

```javascript
async function printLabel(zplCode, target) {
  // target: { printer_mac, printer_ip, printer_name } — any combination, printer_mac recommended
  const response = await fetch("http://192.168.1.100:5050/print", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...target,
      raw_command: zplCode,
      source: "Web App",
    }),
  });

  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.detail || "Print request failed");
  }
  console.log(`Job sent: ${result.job_id}`);
  return result;
}

// Usage: MAC address with the last known IP as a hint
const zpl = `^XA^FO50,50^A0N,50,50^FDProduct: Widget^FS^FO50,120^BY3^BCN,100,Y,N,N^FD123456789^FS^XZ`;
printLabel(zpl, { printer_mac: "00:07:4D:6F:C2:14", printer_ip: "192.168.1.150" });
```

### Python Client

```python
import requests

def print_label(zpl, printer_ip, server_url="http://192.168.1.100:5050"):
    response = requests.post(
        f"{server_url}/print",
        json={
            "printer_ip": printer_ip,
            "raw_command": zpl,
            "source": "Python Client"
        }
    )
    response.raise_for_status()
    return response.json()

# Usage
zpl = "^XA^FO50,50^A0N,50,50^FDHello from Python^FS^XZ"
result = print_label(zpl, "192.168.1.150")
print(result)
```

## Troubleshooting

### Cannot connect to the server

```bash
# Check if the port is open
nc -zv localhost 5050

# Windows: check if the port is listening
netstat -an | findstr 5050
```

### Network printer unreachable

```bash
# Test connectivity to the printer
nc -zv <printer-ip> 9100

# Or use the API preflight check
curl "http://localhost:5050/connection?printer_ip=<printer-ip>"
```

### `Printer with MAC ... not found on network`

- Check that the printer's subnet is listed in `GET /subnets`. If it is not (VPN, other VLAN), add it with `POST /subnets` and run `POST /subnets/scan`.
- MAC resolution across routers relies on SNMP (UDP 161). If SNMP is disabled on the printer or blocked by a firewall, only the ARP table of the local subnet can be used.
- Send the last known IP in `printer_ip` together with `printer_mac`: a verified hint resolves instantly without scanning.

### Identity `mismatch` or `unverifiable`

- `mismatch` means another device now answers at the cached IP. The cached IP is invalidated automatically; run `GET /connection?printer_mac=...` again to re-resolve.
- `unverifiable` means the printer does not answer SNMP or ARP. Jobs still print by default; set `strict_identity: true` only if every printer answers SNMP.

### Stale printer list

Run `POST /printers/clear` (or `GET /printers?refresh=true`, or **Scan Network** in the GUI). The cache file is `~/.config/zebra-print-bridge/network_printers.json`.

### Local printer not found

`GET /printers` shows the exact names in `local_printers`. Matching is case-insensitive and allows partial names. On Windows local printing requires `pywin32`; on macOS/Linux it requires CUPS (`lp`).

## Network Configuration

### Allow Remote Access

The server binds to `0.0.0.0` by default, allowing access from any device on the network.

### Ports Used

| Direction | Protocol / Port | Purpose |
|-----------|-----------------|---------|
| Inbound   | TCP 5050 (configurable) | HTTP API and web test client |
| Outbound  | TCP 9100        | RAW printing and passive reachability checks |
| Outbound  | UDP 161 (SNMP)  | Printer MAC, serial and friendly name |
| Outbound  | TCP 80          | Optional: printer web page title used as friendly name |

### Firewall Configuration

```bash
# Windows (run as Administrator)
netsh advfirewall firewall add rule name="Zebra Print Bridge" dir=in action=allow protocol=tcp localport=5050
```

## Performance Tips

1. **Use `printer_mac` with `printer_ip` as a hint**: a verified hint resolves instantly; a MAC that is not cached triggers a full subnet scan on first use.
2. **Keep `custom_subnets` tight**: every extra `/22` adds about a thousand hosts to each scan.
3. **Use wired Ethernet** for more reliable network printing.
4. **Set a static IP** for the host machine.
5. **Use `GET /connection` before large print batches** to fail fast on offline printers.

## Project Structure

```
zebra-print-bridge-win/
├── .github/
│   └── workflows/
│       └── release.yml    # Build, tag and publish releases on push to main
├── app/
│   ├── __init__.py        # Version constant (single source of truth)
│   ├── config.py          # Configuration management (config.json)
│   ├── gui.py             # Desktop GUI (CustomTkinter)
│   ├── main.py            # PrintBridge core (queue, resolution glue) + CLI entry point
│   ├── printer_manager.py # Target resolution, SNMP/ARP identity, discovery, cache, network/local sending
│   ├── server.py          # FastAPI HTTP server and endpoints
│   ├── suitelet_sync.py   # NetSuite Suitelet server synchronization client
│   ├── updater.py         # Auto-update checker (GitHub raw version.json + Releases)
│   └── utils.py           # IP/MAC/hostname helpers, ARP lookups, safe subprocess wrapper
├── resources/
│   ├── icon.ico           # Application icon (Windows)
│   ├── icon.png           # Application icon (PNG)
│   └── test-client.html   # Web test client UI
├── tests/                 # pytest suite (resolution, cache identity, SNMP, subnets, server concurrency)
├── build.py               # PyInstaller build script (syncs version.json and installer.iss)
├── installer.iss          # Inno Setup installer script
├── ZebraPrintBridge.spec  # PyInstaller spec generated by a previous build (build.py does not use it)
├── run_gui.py             # GUI entry point
├── version.json           # Auto-update manifest (kept in sync by build.py and CI)
├── requirements.txt       # Runtime dependencies
└── requirements-dev.txt   # Test dependencies
```

## License

MIT License

## Contributing

1. Fork the repository
2. Create a feature branch
3. Submit a pull request

## Support

For issues and feature requests, please open a GitHub issue.
