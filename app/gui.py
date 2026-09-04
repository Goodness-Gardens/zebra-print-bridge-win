#!/usr/bin/env python3
"""
Zebra Print Bridge – Desktop GUI (CustomTkinter)
Runs the FastAPI server in a background thread and provides a modern control panel
with scanned network printers (hostname & IP) and local Windows OS devices.
"""

import logging
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Dict, List

import customtkinter as ctk

# ── Ensure repo root is importable ───────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import __version__
from app.config import Config, get_platform_info
from app.main import PrintBridge, setup_logging
from app.updater import check_for_updates_async, download_installer, launch_installer_and_exit
from app.utils import get_local_ip, get_hostname

logger = logging.getLogger(__name__)

# ── Appearance ───────────────────────────────────────────────────────────
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

RESOURCE_DIR = Path(__file__).resolve().parent.parent / "resources"

# Colours – Light professional palette
BG_DARK = "#f0f2f5"         # light gray background
CARD_BG = "#ffffff"          # white cards
CARD_BORDER = "#e2e8f0"      # border for cards
ACCENT = "#4a6fa5"           # slate blue
ACCENT_HOVER = "#3b5d8e"     # darker slate blue on hover
TEXT_MAIN = "#1e293b"         # dark slate text
TEXT_DIM = "#64748b"          # muted gray
RED = "#dc2626"              # professional red
GREEN = "#16a34a"            # professional green
YELLOW = "#d97706"           # warm amber

LOG_MAX_LINES = 300


