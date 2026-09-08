# Zebra Print Bridge

A lightweight desktop application that receives ZPL II commands from
web applications and sends them directly to Zebra printers over the network via TCP/IP.

> **Mode: Network & Local RAW command.**
> Every print request specifies `printer_ip` (IPv4 or hostname), or `printer_name` (local OS printer), and `raw_command` directly.
> No saved printers or complex printer management.

## Features

- **Desktop GUI**: Modern dark-themed control panel (CustomTkinter)
- **Embedded HTTP Server**: Receives ZPL commands from any web application
- **Direct Network Printing**: Sends RAW commands directly to the printer IP or hostname on port 9100
- **Web Test Client**: Built-in browser UI for testing print jobs
- **Auto-Updater**: Checks the GitHub repository directly for new versions and offers one-click updates
- **Queued Processing**: Accepts incoming jobs and processes them in order
- **Connection Preflight**: Rejects unreachable printers before enqueuing
- **Windows Installer**: Distributable `.exe` installer via Inno Setup
- **Lightweight Configuration**: Stores only port and log settings

## Requirements

### For Windows Installer (End Users)

- Windows 10/11 (64-bit)
- Network-connected Zebra printer (TCP/IP, port 9100)

### For Development

- Python 3.9+
- Network-connected Zebra printer (TCP/IP, port 9100)

## Installation

### Option A: Windows Installer (Recommended for End Users)

