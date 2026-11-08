#!/usr/bin/env python3
"""Apple-inspired desktop interface for SharePoint transcript extraction."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from tkinter import BooleanVar, StringVar, filedialog, messagebox
from urllib.parse import parse_qs, unquote, urlparse

import customtkinter as ctk
from PIL import Image
from dotenv import load_dotenv


APP_DIR = Path(__file__).resolve().parent
SCRIPT = APP_DIR / "extract_stream_transcript.py"
STATE_FILE = APP_DIR / ".pw-sharepoint-profile" / "gui_state.json"
load_dotenv(APP_DIR / ".env")

BG = ("#f5f5f7", "#1c1c1e")
CARD = ("#ffffff", "#2c2c2e")
TEXT = ("#1d1d1f", "#f5f5f7")
SECONDARY = ("#6e6e73", "#aeaeb2")
SEPARATOR = ("#e5e5ea", "#3a3a3c")
BLUE = ("#007aff", "#0a84ff")
BLUE_HOVER = ("#0066d6", "#409cff")
GREEN = ("#248a3d", "#30d158")
RED = ("#ff3b30", "#ff453a")
RED_HOVER = ("#e02d23", "#ff6961")
URL_BG = ("#f2f2f7", "#2c2c2e")
URL_TEXT = ("#3a3a3c", "#f2f2f7")
ENTRY_BG = ("#fbfbfd", "#242426")
NEUTRAL_BUTTON = ("#e9e9ed", "#3a3a3c")
NEUTRAL_HOVER = ("#dcdce1", "#48484a")
SUBTLE_HOVER = ("#e5e5ea", "#48484a")
ROW_BLUE_HOVER = ("#e7f1ff", "#15304d")
ROW_RED_HOVER = ("#fff0ef", "#3a2424")
ACTIVITY_BG = ("#f8f8fa", "#232326")
SCROLLBAR = ("#c7c7cc", "#48484a")
SCROLLBAR_HOVER = ("#aeaeb2", "#636366")


def recording_title_from_url(url: str) -> str:
    """Get the recording filename from SharePoint's encoded `id` parameter."""
    if not url:
        return ""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    candidates = [value for key in ("id", "file", "filename", "title") for value in query.get(key, [])]
    candidates.append(parsed.path)
    ignored = {
        "stream.aspx", "recordings", "recording", "view only", "shared documents",
        "documenti condivisi", "general", "video", "videos", "stream",
        "sites", "_layouts", "_api",
    }
    for index, candidate in enumerate(candidates):
        decoded = unquote(candidate).replace("\\", "/")
        for _ in range(2):
            decoded = unquote(decoded)
        # A generic Stream route such as /_layouts/15/stream.aspx contains no
        # recording title. Treat path segments as titles only when they name a
        # media file; SharePoint's numeric layout version must never be used.
        is_path = index == len(candidates) - 1
        for part in reversed(decoded.split("/")):
            part = part.strip()
            if (not part or part.casefold() in ignored or part.isdigit()
                    or (is_path and not re.search(r"\.(?:mp4|m4v|webm|mov|mkv)$", part, re.IGNORECASE))):
                continue
            title = re.sub(r"\.(?:mp4|m4v|webm|mov|mkv|json|vtt|txt)$", "", part, flags=re.IGNORECASE)
            title = re.sub(
                r"[-_]\d{8}_\d{6}[-_](?:meeting recording|registrazione della riunione).*$",
                "", title, flags=re.IGNORECASE,
            ).strip(" -_")
            if title and not re.fullmatch(r"[0-9a-f-]{24,}", title, flags=re.IGNORECASE):
                return title
    return ""