class ZebraBridgeApp(ctk.CTk):
    """Main application window."""

    def __init__(self):
        super().__init__()

        # ── Window basics ────────────────────────────────────────────────
        self.title(f"Zebra Print Bridge  v{__version__}")
        self.geometry("900x680")
        self.minsize(780, 560)
        self.configure(fg_color=BG_DARK)

        try:
            icon_path = RESOURCE_DIR / "icon.ico"
            if icon_path.exists():
                self.iconbitmap(str(icon_path))
        except Exception:
            pass  # icon is optional

        # ── State ────────────────────────────────────────────────────────
        self.config_obj = Config()
        self.bridge: PrintBridge | None = None
        self.server_thread: threading.Thread | None = None
        self.server_running = False
        self.update_info = None  # populated by updater
        self.is_scanning_network = False
        self.network_printers: List[Dict] = []
        self.local_printers: List[Dict] = []

        # ── Build UI ─────────────────────────────────────────────────────
        self._build_header()
        self._build_status_cards()
        self._build_tabs()
        self._build_footer()

        # ── Logging handler to capture logs in UI ────────────────────────
        self._setup_log_handler()

        # ── Check for updates in background ──────────────────────────────
        check_for_updates_async(self._on_update_check_result)

        # ── Graceful close ───────────────────────────────────────────────
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Auto-start server ────────────────────────────────────
        self.after(500, self._start_server)

    # ── HEADER ───────────────────────────────────────────────────────────
    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=CARD_BG, corner_radius=12)
        header.pack(fill="x", padx=16, pady=(16, 8))

        left = ctk.CTkFrame(header, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True, padx=16, pady=12)

        title_label = ctk.CTkLabel(
            left, text="🖨  Zebra Print Bridge",
            font=ctk.CTkFont(size=22, weight="bold"), text_color=TEXT_MAIN,
        )
        title_label.pack(anchor="w")

        self.subtitle_label = ctk.CTkLabel(
            left, text=f"v{__version__}  •  Bridge RAW Mode  •  Host: {get_hostname()}",
            font=ctk.CTkFont(size=13), text_color=TEXT_DIM,
        )
        self.subtitle_label.pack(anchor="w", pady=(2, 0))

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.pack(side="right", padx=16, pady=12)

        self.toggle_btn = ctk.CTkButton(
            right, text="⏹  Stop", width=130, height=38,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=RED, hover_color="#b91c1c",
            corner_radius=10, command=self._toggle_server,
        )
        self.toggle_btn.pack(side="right", padx=(8, 0))

        self.dashboard_btn = ctk.CTkButton(
            right, text="🌐  Dashboard", width=130, height=38,
            font=ctk.CTkFont(size=14),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            corner_radius=10, command=self._open_dashboard,
        )
        self.dashboard_btn.pack(side="right")

    # ── STATUS CARDS ─────────────────────────────────────────────────────
    def _build_status_cards(self):
        cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        cards_frame.pack(fill="x", padx=16, pady=4)
        cards_frame.columnconfigure((0, 1, 2, 3), weight=1)

        self.status_indicator = self._card(cards_frame, 0, "Status", "Starting…", YELLOW)
        self.ip_label = self._card(cards_frame, 1, "Network IP", get_local_ip() or "—", ACCENT)
        self.port_label = self._card(cards_frame, 2, "Port", str(self.config_obj.port), ACCENT)
        self.jobs_label = self._card(cards_frame, 3, "Completed Jobs", "0", ACCENT)

        # Update badge (hidden until update found)
        self.update_banner = ctk.CTkFrame(self, fg_color="#fef3c7", corner_radius=10)
        self.update_label = ctk.CTkLabel(
            self.update_banner, text="", font=ctk.CTkFont(size=13),
            text_color=YELLOW,
        )
        self.update_label.pack(side="left", padx=16, pady=8)
        self.update_btn = ctk.CTkButton(
            self.update_banner, text="⬇  Download", width=130, height=32,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=YELLOW, text_color="#ffffff", hover_color="#b45309",
            corner_radius=8, command=self._download_update,
        )
        self.update_btn.pack(side="right", padx=16, pady=8)
        self.update_banner.pack_forget()

    def _card(self, parent, col, title, value, color):
        frame = ctk.CTkFrame(parent, fg_color=CARD_BG, corner_radius=10)
        frame.grid(row=0, column=col, padx=4, pady=4, sticky="nsew")

        ctk.CTkLabel(
            frame, text=title, font=ctk.CTkFont(size=11),
            text_color=TEXT_DIM,
        ).pack(anchor="w", padx=14, pady=(10, 0))

        lbl = ctk.CTkLabel(
            frame, text=value, font=ctk.CTkFont(size=18, weight="bold"),
            text_color=color,
        )
        lbl.pack(anchor="w", padx=14, pady=(2, 10))
        return lbl

    # ── MAIN TABS ────────────────────────────────────────────────────────
    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self, corner_radius=12, fg_color=CARD_BG)
        self.tabs.pack(fill="both", expand=True, padx=16, pady=8)

        # Tab 1: Activity Log (Default View)
        self.tab_logs = self.tabs.add("📋  Activity Log")
        # Tab 2: Printers & Devices
        self.tab_printers = self.tabs.add("🖨  Devices & Printers")

        self._build_logs_tab()
        self._build_printers_tab()
        self.tabs.set("📋  Activity Log")

    # ── PRINTERS TAB ─────────────────────────────────────────────────────
    def _build_printers_tab(self):
        parent = self.tab_printers

        # Top control bar
        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.pack(fill="x", padx=10, pady=(6, 8))

        self.printers_status_label = ctk.CTkLabel(
            toolbar, text="Loading printers...",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=TEXT_DIM,
        )
        self.printers_status_label.pack(side="left", padx=4)

        self.scan_net_btn = ctk.CTkButton(
            toolbar, text="🌐  Scan Network", width=140, height=32,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            corner_radius=8, command=lambda: self._refresh_printers_async(force_subnet_scan=True),
        )
        self.scan_net_btn.pack(side="right", padx=(6, 0))

        self.refresh_btn = ctk.CTkButton(
            toolbar, text="🔄  Refresh", width=110, height=32,
            font=ctk.CTkFont(size=12),
            fg_color="#e2e8f0", hover_color="#cbd5e1", text_color=TEXT_MAIN,
            corner_radius=8, command=lambda: self._refresh_printers_async(force_subnet_scan=False),
        )
        self.refresh_btn.pack(side="right")

        # Two-column container for Network and Local printers
        columns_frame = ctk.CTkFrame(parent, fg_color="transparent")
        columns_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        columns_frame.columnconfigure((0, 1), weight=1)
        columns_frame.rowconfigure(0, weight=1)

        # Left Column: Network Printers
        self.net_column = ctk.CTkFrame(columns_frame, fg_color="#f8fafc", corner_radius=10)
        self.net_column.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=0)

        net_header = ctk.CTkFrame(self.net_column, fg_color="transparent")
        net_header.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            net_header, text="🌐  Network Printers (Zebra TCP/IP)",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT_MAIN,
        ).pack(anchor="w")
        ctk.CTkLabel(
            net_header, text="Discovered on local network via port 9100",
            font=ctk.CTkFont(size=11), text_color=TEXT_DIM,
        ).pack(anchor="w")

        self.net_scroll = ctk.CTkScrollableFrame(self.net_column, fg_color="transparent")
        self.net_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Right Column: Local OS Printers (Windows devices / USB)
        self.local_column = ctk.CTkFrame(columns_frame, fg_color="#f8fafc", corner_radius=10)
        self.local_column.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=0)

        local_header = ctk.CTkFrame(self.local_column, fg_color="transparent")
        local_header.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            local_header, text="💻  Local System Printers (Windows / USB)",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT_MAIN,
        ).pack(anchor="w")
        ctk.CTkLabel(
            local_header, text="Installed devices on operating system",
            font=ctk.CTkFont(size=11), text_color=TEXT_DIM,
        ).pack(anchor="w")

        self.local_scroll = ctk.CTkScrollableFrame(self.local_column, fg_color="transparent")
        self.local_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ── LOGS TAB ─────────────────────────────────────────────────────────
    def _build_logs_tab(self):
        parent = self.tab_logs

        header_row = ctk.CTkFrame(parent, fg_color="transparent")
        header_row.pack(fill="x", padx=10, pady=(6, 6))

        ctk.CTkLabel(
            header_row, text="📋  Recent Activity Log",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT_MAIN,
        ).pack(side="left")

        clear_btn = ctk.CTkButton(
            header_row, text="Clear", width=70, height=28,
            font=ctk.CTkFont(size=12),
            fg_color="#e2e8f0", hover_color="#cbd5e1", text_color="#475569",
            corner_radius=6, command=self._clear_logs,
        )
        clear_btn.pack(side="right")

        self.log_text = ctk.CTkTextbox(
            parent, font=ctk.CTkFont(family="Consolas", size=12),
            fg_color="#f8fafc", text_color="#334155",
            corner_radius=8, wrap="word", state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ── FOOTER ───────────────────────────────────────────────────────────
    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color="transparent", height=28)
        footer.pack(fill="x", padx=16, pady=(0, 10))

        platform_info = get_platform_info()
        plat_text = platform_info.get("platform", f"{platform_info.get('system', '')} {platform_info.get('machine', '')}")

        ctk.CTkLabel(
            footer, text=f"Platform: {plat_text}",
            font=ctk.CTkFont(size=11), text_color=TEXT_DIM,
        ).pack(side="left")

        self.update_link = ctk.CTkLabel(
            footer, text="Check for updates",
            font=ctk.CTkFont(size=11), text_color=ACCENT,
            cursor="hand2"
        )
        self.update_link.bind("<Button-1>", lambda e: self._manual_check_updates())
        self.update_link.bind("<Enter>", lambda e: self.update_link.configure(font=ctk.CTkFont(size=11, underline=True)))
        self.update_link.bind("<Leave>", lambda e: self.update_link.configure(font=ctk.CTkFont(size=11, underline=False)))
        self.update_link.pack(side="right", padx=(8, 0))

        ctk.CTkLabel(
            footer, text=f"v{__version__}  •",
            font=ctk.CTkFont(size=11), text_color=TEXT_DIM,
        ).pack(side="right")

    # ── PRINTER LIST RENDERING & REFRESH ─────────────────────────────────
    def _set_scanning_state(self, is_scanning: bool):
        self.is_scanning_network = is_scanning
        if is_scanning:
            self.scan_net_btn.configure(text="⏳  Scanning...", state="disabled")
            self.printers_status_label.configure(text="Scanning subnet for Zebra printers...")
        else:
            self.scan_net_btn.configure(text="🌐  Scan Network", state="normal")
            count_net = len(self.network_printers)
            count_loc = len(self.local_printers)
            self.printers_status_label.configure(
                text=f"{count_net} network printer(s)  •  {count_loc} local printer(s)"
            )

    def _refresh_printers_async(self, force_subnet_scan: bool = False):
        """Asynchronously refresh network and local printer lists."""
        if not self.bridge:
            return

        def _worker():
            try:
                if force_subnet_scan:
                    self.after(0, lambda: self._set_scanning_state(True))
                    self._append_log_safe("[INFO] Starting subnet scan for Zebra printers...")
                    self.bridge.printer_manager.scan_subnet()
                    self._append_log_safe("[INFO] Subnet scan completed.")
                else:
                    self.bridge.printer_manager.refresh_known_printers()

                net = self.bridge.list_network_printers()
                loc = self.bridge.list_local_printers()

                self.after(0, lambda: self._render_printers(net, loc))
            except Exception as exc:
                logger.error("Error refreshing printers: %s", exc)
                self._append_log_safe(f"[ERROR] Error refreshing printers: {exc}")
            finally:
                self.after(0, lambda: self._set_scanning_state(False))

        threading.Thread(target=_worker, daemon=True).start()

    def _render_printers(self, network_printers: List[Dict], local_printers: List[Dict]):
        self.network_printers = network_printers
        self.local_printers = local_printers
        self._set_scanning_state(False)

        # Clear existing widgets
        for widget in self.net_scroll.winfo_children():
            widget.destroy()
        for widget in self.local_scroll.winfo_children():
            widget.destroy()

        # Render Network Printers
        if not network_printers:
            empty_frame = ctk.CTkFrame(self.net_scroll, fg_color=CARD_BG, corner_radius=8)
            empty_frame.pack(fill="x", padx=4, pady=6)
            ctk.CTkLabel(
                empty_frame,
                text="No Zebra network printers detected.\nClick 'Scan Network' to search subnet.",
                font=ctk.CTkFont(size=12), text_color=TEXT_DIM, justify="center",
            ).pack(padx=16, pady=20)
        else:
            for p in network_printers:
                self._create_network_printer_card(p)

        # Render Local Printers
        if not local_printers:
            empty_frame = ctk.CTkFrame(self.local_scroll, fg_color=CARD_BG, corner_radius=8)
            empty_frame.pack(fill="x", padx=4, pady=6)
            ctk.CTkLabel(
                empty_frame,
                text="No local printers installed in system.",
                font=ctk.CTkFont(size=12), text_color=TEXT_DIM, justify="center",
            ).pack(padx=16, pady=20)
        else:
            for p in local_printers:
                self._create_local_printer_card(p)

    def _create_network_printer_card(self, p: Dict):
        card = ctk.CTkFrame(self.net_scroll, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=CARD_BORDER)
        card.pack(fill="x", padx=4, pady=4)

        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.pack(fill="x", padx=12, pady=(10, 2))

        name = p.get("name", "Zebra Printer")
        ctk.CTkLabel(
            top_row, text=name,
            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT_MAIN,
        ).pack(side="left")

        # Status badge
        ctk.CTkLabel(
            top_row, text="● Online",
            font=ctk.CTkFont(size=11, weight="bold"), text_color=GREEN,
        ).pack(side="right")

        # Hostname & IP details
        info_frame = ctk.CTkFrame(card, fg_color="transparent")
        info_frame.pack(fill="x", padx=12, pady=(2, 8))

        hostname = p.get("hostname") or "—"
        ip = p.get("address") or p.get("ip") or ""
        port = p.get("port", 9100)

        ctk.CTkLabel(
            info_frame, text=f"Hostname:  {hostname}",
            font=ctk.CTkFont(size=12), text_color=TEXT_DIM,
        ).pack(anchor="w")

        ctk.CTkLabel(
            info_frame, text=f"IP:  {ip}:{port}",
            font=ctk.CTkFont(family="Consolas", size=12, weight="bold"), text_color=TEXT_MAIN,
        ).pack(anchor="w", pady=(1, 6))

        # Action buttons
        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 10))

        copy_btn = ctk.CTkButton(
            btn_row, text="📋 Copy IP", width=95, height=26,
            font=ctk.CTkFont(size=11),
            fg_color="#e2e8f0", hover_color="#cbd5e1", text_color=TEXT_MAIN,
            corner_radius=6,
        )
        copy_btn.configure(command=lambda: self._copy_to_clipboard(ip, copy_btn, "📋 Copy IP"))
        copy_btn.pack(side="left", padx=(0, 6))

        test_btn = ctk.CTkButton(
            btn_row, text="🔌 Test", width=80, height=26,
            font=ctk.CTkFont(size=11),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            corner_radius=6,
        )
        test_btn.configure(command=lambda: self._test_network_printer(p, test_btn))
        test_btn.pack(side="left")

    def _create_local_printer_card(self, p: Dict):
        card = ctk.CTkFrame(self.local_scroll, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=CARD_BORDER)
        card.pack(fill="x", padx=4, pady=4)

        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.pack(fill="x", padx=12, pady=(10, 2))

        name = p.get("name", "Local Printer")
        ctk.CTkLabel(
            top_row, text=name,
            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT_MAIN,
        ).pack(side="left")

        # Status badge
        status = (p.get("status") or "available").lower()
        if status in ("ready", "available", "idle"):
            status_text = "● Ready"
            status_color = GREEN
        elif status == "paused":
            status_text = "● Paused"
            status_color = YELLOW
        else:
            status_text = f"● {status.capitalize()}"
            status_color = RED

        ctk.CTkLabel(
            top_row, text=status_text,
            font=ctk.CTkFont(size=11, weight="bold"), text_color=status_color,
        ).pack(side="right")

        # Port and driver details
        info_frame = ctk.CTkFrame(card, fg_color="transparent")
        info_frame.pack(fill="x", padx=12, pady=(2, 8))

        port = p.get("port") or "—"
        driver = p.get("driver") or "—"

        ctk.CTkLabel(
            info_frame, text=f"Port / Connection:  {port}",
            font=ctk.CTkFont(size=12), text_color=TEXT_DIM,
        ).pack(anchor="w")

        ctk.CTkLabel(
            info_frame, text=f"Driver:  {driver}",
            font=ctk.CTkFont(size=11), text_color=TEXT_DIM,
        ).pack(anchor="w", pady=(1, 6))

        # Action buttons
        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 10))

        copy_btn = ctk.CTkButton(
            btn_row, text="📋 Copy Name", width=120, height=26,
            font=ctk.CTkFont(size=11),
            fg_color="#e2e8f0", hover_color="#cbd5e1", text_color=TEXT_MAIN,
            corner_radius=6,
        )
        copy_btn.configure(command=lambda: self._copy_to_clipboard(name, copy_btn, "📋 Copy Name"))
        copy_btn.pack(side="left", padx=(0, 6))

        test_btn = ctk.CTkButton(
            btn_row, text="🔌 Test", width=80, height=26,
            font=ctk.CTkFont(size=11),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            corner_radius=6,
        )
        test_btn.configure(command=lambda: self._test_local_printer(name, test_btn))
        test_btn.pack(side="left")

    def _copy_to_clipboard(self, text: str, btn: ctk.CTkButton, original_text: str):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            btn.configure(text="✔ Copied!")
            self.after(1400, lambda: btn.configure(text=original_text))
        except Exception as exc:
            logger.error("Failed to copy to clipboard: %s", exc)

    def _test_network_printer(self, p: Dict, btn: ctk.CTkButton):
        btn.configure(text="Testing...", state="disabled")
        target_name = p.get("address") or p.get("name")

        def _worker():
            if self.bridge:
                success, msg = self.bridge.printer_manager.test_connection(p)
                status_text = "✔ OK" if success else "✖ Error"
                self.after(0, lambda: btn.configure(text=status_text, state="normal"))
                self.after(2500, lambda: btn.configure(text="🔌 Test"))
                self._append_log_safe(f"[INFO] Connection test to {target_name}: {msg}")

        threading.Thread(target=_worker, daemon=True).start()

    def _test_local_printer(self, name: str, btn: ctk.CTkButton):
        btn.configure(text="Testing...", state="disabled")

        def _worker():
            if self.bridge:
                success, msg = self.bridge.printer_manager.test_local_connection(name)
                status_text = "✔ OK" if success else "✖ Inactive"
                self.after(0, lambda: btn.configure(text=status_text, state="normal"))
                self.after(2500, lambda: btn.configure(text="🔌 Test"))
                self._append_log_safe(f"[INFO] Local printer test '{name}': {msg}")

        threading.Thread(target=_worker, daemon=True).start()

    # ── SERVER CONTROL ───────────────────────────────────────────────────
    def _start_server(self):
        if self.server_running:
            return

        setup_logging(self.config_obj.log_level)
        self._setup_log_handler()  # Restore GUI log handler after basicConfig(force=True)
        self.bridge = PrintBridge(self.config_obj)
        self.server_running = True

        def _run():
            try:
                self.bridge.start()
            except Exception as exc:
                self._append_log(f"[ERROR] Server encountered a fatal error: {exc}")
                self.server_running = False
                self.after(0, lambda: self._update_status_indicator(False))

        self.server_thread = threading.Thread(target=_run, daemon=True)
        self.server_thread.start()

        self._update_status_indicator(True)
        self._append_log("[INFO] Server started successfully.")
        self._start_stats_polling()

        # Initial printer list population
        self.after(600, lambda: self._refresh_printers_async(force_subnet_scan=False))

    def _stop_server(self):
        if not self.server_running or not self.bridge:
            return
        self._append_log("[INFO] Stopping server...")
        self.bridge.stop()
        self.server_running = False
        self._update_status_indicator(False)
        self._append_log("[INFO] Server stopped.")

    def _toggle_server(self):
        if self.server_running:
            self._stop_server()
        else:
            self._start_server()

    def _update_status_indicator(self, running: bool):
        if running:
            self.status_indicator.configure(text="● Running", text_color=GREEN)
            self.toggle_btn.configure(text="⏹  Stop", fg_color=RED, hover_color="#b91c1c")
        else:
            self.status_indicator.configure(text="● Stopped", text_color=RED)
            self.toggle_btn.configure(text="▶  Start", fg_color=GREEN, hover_color="#15803d")

    def _open_dashboard(self):
        port = self.config_obj.port
        webbrowser.open(f"http://localhost:{port}/dashboard")

    # ── STATS POLLING ────────────────────────────────────────────────────
    def _start_stats_polling(self):
        self._poll_stats()

    def _poll_stats(self):
        if self.bridge and self.server_running:
            completed = self.bridge.stats.get("completed", 0)
            self.jobs_label.configure(text=str(completed))
        if self.server_running:
            self.after(3000, self._poll_stats)

    # ── LOG HANDLER → UI ────────────────────────────────────────────────
    def _setup_log_handler(self):
        handler = _GUILogHandler(self._append_log_safe)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(handler)

    def _append_log_safe(self, text: str):
        """Thread-safe wrapper."""
        self.after(0, self._append_log, text)

    def _append_log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        # Trim excess lines
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > LOG_MAX_LINES:
            self.log_text.delete("1.0", f"{lines - LOG_MAX_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_logs(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ── UPDATER ──────────────────────────────────────────────────────────
    def _manual_check_updates(self):
        if hasattr(self, "update_link"):
            self.update_link.configure(text="Checking...")
        self._append_log("[INFO] Checking for updates...")
        
        def _on_result(info):
            if hasattr(self, "update_link"):
                self.after(0, lambda: self.update_link.configure(text="Check for updates"))
            if info:
                ver = info.get("version", "?")
                self._append_log(f"[INFO] Update check complete: New version v{ver} found!")
                self._on_update_check_result(info)
            else:
                self._append_log("[INFO] Update check complete: You are already on the latest version or no update found.")

        check_for_updates_async(_on_result)

    def _on_update_check_result(self, info):
        """Called from updater thread; schedule on main thread."""
        if info:
            self.update_info = info
            self.after(0, self._show_update_banner)

    def _show_update_banner(self):
        if not self.update_info:
            return
        ver = self.update_info.get("version", "?")
        notes = self.update_info.get("notes", "")
        self.update_label.configure(text=f"🔔  New version available: v{ver}  —  {notes}")
        self.update_banner.pack(fill="x", padx=16, pady=(0, 4), before=self.tabs)

    def _download_update(self):
        if not self.update_info:
            return

        url = self.update_info.get("download_url", "")
        if not url:
            self._append_log("[ERROR] Update failed: No download URL provided.")
            return

        self.update_btn.configure(text="Downloading…", state="disabled")
        self._append_log(f"[INFO] Downloading update from: {url}")

        def _worker():
            def _progress(downloaded, total):
                if total > 0:
                    pct = int(downloaded / total * 100)
                    self.after(0, lambda p=pct: self.update_btn.configure(text=f"{p}%"))

            path = download_installer(url, progress_callback=_progress)
            if path:
                self.after(0, lambda: self._append_log(f"[INFO] Update downloaded successfully to: {path}"))
                self.after(0, lambda: self.update_btn.configure(text="Installing…"))
                self.after(1000, lambda: launch_installer_and_exit(path))
            else:
                self.after(0, lambda: self._append_log("[ERROR] Update download failed."))
                self.after(0, lambda: self.update_btn.configure(text="⬇  Retry", state="normal"))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_close(self):
        self._stop_server()
        self.destroy()


class _GUILogHandler(logging.Handler):
    """Sends log records to the GUI callback."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def emit(self, record):
        try:
            msg = self.format(record)
            self._callback(msg)
        except Exception:
            pass


# ── Entry point ──────────────────────────────────────────────────────────
def main():
    if sys.platform == "win32":
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\ZebraPrintBridge_Mutex")
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            # Show a warning instead of quietly failing, so user knows what happened
            ctypes.windll.user32.MessageBoxW(0, "Zebra Print Bridge is already running.", "Zebra Print Bridge", 0x30 | 0x0)
            sys.exit(0)
        # Keep a reference to the mutex so it isn't garbage collected
        global _single_instance_mutex
        _single_instance_mutex = mutex

    app = ZebraBridgeApp()
    app.mainloop()


if __name__ == "__main__":
    main()
