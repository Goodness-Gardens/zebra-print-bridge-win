#!/usr/bin/env python3
"""
Zebra Print Bridge – Desktop GUI (CustomTkinter)
Runs the FastAPI server in a background thread and provides a modern control panel.
"""

import logging
import socket
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

# ── Ensure repo root is importable ───────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import __version__
from app.config import Config, get_platform_info
from app.main import PrintBridge, setup_logging
from app.updater import check_for_updates_async, download_installer, launch_installer_and_exit
from app.utils import get_local_ip, get_hostname

# ── Appearance ───────────────────────────────────────────────────────────
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

RESOURCE_DIR = Path(__file__).resolve().parent.parent / "resources"

# Colours – Light professional palette
BG_DARK = "#f0f2f5"         # light gray background
CARD_BG = "#ffffff"          # white cards
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
        self.geometry("820x620")
        self.minsize(720, 520)
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

        # ── Build UI ─────────────────────────────────────────────────────
        self._build_header()
        self._build_status_cards()
        self._build_log_area()
        self._build_footer()

        # ── Logging handler to capture logs in UI ────────────────────────
        self._setup_log_handler()

        # ── Check for updates in background ──────────────────────────────
        check_for_updates_async(self._on_update_check_result)

        # ── Graceful close ───────────────────────────────────────────────
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Auto-start server ────────────────────────────────────────────
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
            left, text=f"v{__version__}  •  Bridge Mode  •  Host: {get_hostname()}",
            font=ctk.CTkFont(size=13), text_color=TEXT_DIM,
        )
        self.subtitle_label.pack(anchor="w", pady=(2, 0))

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.pack(side="right", padx=16, pady=12)

        self.toggle_btn = ctk.CTkButton(
            right, text="⏹  Stop", width=140, height=38,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=RED, hover_color="#b91c1c",
            corner_radius=10, command=self._toggle_server,
        )
        self.toggle_btn.pack(side="right", padx=(8, 0))

        self.dashboard_btn = ctk.CTkButton(
            right, text="🌐  Dashboard", width=140, height=38,
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
        self.ip_label = self._card(cards_frame, 1, "Network IP", self._get_local_ip() or "—", ACCENT)
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
        # Initially hidden
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

    # ── LOG AREA ─────────────────────────────────────────────────────────
    def _build_log_area(self):
        log_frame = ctk.CTkFrame(self, fg_color=CARD_BG, corner_radius=12)
        log_frame.pack(fill="both", expand=True, padx=16, pady=8)

        header_row = ctk.CTkFrame(log_frame, fg_color="transparent")
        header_row.pack(fill="x", padx=14, pady=(10, 4))

        ctk.CTkLabel(
            header_row, text="📋  Recent Activity",
            font=ctk.CTkFont(size=14, weight="bold"), text_color=TEXT_MAIN,
        ).pack(side="left")

        clear_btn = ctk.CTkButton(
            header_row, text="Clear", width=70, height=28,
            font=ctk.CTkFont(size=12),
            fg_color="#e2e8f0", hover_color="#cbd5e1", text_color="#475569",
            corner_radius=6, command=self._clear_logs,
        )
        clear_btn.pack(side="right")

        self.log_text = ctk.CTkTextbox(
            log_frame, font=ctk.CTkFont(family="Consolas", size=12),
            fg_color="#f8fafc", text_color="#334155",
            corner_radius=8, wrap="word", state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

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
        self.update_banner.pack(fill="x", padx=16, pady=(0, 4), before=self.log_text.master)

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

    # ── UTILS ────────────────────────────────────────────────────────────
    @staticmethod
    def _get_local_ip():
        return get_local_ip()

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