def recording_folder_name(output_dir: Path, fallback: str, preferred: str = "", url: str = "") -> str:
    try:
        data = json.loads((output_dir / "trascrizione.json").read_text(encoding="utf-8"))
        source = data.get("source") or {}
    except (OSError, json.JSONDecodeError):
        source = {}

    candidates = (
        preferred,
        recording_title_from_url(url),
        recording_title_from_url(str(source.get("page_url") or "")),
        source.get("videoTitle"),
        source.get("displayName"),
        source.get("pageTitle"),
        fallback,
    )
    title = ""
    for candidate in candidates:
        if not candidate:
            continue
        cleaned = re.sub(r"\s*[-|]\s*(microsoft stream|sharepoint).*?$", "", str(candidate), flags=re.IGNORECASE)
        cleaned = re.sub(r"\.(?:json|mp4|webm|vtt|txt)$", "", cleaned.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")[:100].rstrip(" .")
        if cleaned and not re.fullmatch(r"Registrazione_\d+", cleaned, flags=re.IGNORECASE):
            title = cleaned
            break
    if not title:
        title = fallback

    candidate = title
    suffix = 2
    while (output_dir.parent / candidate).exists():
        candidate = f"{title} ({suffix})"
        suffix += 1
    return candidate


@dataclass
class Task:
    url: str
    folder: str
    folder_override: str = ""
    status: str = "In coda"
    iid: str = ""


class TranscriptApp:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.root.title("Unina Transcript Downloader")
        self.root.geometry("1080x800")
        self.root.minsize(820, 660)
        self.root.configure(fg_color=BG)

        if sys.platform == "darwin":
            self.font_family = "SF Pro Display"
        elif os.name == "nt":
            self.font_family = "Segoe UI"
        else:
            self.font_family = "Noto Sans"

        self.output_dir = StringVar(value=str(APP_DIR / "output"))
        self.url_var = StringVar(value=os.environ.get("STREAM_URL", ""))
        self.timeout_minutes = StringVar(value="15")
        self.browser_executable = StringVar(value="")
        self.auto_save_state = BooleanVar(value=True)
        self.dark_mode = BooleanVar(value=False)
        self.tasks: list[Task] = []
        self.messages: queue.Queue[tuple] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_after_current = threading.Event()
        self.active_process: subprocess.Popen[str] | None = None
        self.completed = 0
        self.log_lines = 0
        self.icons = self._load_icons()
        self._set_window_icon()
        self._build_ui()
        self._load_state()
        self.root.after(100, self._poll_messages)

    def _load_icons(self) -> dict[str, ctk.CTkImage]:
        files = {
            "add": ("plus_white.png", "plus_white.png"),
            "settings": ("settings_navy.png", "settings_dark.png"),
            "stop": ("stop_navy.png", "stop_dark.png"),
            "start": ("download_white.png", "download_white.png"),
            "copy": ("copy_blue.png", "copy_dark.png"),
            "task_settings": ("settings_gray.png", "settings_dark.png"),
            "remove": ("trash_red.png", "trash_dark.png"),
        }
        return {
            name: ctk.CTkImage(
                light_image=Image.open(APP_DIR / "assets" / "icons" / light_file),
                dark_image=Image.open(APP_DIR / "assets" / "icons" / dark_file),
                size=(18, 18),
            )
            for name, (light_file, dark_file) in files.items()
        }

    def _set_window_icon(self) -> None:
        try:
            self.window_icon = ImageTkPhotoImage(APP_DIR / "assets" / "app_icon.png")
            self.root.iconphoto(True, self.window_icon)
            ico = APP_DIR / "assets" / "app_icon.ico"
            if os.name == "nt" and ico.exists():
                self.root.iconbitmap(str(ico))
        except Exception:
            pass

    def _build_ui(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        page = ctk.CTkFrame(self.root, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew", padx=34, pady=28)
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=2)
        page.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 22))
        header.grid_columnconfigure(1, weight=1)
        self.header_logo = ctk.CTkImage(
            light_image=Image.open(APP_DIR / "assets" / "logo_header.png"), size=(118, 76)
        )
        ctk.CTkLabel(header, text="", image=self.header_logo, width=118, height=76).grid(
            row=0, column=0, rowspan=2, sticky="w", padx=(0, 18)
        )
        ctk.CTkLabel(header, text="Unina Transcript Downloader", text_color=TEXT,
                     font=ctk.CTkFont(self.font_family, 25, "bold"), anchor="w").grid(
            row=0, column=1, sticky="sw"
        )
        ctk.CTkLabel(header, text="Le tue trascrizioni SharePoint, organizzate in un unico posto",
                     text_color=SECONDARY, font=ctk.CTkFont(self.font_family, 13), anchor="w").grid(
            row=1, column=1, sticky="nw", pady=(4, 0)
        )
        ctk.CTkButton(
            header, text="Impostazioni", image=self.icons["settings"], compound="left",
            command=self.open_settings, width=142, height=40, corner_radius=12,
            fg_color=CARD, hover_color=NEUTRAL_BUTTON, text_color=TEXT,
            font=ctk.CTkFont(self.font_family, 13), border_width=1, border_color=SEPARATOR,
        ).grid(row=0, column=2, rowspan=2, sticky="e", padx=(16, 0))

        add_card = ctk.CTkFrame(page, fg_color=CARD, corner_radius=18, border_width=1,
                                border_color=SEPARATOR)
        add_card.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        add_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(add_card, text="Aggiungi una registrazione", text_color=TEXT,
                     font=ctk.CTkFont(self.font_family, 15, "bold"), anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(16, 8)
        )
        self.url_entry = ctk.CTkEntry(
            add_card, textvariable=self.url_var, height=44, corner_radius=12,
            placeholder_text="Incolla qui il link SharePoint della registrazione",
            border_color=SEPARATOR, fg_color=ENTRY_BG, text_color=TEXT,
            font=ctk.CTkFont(self.font_family, 13),
        )
        self.url_entry.grid(row=1, column=0, sticky="ew", padx=(20, 10), pady=(0, 16))
        self.url_entry.bind("<Return>", lambda _event: self.add_task())
        self.add_button = ctk.CTkButton(
            add_card, text="Aggiungi alla coda", image=self.icons["add"], compound="left",
            command=self.add_task, height=44, corner_radius=12, fg_color=BLUE,
            hover_color=BLUE_HOVER, text_color="white", font=ctk.CTkFont(self.font_family, 13, "bold"),
        )
        self.add_button.grid(row=1, column=1, sticky="e", padx=(0, 20), pady=(0, 16))

        queue_card = ctk.CTkFrame(page, fg_color=CARD, corner_radius=18, border_width=1,
                                  border_color=SEPARATOR)
        queue_card.grid(row=2, column=0, sticky="nsew", pady=(0, 16))
        queue_card.grid_columnconfigure(0, weight=1)
        queue_card.grid_rowconfigure(1, weight=1)
        queue_head = ctk.CTkFrame(queue_card, fg_color="transparent", corner_radius=0)
        queue_head.grid(row=0, column=0, sticky="ew", padx=20, pady=(17, 12))
        queue_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(queue_head, text="Coda trascrizioni", text_color=TEXT,
                     font=ctk.CTkFont(self.font_family, 17, "bold")).grid(row=0, column=0, sticky="w")
        self.count_label = ctk.CTkLabel(
            queue_head, text="0 in coda · 0 completate", text_color=SECONDARY,
            font=ctk.CTkFont(self.font_family, 12),
        )
        self.count_label.grid(row=0, column=1, sticky="e", padx=(10, 14))
        self.stop_button = ctk.CTkButton(
            queue_head, text="Ferma coda", image=self.icons["stop"], compound="left",
            command=self.stop_queue, width=126, height=36, corner_radius=10,
            fg_color=URL_BG, hover_color=SUBTLE_HOVER, text_color=TEXT,
            font=ctk.CTkFont(self.font_family, 12), state="disabled",
        )
        self.stop_button.grid(row=0, column=2, padx=(0, 8))
        self.start_button = ctk.CTkButton(
            queue_head, text="Avvia estrazione", image=self.icons["start"], compound="left",
            command=self.start_queue, width=158, height=36, corner_radius=10,
            fg_color=BLUE, hover_color=BLUE_HOVER, text_color="white",
            font=ctk.CTkFont(self.font_family, 12, "bold"),
        )
        self.start_button.grid(row=0, column=3)

        self.queue_items_frame = ctk.CTkScrollableFrame(
            queue_card, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=SCROLLBAR, scrollbar_button_hover_color=SCROLLBAR_HOVER,
        )
        self.queue_items_frame.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 10))
        self.queue_items_frame.grid_columnconfigure(0, weight=1)

        activity_card = ctk.CTkFrame(page, fg_color=CARD, corner_radius=18, border_width=1,
                                     border_color=SEPARATOR)
        activity_card.grid(row=3, column=0, sticky="nsew")
        activity_card.grid_columnconfigure(0, weight=1)
        activity_card.grid_rowconfigure(1, weight=1)
        activity_head = ctk.CTkFrame(activity_card, fg_color="transparent", corner_radius=0)
        activity_head.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 8))
        activity_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(activity_head, text="Attività", text_color=TEXT,
                     font=ctk.CTkFont(self.font_family, 14, "bold")).grid(row=0, column=0, sticky="w")
        self.clear_button = ctk.CTkButton(
            activity_head, text="Svuota coda", image=self.icons["remove"], compound="left",
            command=self.clear_queue, width=124, height=32, corner_radius=10,
            fg_color="transparent", hover_color=ROW_RED_HOVER, text_color=RED,
            font=ctk.CTkFont(self.font_family, 11, "bold"),
        )
        self.clear_button.grid(row=0, column=1, sticky="e")
        self.log = ctk.CTkTextbox(
            activity_card, height=112, corner_radius=12, border_width=0,
            fg_color=ACTIVITY_BG, text_color=SECONDARY,
            font=ctk.CTkFont(self.font_family, 11), wrap="word",
        )
        self.log.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.log.configure(state="disabled")

    def _render_queue(self) -> None:
        for child in self.queue_items_frame.winfo_children():
            child.destroy()
        if not self.tasks:
            ctk.CTkLabel(
                self.queue_items_frame,
                text="La coda è vuota. Incolla un link SharePoint qui sopra per iniziare.",
                text_color=SECONDARY, font=ctk.CTkFont(self.font_family, 13),
                height=90,
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=12)
        for index, task in enumerate(self.tasks):
            self._render_task_row(task, index)

    def _render_task_row(self, task: Task, index: int) -> None:
        row = ctk.CTkFrame(self.queue_items_frame, fg_color=URL_BG, corner_radius=14)
        row.grid(row=index, column=0, sticky="ew", padx=3, pady=5)
        row.grid_columnconfigure(0, weight=1)
        url_text = task.url if len(task.url) <= 112 else task.url[:109] + "…"
        ctk.CTkLabel(row, text=url_text, text_color=URL_TEXT, anchor="w",
                     font=ctk.CTkFont(self.font_family, 12, "normal"),
                     wraplength=680).grid(row=0, column=0, sticky="ew", padx=(16, 8), pady=(11, 3))

        if task.status == "Completata":
            status_text, status_color = f"Completata  ·  Cartella: {task.folder}", GREEN
        elif task.folder_override and task.status == "In coda":
            status_text, status_color = f"{task.status}  ·  Cartella: {task.folder_override}", SECONDARY
        else:
            status_text, status_color = task.status, (RED if task.status == "Errore" else SECONDARY)
        ctk.CTkLabel(row, text=status_text, text_color=status_color, anchor="w",
                     font=ctk.CTkFont(self.font_family, 11)).grid(
            row=1, column=0, sticky="ew", padx=(16, 8), pady=(0, 11)
        )

        actions = ctk.CTkFrame(row, fg_color="transparent", corner_radius=0)
        actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=(4, 10), pady=8)
        busy = task.status == "In corso"
        ctk.CTkButton(
            actions, text="", image=self.icons["copy"], width=38, height=36, corner_radius=10,
            fg_color=CARD, hover_color=ROW_BLUE_HOVER, command=lambda value=task.url: self.copy_url(value),
        ).pack(side="left", padx=3)
        ctk.CTkButton(
            actions, text="", image=self.icons["task_settings"], width=38, height=36, corner_radius=10,
            fg_color=CARD, hover_color=NEUTRAL_BUTTON, state="disabled" if busy or task.status == "Completata" else "normal",
            command=lambda item=task: self.task_settings(item),
        ).pack(side="left", padx=3)
        ctk.CTkButton(
            actions, text="", image=self.icons["remove"], width=38, height=36, corner_radius=10,
            fg_color=CARD, hover_color=ROW_RED_HOVER, state="disabled" if busy else "normal",
            command=lambda item=task: self.remove_task(item),
        ).pack(side="left", padx=3)

    def open_settings(self) -> None:
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Impostazioni")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(690, max(520, screen_width - 48))
        height = min(760, max(560, screen_height - 80))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.minsize(min(620, width), min(620, height))
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(0, weight=1)
        panel = ctk.CTkScrollableFrame(
            dialog, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=SCROLLBAR,
            scrollbar_button_hover_color=SCROLLBAR_HOVER,
        )
        panel.grid(row=0, column=0, sticky="nsew", padx=26, pady=(20, 8))
        panel.grid_columnconfigure(0, weight=1)

        output_var = StringVar(value=self.output_dir.get())
        timeout_var = StringVar(value=self.timeout_minutes.get())
        browser_var = StringVar(value=self.browser_executable.get())
        save_state_var = BooleanVar(value=self.auto_save_state.get())
        dark_mode_var = BooleanVar(value=self.dark_mode.get())

        ctk.CTkLabel(panel, text="Impostazioni", text_color=TEXT,
                     font=ctk.CTkFont(self.font_family, 24, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 20))

        self._settings_label(panel, "Cartella di destinazione", 1)
        output_row = ctk.CTkFrame(panel, fg_color="transparent", corner_radius=0)
        output_row.grid(row=2, column=0, sticky="ew")
        output_row.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(output_row, textvariable=output_var, height=42, corner_radius=11).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ctk.CTkButton(output_row, text="Scegli cartella", width=132, height=42, corner_radius=11,
                      fg_color=NEUTRAL_BUTTON, hover_color=NEUTRAL_HOVER, text_color=TEXT,
                      command=lambda: self._browse_output(output_var)).grid(row=0, column=1)
        self._settings_hint(panel, "Ogni lezione viene salvata nella propria sottocartella.", 3)

        self._settings_label(panel, "Timeout del browser · minuti", 4)
        ctk.CTkEntry(panel, textvariable=timeout_var, width=110, height=40, corner_radius=11).grid(
            row=5, column=0, sticky="w"
        )
        self._settings_hint(panel, "Imposta 0 per attendere senza limite login e caricamento della pagina.", 6)

        self._settings_label(panel, "Chrome o Chromium personalizzato", 7)
        browser_row = ctk.CTkFrame(panel, fg_color="transparent", corner_radius=0)
        browser_row.grid(row=8, column=0, sticky="ew")
        browser_row.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(browser_row, textvariable=browser_var, height=42, corner_radius=11).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ctk.CTkButton(browser_row, text="Scegli app", width=112, height=42, corner_radius=11,
                      fg_color=NEUTRAL_BUTTON, hover_color=NEUTRAL_HOVER, text_color=TEXT,
                      command=lambda: self._browse_browser(browser_var)).grid(row=0, column=1)
        self._settings_hint(panel, "Lascia vuoto per usare Chromium fornito da Playwright.", 9)

        self._settings_label(panel, "Aspetto", 10)
        ctk.CTkSwitch(
            panel, text="Modalità scura", variable=dark_mode_var,
            progress_color=BLUE,
            font=ctk.CTkFont(self.font_family, 13),
        ).grid(row=11, column=0, sticky="w")
        self._settings_hint(panel, "La modalità scelta si applica dopo aver salvato.", 12)

        self._settings_label(panel, "Sessione e stato", 13)
        ctk.CTkCheckBox(panel, text="Salva automaticamente impostazioni e coda",
                        variable=save_state_var, corner_radius=6, checkbox_width=20,
                        checkbox_height=20, fg_color=BLUE, hover_color=BLUE_HOVER,
                        font=ctk.CTkFont(self.font_family, 13)).grid(row=14, column=0, sticky="w")
        ctk.CTkLabel(
            panel,
            text="La sessione SharePoint resta nel profilo Chromium persistente. Le credenziali continuano a essere lette da .env.",
            text_color=SECONDARY, wraplength=600, justify="left",
            font=ctk.CTkFont(self.font_family, 11),
        ).grid(row=15, column=0, sticky="w", pady=(8, 0))

        buttons = ctk.CTkFrame(dialog, fg_color="transparent", corner_radius=0)
        buttons.grid(row=1, column=0, sticky="ew", padx=26, pady=(8, 16))
        buttons.grid_columnconfigure(0, weight=1)

        def save_settings() -> None:
            try:
                timeout = int(timeout_var.get())
                if timeout < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Timeout non valido", "Inserisci 0 o un numero di minuti positivo.", parent=dialog)
                return
            browser = browser_var.get().strip()
            if browser and not Path(browser).is_file():
                messagebox.showerror("Browser non trovato", "Seleziona un file Chrome/Chromium esistente.", parent=dialog)
                return
            if not output_var.get().strip():
                messagebox.showerror("Cartella mancante", "Scegli la cartella di destinazione.", parent=dialog)
                return
            self.output_dir.set(output_var.get().strip())
            self.timeout_minutes.set(str(timeout))
            self.browser_executable.set(browser)
            self.auto_save_state.set(save_state_var.get())
            self.dark_mode.set(dark_mode_var.get())
            self._save_state()
            dialog.destroy()
            self.root.after(
                50,
                lambda: ctk.set_appearance_mode("Dark" if self.dark_mode.get() else "Light"),
            )

        def cancel_settings() -> None:
            dialog.destroy()

        ctk.CTkButton(buttons, text="Annulla", width=110, height=40, corner_radius=11,
                      fg_color=NEUTRAL_BUTTON, hover_color=NEUTRAL_HOVER, text_color=TEXT,
                      command=cancel_settings).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(buttons, text="Salva modifiche", width=150, height=40, corner_radius=11,
                      fg_color=BLUE, hover_color=BLUE_HOVER, text_color="white",
                      font=ctk.CTkFont(self.font_family, 12, "bold"),
                      command=save_settings).grid(row=0, column=2)
        dialog.bind("<Escape>", lambda _event: cancel_settings())
        dialog.protocol("WM_DELETE_WINDOW", cancel_settings)

    def _settings_label(self, parent, text: str, row: int) -> None:
        ctk.CTkLabel(parent, text=text, text_color=TEXT,
                     font=ctk.CTkFont(self.font_family, 13, "bold")).grid(
            row=row, column=0, sticky="w", pady=(18, 7)
        )

    def _settings_hint(self, parent, text: str, row: int) -> None:
        ctk.CTkLabel(parent, text=text, text_color=SECONDARY,
                     font=ctk.CTkFont(self.font_family, 11), wraplength=600,
                     justify="left").grid(row=row, column=0, sticky="w", pady=(5, 0))

    @staticmethod
    def _browse_output(variable: StringVar) -> None:
        path = filedialog.askdirectory(initialdir=variable.get() or str(APP_DIR),
                                       title="Scegli la cartella di destinazione")
        if path:
            variable.set(path)

    @staticmethod
    def _browse_browser(variable: StringVar) -> None:
        filetypes = [("Chrome / Chromium", "*.exe"), ("Tutti i file", "*.*")] if os.name == "nt" else [("Tutti i file", "*")]
        path = filedialog.askopenfilename(title="Scegli Chrome o Chromium", filetypes=filetypes)
        if path:
            variable.set(path)

    def task_settings(self, task: Task) -> None:
        if task.status != "In coda":
            return
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Impostazioni registrazione")
        dialog.geometry("500x230")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        panel = ctk.CTkFrame(dialog, fg_color="transparent", corner_radius=0)
        panel.pack(fill="both", expand=True, padx=22, pady=20)
        ctk.CTkLabel(panel, text="Nome cartella", text_color=TEXT,
                     font=ctk.CTkFont(self.font_family, 17, "bold")).pack(anchor="w")
        ctk.CTkLabel(panel, text="Lascia vuoto per usare il titolo della registrazione.",
                     text_color=SECONDARY, font=ctk.CTkFont(self.font_family, 11)).pack(anchor="w", pady=(4, 12))
        folder_var = StringVar(value=task.folder_override)
        entry = ctk.CTkEntry(panel, textvariable=folder_var, height=40, corner_radius=11)
        entry.pack(fill="x")
        entry.focus_set()
        result: dict[str, str | None] = {"value": None}
        buttons = ctk.CTkFrame(panel, fg_color="transparent", corner_radius=0)
        buttons.pack(fill="x", pady=(16, 0))
        def accept() -> None:
            result["value"] = folder_var.get().strip()
            dialog.destroy()
        ctk.CTkButton(buttons, text="Annulla", width=100, height=38, corner_radius=10,
                      fg_color=NEUTRAL_BUTTON, hover_color=NEUTRAL_HOVER, text_color=TEXT,
                      command=dialog.destroy).pack(side="right", padx=(8, 0))
        ctk.CTkButton(buttons, text="Salva", width=100, height=38, corner_radius=10,
                      fg_color=BLUE, hover_color=BLUE_HOVER, text_color="white",
                      command=accept).pack(side="right")
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        self.root.wait_window(dialog)
        if result["value"] is None:
            return
        task.folder_override = result["value"]
        self._render_queue()
        self._save_state()

    def add_task(self) -> None:
        url = self.url_var.get().strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            messagebox.showerror("Link non valido", "Incolla un link SharePoint/Stream completo (https://…).")
            return
        if any(item.url == url and item.status in ("In coda", "In corso") for item in self.tasks):
            messagebox.showinfo("Già presente", "Questo link è già nella coda.")
            return
        number = 1
        while True:
            folder = f"Registrazione_{number:03d}"
            if not (Path(self.output_dir.get()).expanduser() / folder).exists() and not any(
                item.folder == folder for item in self.tasks
            ):
                break
            number += 1
        task = Task(url=url, folder=folder)
        task.iid = str(id(task))
        self.tasks.append(task)
        self.url_var.set("")
        self._render_queue()
        self._log("Link aggiunto alla coda")
        self._update_counts()
        self._save_state()

    def remove_task(self, task: Task) -> None:
        if task.status == "In corso" or task not in self.tasks:
            return
        self.tasks.remove(task)
        if task.status == "Completata":
            self.completed = max(0, self.completed - 1)
        self._render_queue()
        self._update_counts()
        self._save_state()

    def copy_url(self, url: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self._log("Link copiato negli appunti")

    def clear_queue(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Coda attiva", "Puoi svuotare la coda quando l'estrazione è terminata.")
            return
        self.tasks.clear()
        self.completed = 0
        self._render_queue()
        self._update_counts()
        self._save_state()

    def start_queue(self) -> None:
        pending = [task for task in self.tasks if task.status == "In coda"]
        if not pending:
            messagebox.showinfo("Coda vuota", "Aggiungi almeno un link SharePoint alla coda.")
            return
        try:
            timeout = int(self.timeout_minutes.get())
            if timeout < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Timeout non valido", "Apri Impostazioni e inserisci 0 o un numero positivo.")
            return
        browser_executable = self.browser_executable.get().strip()
        if browser_executable and not Path(browser_executable).is_file():
            messagebox.showerror("Browser non trovato", "Apri Impostazioni e seleziona un Chrome/Chromium esistente.")
            return
        output_root = Path(self.output_dir.get()).expanduser()
        try:
            output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Cartella non accessibile", str(exc))
            return
        self.stop_after_current.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.clear_button.configure(state="disabled")
        self._save_state()
        self.worker = threading.Thread(
            target=self._run_queue,
            args=(pending, output_root, timeout, browser_executable),
            daemon=True,
        )
        self.worker.start()

    def stop_queue(self) -> None:
        self.stop_after_current.set()
        self.stop_button.configure(state="disabled")
        self._log("La coda si fermerà dopo la registrazione in corso")

    def _run_queue(self, tasks: list[Task], output_root: Path, timeout: int, browser: str) -> None:
        for task in tasks:
            if self.stop_after_current.is_set():
                break
            if task not in self.tasks:
                continue
            task.status = "In corso"
            self.messages.put(("status", task, "In corso"))
            out_dir: Path | None = None
            try:
                out_dir = Path(tempfile.mkdtemp(prefix=".unina-transcript-", dir=output_root))
                command = [sys.executable, "-u", str(SCRIPT), "--url", task.url,
                           "--out-dir", str(out_dir), "--timeout-minutes", str(timeout)]
                if browser:
                    command.extend(["--browser-executable", browser])
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                proc = subprocess.Popen(
                    command, cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
                )
                self.active_process = proc
                assert proc.stdout is not None
                for line in proc.stdout:
                    clean = line.strip()
                    if clean:
                        self.messages.put(("log", clean))
                code = proc.wait()
                if code == 0:
                    folder = recording_folder_name(out_dir, task.folder, task.folder_override, task.url)
                    destination = output_root / folder
                    out_dir.rename(destination)
                    self.messages.put(("folder", task, folder))
                    self.messages.put(("log", f"File salvati in: {destination}"))
                    self.messages.put(("status", task, "Completata"))
                else:
                    self.messages.put(("status", task, "Errore"))
            except Exception as exc:
                self.messages.put(("log", f"Avvio non riuscito: {exc}"))
                self.messages.put(("status", task, "Errore"))
            finally:
                self.active_process = None
                if out_dir is not None and out_dir.exists():
                    shutil.rmtree(out_dir, ignore_errors=True)
        self.messages.put(("finished",))

    def _poll_messages(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            kind = message[0]
            if kind == "log":
                self._log(message[1])
            elif kind == "folder":
                _, task, folder = message
                task.folder = folder
                self._render_queue()
                self._save_state()
            elif kind == "status":
                _, task, status = message
                task.status = status
                self._render_queue()
                if status == "Completata":
                    self.completed += 1
                self._update_counts()
                self._save_state()
            elif kind == "finished":
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.clear_button.configure(state="normal")
                self._log("Coda terminata")
        self.root.after(100, self._poll_messages)

    def _log(self, message: str) -> None:
        from datetime import datetime
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now().strftime('%H:%M:%S')}  {message}\n")
        self.log_lines += 1
        if self.log_lines > 150:
            self.log.delete("1.0", "2.0")
            self.log_lines -= 1
        self.log.see("end")
        self.log.configure(state="disabled")

    def _update_counts(self) -> None:
        waiting = sum(task.status in ("In coda", "In corso") for task in self.tasks)
        self.count_label.configure(text=f"{waiting} in coda  ·  {self.completed} completate")

    def _load_state(self) -> None:
        if not STATE_FILE.is_file():
            self._render_queue()
            return
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            settings = state.get("settings") or {}
            self.output_dir.set(settings.get("output_dir") or self.output_dir.get())
            self.timeout_minutes.set(str(settings.get("timeout_minutes", 15)))
            self.browser_executable.set(settings.get("browser_executable", ""))
            self.auto_save_state.set(bool(settings.get("auto_save_state", True)))
            self.dark_mode.set(bool(settings.get("dark_mode", False)))
            ctk.set_appearance_mode("Dark" if self.dark_mode.get() else "Light")
            output_root = Path(self.output_dir.get()).expanduser()
            for saved in state.get("queue", []):
                url = str(saved.get("url") or "").strip()
                if not url:
                    continue
                folder = str(saved.get("folder") or f"Registrazione_{len(self.tasks) + 1:03d}")
                done = saved.get("status") == "Completata" and (output_root / folder).is_dir()
                task = Task(
                    url=url,
                    folder=folder,
                    folder_override=str(saved.get("folder_override") or ""),
                    status="Completata" if done else "In coda",
                    iid=str(id(saved)),
                )
                self.tasks.append(task)
                if done:
                    self.completed += 1
            self._render_queue()
            self._update_counts()
            if self.tasks:
                self._log("Coda e impostazioni ripristinate")
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self._log(f"Impossibile ripristinare lo stato: {exc}")

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                timeout = max(0, int(self.timeout_minutes.get()))
            except ValueError:
                timeout = 15
            state = {
                "settings": {
                    "output_dir": self.output_dir.get(),
                    "timeout_minutes": timeout,
                    "browser_executable": self.browser_executable.get(),
                    "auto_save_state": self.auto_save_state.get(),
                    "dark_mode": self.dark_mode.get(),
                },
                "queue": [
                    {"url": task.url, "folder": task.folder,
                     "folder_override": task.folder_override, "status": task.status}
                    for task in self.tasks
                ] if self.auto_save_state.get() else [],
            }
            temporary = STATE_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(STATE_FILE)
        except OSError as exc:
            self._log(f"Salvataggio stato non riuscito: {exc}")


def ImageTkPhotoImage(path: Path):
    """Create a Tk-compatible window icon while retaining its reference in the caller."""
    import tkinter as tk
    return tk.PhotoImage(file=str(path))


def main() -> None:
    dark_mode = False
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        dark_mode = bool((saved.get("settings") or {}).get("dark_mode", False))
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    ctk.set_appearance_mode("Dark" if dark_mode else "Light")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    TranscriptApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