1. Download the latest `ZebraBridgeSetup.exe` from [Releases](https://github.com/Goodness-Gardens/zebra-print-bridge-win/releases).
2. Run the installer and follow the wizard.
3. Launch **Zebra Print Bridge** from the Start Menu or Desktop shortcut.

### Option C: Source Code (Development)

```bash
# Clone the repository
git clone https://github.com/Goodness-Gardens/zebra-print-bridge-win.git
cd zebra-print-bridge-win

# Create virtual environment and install dependencies
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

### Desktop GUI (Windows)

```bash
# From source
.\venv\Scripts\python.exe run_gui.py
```

Or launch from the Start Menu if installed via the Windows installer.

The GUI provides:
- **Server status** with start/stop controls
- **Network IP and port** display
- **Live activity log** viewer
- **Dashboard** button to open the web test client
- **Auto-update banner** when a new version is available
- **Check for updates option** directly in the footer

### Command Line (Headless Mode)

```bash
python app/main.py
```

Options:

```
-p, --port        HTTP server port (default: 5050)
-c, --config      Path to configuration directory
--log-level       DEBUG | INFO | WARNING | ERROR (default: INFO)
--log-file        Path to log file
```

### Configuration

The application stores its config in `~/.config/zebra-print-bridge/config.json`.
Available fields include:

```json
{
  "port": 5050,
  "log_level": "INFO",
  "web_interface": true,
  "network_timeout": 0.5,
  "auto_start": false
}
```

## Building & Distribution

### Build the Executable (PyInstaller)

```bash
.\venv\Scripts\python.exe build.py
```

The output is created in `dist/ZebraPrintBridge/ZebraPrintBridge.exe`.

### Create the Windows Installer (Inno Setup)

1. Install [Inno Setup](https://jrsoftware.org/isinfo.php) (free).
2. Open `installer.iss` in Inno Setup Compiler.
3. Click **Build → Compile**.
4. The installer is generated in `installer_output/ZebraBridgeSetup_<version>.exe`.

1. Upload the new installer as an asset in a **GitHub Release**.
2. Right-click the `.exe` asset in the release and copy its direct link.
3. Edit `version.json` in the repo root with the new version number and paste the exact `download_url`.
4. Push your changes to the `main` branch.
5. The desktop app checks the `version.json` directly from GitHub's raw content servers on startup (or manually via the footer option) and shows an update banner if a newer version is available.

`version.json` format:

```json
{
  "version": "2.1.0",
  "download_url": "https://github.com/Goodness-Gardens/zebra-print-bridge-win/releases/download/v2.1.0/ZebraBridgeSetup_2.1.0.exe",
  "notes": "Bug fixes and improvements."
}
```

## API Reference

The server listens on `http://<host-ip>:5050` by default.

### Endpoints

#### `GET /`

For browsers, redirects to `/dashboard`. For API clients, returns JSON status.

**JSON Response:**

```json
{
  "status": "running",
  "service": "Zebra Print Bridge",
  "version": "2.0.0",
  "mode": "raw_printing"
}
```

#### `GET /health`

Simple health check.

```json
{
  "healthy": true
}
```

#### `GET /info`

Server info including network address and required fields.

```json
{
  "service": "Zebra Print Bridge",
  "version": "2.0.0",
  "mode": "raw_printing",
  "port": 5050,
  "local_url": "http://localhost:5050",
  "network_url": "http://192.168.1.100:5050",
  "network_ip": "192.168.1.100",
  "platform": "Windows 10",
  "uptime": "2h 15m 30s",
  "required_fields": {
    "json_print": [
      "printer_ip (IPv4, hostname, or 'test'; alias: printer_host) OR printer_name (local OS printer)",
      "raw_command (or legacy field 'zpl')"
    ],
    "raw_print": ["printer_ip (query, IPv4, hostname, or 'test')", "raw body"]
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
    "test_client": "/test-client (GET)"
  }
}
```

#### `GET /status`

Queue metrics, runtime counters, and recent usage data.

```json
{
  "server_running": true,
  "mode": "raw_printing",
  "pending_jobs": 0,
  "completed_jobs": 15,
  "failed_jobs": 1,
  "uptime": "2h 15m 30s",
  "recent_jobs": [
    {
      "id": "143201123456",
      "source": "Web App",
      "printer_ip": "192.168.1.100",
      "status": "completed",
      "time": "14:32:01",
      "error": null
    }
  ],
  "usage": {
    "counters": {
      "json_print_requested": 15
    },
    "route_hits": {
      "POST /print": 15
    }
  }
}
```

#### `GET /connection?printer_ip=<IPv4|hostname|test>&printer_name=<name>`

Checks printer connectivity without sending any print job. Accepts either `printer_ip` (IPv4 or network hostname) or `printer_name` (local OS printer).

```bash
curl "http://192.168.1.100:5050/connection?printer_ip=192.168.1.50"
# or by network hostname
curl "http://192.168.1.100:5050/connection?printer_ip=zebra-printer.local"
# or
curl "http://192.168.1.100:5050/connection?printer_name=Zebra_GK420d"
```

```json
{
  "success": true,
  "printer_ip": "192.168.1.50",
  "printer_type": "network",
  "message": "Connected to 192.168.1.50:9100",
  "latency_ms": 8.42
}
```

**Error Response (printer unreachable):**

```json
{
  "detail": "Cannot connect to printer"
}
```

#### `GET /printers`

List local/USB printers installed in the OS.

```json
{
  "printers": ["Zebra_GK420d", "Microsoft Print to PDF"],
  "count": 2
}
```

#### `GET /logs`

Recent activity and counters.

#### `POST /logs/clear`

Clears the active log file plus dashboard/runtime history. Pending jobs are preserved.

#### `GET /dashboard` and `GET /test-client`

Built-in web test client. Open in a browser to send test print jobs interactively.

```
http://192.168.1.100:5050/test-client
```

---

#### `POST /print`

Send a ZPL print job via JSON payload.

**Request:**

```bash
curl -X POST http://192.168.1.100:5050/print \
  -H "Content-Type: application/json" \
  -d '{
    "printer_ip": "192.168.1.50",
    "raw_command": "^XA^FO50,50^A0N,50,50^FDHello World^FS^XZ",
    "source": "My Web App"
  }'
```

**Resolution & Fallback Hierarchy:**
1. **Primary (`printer_name`)**: If provided, attempts to print to this OS-installed printer.
2. **Fallback 1 (Default OS Printer)**: If `printer_name` is not found (or omitted), falls back to the system's default printer.
3. **Fallback 2 (`printer_ip`)**: If neither local printer is available and `printer_ip` is specified, routes to the network printer via TCP/IP port 9100.

**Parameters:**

| Parameter      | Type   | Required | Description                                              |
|----------------|--------|----------|----------------------------------------------------------|
| `printer_name` | string | No       | **Primary**: Name of local/USB printer installed in OS   |
| `printer_ip`   | string | No       | **Fallback**: Printer IPv4 address, hostname, or `"test"` (alias: `printer_host`) |
| `raw_command`  | string | Yes      | Raw ZPL command string (alias: `zpl` for legacy payload) |
| `source`       | string | No       | Source label for tracking (default: `"Web API"`)         |
| `id`           | string | No       | Custom job ID                                            |
| `dpi`          | int    | No       | Printer resolution (informational only)                  |
| `label_size`   | object | No       | Label dimensions `{width, height}` (informational)       |

*\* If neither `printer_name` nor `printer_ip` is specified, the server automatically routes to the default OS printer.*

**Response:**

```json
{
  "success": true,
  "job_id": "143201123456",
  "message": "Job queued successfully"
}
```

**Error Response (printer unreachable):**

```json
{
  "detail": "Cannot connect to printer"
}
```

#### `POST /print/raw`

Send a raw ZPL command with the printer IP or hostname in the query string.

**Request:**

```bash
curl -X POST "http://192.168.1.100:5050/print/raw?printer_ip=zebra-printer.local&source=MyApp" \
  -H "Content-Type: text/plain; charset=utf-8" \
  --data-binary '^XA^FO50,50^A0N,50,50^FDHello^FS^XZ'
```

**Query Parameters:**

| Parameter    | Description                                                           |
|--------------|-----------------------------------------------------------------------|
| `printer_ip` | Printer IPv4 address, network hostname, or `"test"` (aliases: `ip`, `printer_host`, `host`) |
| `source`     | Source label for tracking                                             |
| `id`         | Custom job ID                                                         |

**Validation:**
- `printer_ip` must be a valid IPv4 address, network hostname, or the special value `"test"`.
- Request body cannot be empty.
- If `printer_ip` is not `"test"` and port `9100` is unreachable, the API returns `503 Service Unavailable`.

---

## Web Integration Examples

### JavaScript / Fetch API

```javascript
async function printLabel(zplCode, printerIp) {
  const response = await fetch("http://192.168.1.100:5050/print", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      printer_ip: printerIp,
      raw_command: zplCode,
      source: "Web App",
    }),
  });

  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.detail || "Print request failed");
  }
  if (result.success) {
    console.log(`Job sent: ${result.job_id}`);
  } else {
    throw new Error(result.message);
  }
  return result;
}

// Usage
const zpl = `^XA^FO50,50^A0N,50,50^FDProduct: Widget^FS^FO50,120^BY3^BCN,100,Y,N,N^FD123456789^FS^XZ`;
printLabel(zpl, "192.168.1.50");
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
result = print_label(zpl, "192.168.1.50")
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

### Network Printer Issues

```bash
# Test connectivity to the printer
nc -zv <printer-ip> 9100

# Or use the API preflight check
curl "http://localhost:5050/connection?printer_ip=<printer-ip>"
```

## Network Configuration

### Allow Remote Access

The server binds to `0.0.0.0` by default, allowing access from any device on the network.

### Firewall Configuration

```bash
# Windows (run as Administrator)
netsh advfirewall firewall add rule name="Zebra Print Bridge" dir=in action=allow protocol=tcp localport=5050
```

## Performance Tips

1. **Use wired Ethernet** for more reliable network printing
2. **Set a static IP** for the host machine
3. **Use `GET /connection` before large print batches** to fail fast on offline printers

## Project Structure

```
zebra-print-bridge/
├── app/
│   ├── __init__.py        # Version constant
│   ├── config.py          # Configuration management
│   ├── gui.py             # Desktop GUI (CustomTkinter)
│   ├── main.py            # PrintBridge core + CLI entry point
│   ├── printer_manager.py # Network socket printing
│   ├── server.py          # FastAPI HTTP server
│   ├── updater.py         # Auto-update checker (GitHub Raw Content)
│   └── utils.py           # Helper utilities
├── resources/
│   ├── icon.ico           # Application icon (Windows)
│   ├── icon.png           # Application icon (PNG)
│   └── test-client.html   # Web test client UI
├── build.py               # PyInstaller build script
├── installer.iss          # Inno Setup installer script
├── run_gui.py             # GUI entry point
├── version.json           # Auto-update version manifest
└── requirements.txt       # Python dependencies
```

## License

MIT License

## Contributing

1. Fork the repository
2. Create a feature branch
3. Submit a pull request

## Support

For issues and feature requests, please open a GitHub issue.
