"""
GUI components for the Discord Soundboard.
"""

import hashlib
import json
import os
import shutil
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

import sounddevice as sd

from .audio import AudioMixer, SoundCache
from .constants import (
    COLORS,
    CONFIG_FILE,
    DEFAULT_EMOJIS,
    EMOJI_CATEGORIES,
    IMAGES_DIR,
    SOUNDS_DIR,
    SUPPORTED_FORMATS,
    SUPPORTED_IMAGE_FORMATS,
    UI,
)
from .editor import SoundEditor
from .models import SoundSlot, SoundTab

# Try to import keyboard for global hotkeys
try:
    import keyboard

    HOTKEYS_AVAILABLE = True
except ImportError:
    HOTKEYS_AVAILABLE = False

# Try to import PIL for image support
try:
    from PIL import Image, ImageTk

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class SoundboardApp:
    """Main GUI application for the soundboard."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(UI["window_title"])
        self.root.geometry(UI["window_size"])
        self.root.configure(bg=COLORS["bg_dark"])

        self.mixer: Optional[AudioMixer] = None
        self.sound_cache = SoundCache()  # Local sound storage with caching
        self.tabs: List[SoundTab] = []  # List of all tabs
        self.current_tab_idx = 0  # Currently active tab index
        self.slot_buttons: Dict[int, tk.Button] = {}
        self.slot_frames: Dict[int, tk.Frame] = {}  # Frames containing buttons + progress bars
        self.slot_progress: Dict[int, tk.Canvas] = {}  # Progress bar canvases
        self.slot_images: Dict[int, ImageTk.PhotoImage] = {}  # Keep references to images
        self.registered_hotkeys: list = []
        self.tab_buttons: List[tk.Button] = []

        # Playing state tracking: slot_idx -> {start_time, duration, tab_idx}
        self.playing_slots: Dict[int, Dict] = {}

        # Ensure images directory exists
        Path(IMAGES_DIR).mkdir(exist_ok=True)

        self._setup_styles()
        self._create_ui()
        self._load_config()
        self._preload_sounds()  # Preload all sounds into memory

        # Start animation loop
        self._animate_progress()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_styles(self):
        """Configure ttk styles for Discord-like appearance."""
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=COLORS["bg_dark"])
        style.configure("TLabel", background=COLORS["bg_dark"], foreground=COLORS["text_primary"])
        style.configure("TButton", background=COLORS["blurple"], foreground=COLORS["text_primary"])

    def _create_ui(self):
        """Build the main user interface."""
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        self._create_device_section(main_frame)
        self._create_tab_bar(main_frame)
        self._create_soundboard_section(main_frame)
        self._create_status_bar(main_frame)

    def _create_device_section(self, parent):
        """Create the audio device selection and PTT section."""
        device_frame = ttk.LabelFrame(parent, text="Audio Options", padding=10)
        device_frame.pack(fill=tk.X, pady=(0, 10))

        # Get available devices
        devices = sd.query_devices()
        input_devices = [
            (i, d["name"]) for i, d in enumerate(devices) if d["max_input_channels"] > 0
        ]
        output_devices = [
            (i, d["name"]) for i, d in enumerate(devices) if d["max_output_channels"] > 0
        ]

        # Input device selector
        ttk.Label(device_frame, text="Microphone (Input):").grid(
            row=0, column=0, sticky="w", padx=5
        )
        self.input_var = tk.StringVar()
        self.input_combo = ttk.Combobox(
            device_frame, textvariable=self.input_var, width=40, state="readonly"
        )
        self.input_combo["values"] = [f"{i}: {name}" for i, name in input_devices]
        if input_devices:
            self.input_combo.current(0)
        self.input_combo.grid(row=0, column=1, padx=5, pady=2)

        # Output device selector
        ttk.Label(device_frame, text="Virtual Cable (Output):").grid(
            row=1, column=0, sticky="w", padx=5
        )
        self.output_var = tk.StringVar()
        self.output_combo = ttk.Combobox(
            device_frame, textvariable=self.output_var, width=40, state="readonly"
        )
        self.output_combo["values"] = [f"{i}: {name}" for i, name in output_devices]

        # Auto-select virtual cable if found
        for idx, (i, name) in enumerate(output_devices):
            if "cable" in name.lower() or "virtual" in name.lower():
                self.output_combo.current(idx)
                break
        else:
            if output_devices:
                self.output_combo.current(0)
        self.output_combo.grid(row=1, column=1, padx=5, pady=2)

        # Right side: Start button and PTT
        right_frame = ttk.Frame(device_frame)
        right_frame.grid(row=0, column=2, rowspan=2, padx=10, sticky="nsew")

        # Start/Stop button
        self.toggle_btn = tk.Button(
            right_frame,
            text="▶ Start",
            command=self._toggle_stream,
            bg=COLORS["green"],
            fg="white",
            font=("Segoe UI", 10, "bold"),
            width=12,
        )
        self.toggle_btn.pack(pady=(0, 5))

        # PTT checkbox
        self.ptt_enabled_var = tk.BooleanVar(value=False)
        self.ptt_checkbox = tk.Checkbutton(
            right_frame,
            text="Using PTT?",
            variable=self.ptt_enabled_var,
            command=self._toggle_ptt_visibility,
            bg=COLORS["bg_dark"],
            fg="white",
            selectcolor=COLORS["bg_medium"],
            font=("Segoe UI", 9),
        )
        self.ptt_checkbox.pack()

        # PTT key frame (hidden by default)
        self.ptt_frame = ttk.Frame(device_frame)
        self.ptt_frame.grid(row=2, column=0, columnspan=3, pady=(8, 0), sticky="w")
        self.ptt_frame.grid_remove()  # Hidden initially

        ttk.Label(self.ptt_frame, text="PTT Key:").pack(side=tk.LEFT, padx=5)

        self.ptt_key_var = tk.StringVar(value="")
        self.ptt_entry = ttk.Entry(self.ptt_frame, textvariable=self.ptt_key_var, width=12)
        self.ptt_entry.pack(side=tk.LEFT, padx=5)

        self.ptt_record_btn = tk.Button(
            self.ptt_frame,
            text="Record Key",
            command=self._record_ptt_key,
            bg=COLORS["blurple"],
            fg="white",
            width=10,
        )
        self.ptt_record_btn.pack(side=tk.LEFT, padx=5)

        tk.Button(
            self.ptt_frame,
            text="Clear",
            command=self._clear_ptt_key,
            bg=COLORS["bg_medium"],
            fg="white",
            width=6,
        ).pack(side=tk.LEFT, padx=2)

        self.ptt_status_label = tk.Label(
            self.ptt_frame,
            text="",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_muted"],
            font=("Segoe UI", 8),
        )
        self.ptt_status_label.pack(side=tk.LEFT, padx=10)

        # Mic volume row (at the bottom)
        mic_row = ttk.Frame(device_frame)
        mic_row.grid(row=3, column=0, columnspan=3, pady=(10, 0), sticky="w")

        ttk.Label(mic_row, text="Mic Volume:").pack(side=tk.LEFT, padx=5)

        self.mic_volume_var = tk.DoubleVar(value=100)
        ttk.Scale(
            mic_row,
            from_=0,
            to=150,
            variable=self.mic_volume_var,
            command=self._update_mic_volume,
            length=200,
        ).pack(side=tk.LEFT, padx=5)

        self.mic_mute_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            mic_row,
            text="Mute Mic",
            variable=self.mic_mute_var,
            command=self._toggle_mic_mute,
            bg=COLORS["bg_dark"],
            fg="white",
            selectcolor=COLORS["red"],
        ).pack(side=tk.LEFT, padx=20)

        # Local speaker monitoring checkbox
        self.monitor_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            mic_row,
            text="🔊 Monitor",
            variable=self.monitor_var,
            command=self._toggle_monitor,
            bg=COLORS["bg_dark"],
            fg="white",
            selectcolor=COLORS["green"],
        ).pack(side=tk.LEFT, padx=10)

        tk.Button(
            mic_row,
            text="Stop All Sounds",
            command=self._stop_all_sounds,
            bg=COLORS["red"],
            fg="white",
        ).pack(side=tk.RIGHT, padx=5)

    def _create_tab_bar(self, parent):
        """Create the tab bar with tabs and + button."""
        self.tab_bar_frame = tk.Frame(parent, bg=COLORS["bg_dark"])
        self.tab_bar_frame.pack(fill=tk.X, pady=(0, 5))

        # Container for tab buttons
        self.tabs_container = tk.Frame(self.tab_bar_frame, bg=COLORS["bg_dark"])
        self.tabs_container.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Add tab button
        self.add_tab_btn = tk.Button(
            self.tab_bar_frame,
            text="+",
            command=self._add_new_tab,
            bg=COLORS["green"],
            fg="white",
            font=("Segoe UI", 12, "bold"),
            width=3,
            relief=tk.FLAT,
        )
        self.add_tab_btn.pack(side=tk.RIGHT, padx=5)

    def _refresh_tab_bar(self):
        """Refresh the tab bar buttons."""
        # Clear existing tab buttons
        for btn in self.tab_buttons:
            btn.destroy()
        self.tab_buttons.clear()

        # Create buttons for each tab
        for idx, tab in enumerate(self.tabs):
            display_name = f"{tab.emoji} {tab.name}" if tab.emoji else tab.name
            is_active = idx == self.current_tab_idx

            btn = tk.Button(
                self.tabs_container,
                text=display_name,
                command=lambda i=idx: self._switch_tab(i),
                bg=COLORS["blurple"] if is_active else COLORS["bg_medium"],
                fg="white",
                font=("Segoe UI", 9, "bold" if is_active else "normal"),
                relief=tk.FLAT,
                padx=10,
                pady=3,
            )
            btn.pack(side=tk.LEFT, padx=2)
            btn.bind("<Button-3>", lambda e, i=idx: self._configure_tab(i))
            self.tab_buttons.append(btn)

    def _switch_tab(self, tab_idx: int):
        """Switch to a different tab."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        self.current_tab_idx = tab_idx
        self._refresh_tab_bar()
        self._refresh_slot_buttons()

    def _add_new_tab(self):
        """Add a new tab."""
        dialog = tk.Toplevel(self.root)
        dialog.title("New Tab")
        dialog.geometry("350x200")
        dialog.configure(bg=COLORS["bg_dark"])
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # Name field
        ttk.Label(frame, text="Tab Name:").grid(row=0, column=0, sticky="w", pady=5)
        name_var = tk.StringVar(value=f"Tab {len(self.tabs) + 1}")
        ttk.Entry(frame, textvariable=name_var, width=25).grid(row=0, column=1, pady=5)

        # Emoji field
        ttk.Label(frame, text="Emoji:").grid(row=1, column=0, sticky="w", pady=5)
        emoji_var = tk.StringVar(value="")
        emoji_entry = ttk.Entry(frame, textvariable=emoji_var, width=10)
        emoji_entry.grid(row=1, column=1, sticky="w", pady=5)

        # Emoji picker button
        def pick_emoji():
            self._show_emoji_picker(emoji_var, dialog)

        tk.Button(
            frame,
            text="Choose Emoji",
            command=pick_emoji,
            bg=COLORS["blurple"],
            fg="white",
        ).grid(row=1, column=2, padx=5)

        def save():
            name = name_var.get().strip() or f"Tab {len(self.tabs) + 1}"
            emoji = emoji_var.get().strip() or None
            new_tab = SoundTab(name=name, emoji=emoji)
            self.tabs.append(new_tab)
            self.current_tab_idx = len(self.tabs) - 1
            self._refresh_tab_bar()
            self._refresh_slot_buttons()
            self._save_config()
            dialog.destroy()

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=3, pady=20)

        tk.Button(
            btn_frame, text="Create", command=save, bg=COLORS["green"], fg="white", width=10
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            bg=COLORS["bg_medium"],
            fg="white",
            width=10,
        ).pack(side=tk.LEFT, padx=5)

    def _configure_tab(self, tab_idx: int):
        """Configure or delete a tab."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        tab = self.tabs[tab_idx]

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Edit Tab: {tab.name}")
        dialog.geometry("350x220")
        dialog.configure(bg=COLORS["bg_dark"])
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # Name field
        ttk.Label(frame, text="Tab Name:").grid(row=0, column=0, sticky="w", pady=5)
        name_var = tk.StringVar(value=tab.name)
        ttk.Entry(frame, textvariable=name_var, width=25).grid(row=0, column=1, pady=5)

        # Emoji field
        ttk.Label(frame, text="Emoji:").grid(row=1, column=0, sticky="w", pady=5)
        emoji_var = tk.StringVar(value=tab.emoji or "")
        emoji_entry = ttk.Entry(frame, textvariable=emoji_var, width=10)
        emoji_entry.grid(row=1, column=1, sticky="w", pady=5)

        # Emoji picker button
        def pick_emoji():
            self._show_emoji_picker(emoji_var, dialog)

        tk.Button(
            frame,
            text="Choose Emoji",
            command=pick_emoji,
            bg=COLORS["blurple"],
            fg="white",
        ).grid(row=1, column=2, padx=5)

        def save():
            tab.name = name_var.get().strip() or f"Tab {tab_idx + 1}"
            tab.emoji = emoji_var.get().strip() or None
            self._refresh_tab_bar()
            self._save_config()
            dialog.destroy()

        def delete():
            # Prevent deleting the last tab
            if len(self.tabs) <= 1:
                messagebox.showwarning("Cannot Delete", "You must have at least one tab.")
                return

            if messagebox.askyesno(
                "Delete Tab",
                f"Are you sure you want to delete '{tab.name}'?\nAll sounds in this tab will be lost.",
            ):
                self.tabs.pop(tab_idx)
                # Adjust current tab index if needed
                if self.current_tab_idx >= len(self.tabs):
                    self.current_tab_idx = len(self.tabs) - 1
                self._refresh_tab_bar()
                self._refresh_slot_buttons()
                self._register_hotkeys()
                self._save_config()
                dialog.destroy()

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=3, pady=20)

        tk.Button(
            btn_frame, text="Save", command=save, bg=COLORS["green"], fg="white", width=10
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="Delete Tab", command=delete, bg=COLORS["red"], fg="white", width=10
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            bg=COLORS["bg_medium"],
            fg="white",
            width=10,
        ).pack(side=tk.LEFT, padx=5)

    def _show_emoji_picker(self, target_var: tk.StringVar, parent: tk.Toplevel):
        """Show emoji picker dialog with categories and scrolling."""
        picker = tk.Toplevel(parent)
        picker.title("Choose Emoji")
        picker.geometry("450x500")
        picker.configure(bg=COLORS["bg_dark"])
        picker.transient(parent)
        picker.grab_set()
        picker.resizable(False, False)

        # Main container
        main_frame = tk.Frame(picker, bg=COLORS["bg_dark"])
        main_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Scrollable canvas
        canvas = tk.Canvas(main_frame, bg=COLORS["bg_dark"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=COLORS["bg_dark"])

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Mouse wheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def cleanup_bindings():
            try:
                canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass

        picker.protocol("WM_DELETE_WINDOW", lambda: (cleanup_bindings(), picker.destroy()))

        # Create emoji grid by category
        for category_name, emojis in EMOJI_CATEGORIES.items():
            # Category header
            header = tk.Label(
                scrollable_frame,
                text=category_name,
                font=("Segoe UI", 10, "bold"),
                bg=COLORS["bg_dark"],
                fg=COLORS["text_primary"],
                anchor="w",
            )
            header.pack(fill=tk.X, padx=5, pady=(10, 5))

            # Emoji grid for this category
            emoji_frame = tk.Frame(scrollable_frame, bg=COLORS["bg_dark"])
            emoji_frame.pack(fill=tk.X, padx=5)

            for idx, emoji in enumerate(emojis):
                col = idx % 12
                row = idx // 12
                btn = tk.Button(
                    emoji_frame,
                    text=emoji,
                    font=("Segoe UI Emoji", 12),
                    width=2,
                    height=1,
                    bg=COLORS["bg_medium"],
                    fg="white",
                    relief=tk.FLAT,
                    command=lambda e=emoji: (cleanup_bindings(), target_var.set(e), picker.destroy()),
                )
                btn.grid(row=row, column=col, padx=1, pady=1)

        # Pack canvas and scrollbar
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Button frame at bottom
        btn_frame = tk.Frame(picker, bg=COLORS["bg_dark"])
        btn_frame.pack(fill=tk.X, pady=10)

        tk.Button(
            btn_frame,
            text="Clear",
            command=lambda: (cleanup_bindings(), target_var.set(""), picker.destroy()),
            bg=COLORS["red"],
            fg="white",
            width=10,
        ).pack(side=tk.LEFT, padx=20)

        tk.Button(
            btn_frame,
            text="Cancel",
            command=lambda: (cleanup_bindings(), picker.destroy()),
            bg=COLORS["bg_medium"],
            fg="white",
            width=10,
        ).pack(side=tk.RIGHT, padx=20)

    def _toggle_ptt_visibility(self):
        """Show or hide PTT settings based on checkbox."""
        if self.ptt_enabled_var.get():
            self.ptt_frame.grid()
            # Re-enable PTT in mixer if running
            if self.mixer:
                ptt_key = self.ptt_key_var.get().strip()
                if ptt_key:
                    self.mixer.set_ptt_key(ptt_key)
        else:
            self.ptt_frame.grid_remove()
            # Disable PTT in mixer if running
            if self.mixer:
                self.mixer.set_ptt_key(None)

        # Save config
        self._save_config()

    def _create_soundboard_section(self, parent):
        """Create the soundboard grid section."""
        board_frame = ttk.LabelFrame(
            parent, text="Soundboard (Right-click to configure)", padding=10
        )
        board_frame.pack(fill=tk.BOTH, expand=True)

        self.grid_frame = ttk.Frame(board_frame)
        self.grid_frame.pack(fill=tk.BOTH, expand=True)

        # Create sound slot compound widgets (button + progress bar)
        for i in range(UI["total_slots"]):
            row, col = divmod(i, UI["grid_columns"])

            # Container frame for button and progress bar
            slot_frame = tk.Frame(self.grid_frame, bg=COLORS["bg_dark"])
            slot_frame.grid(row=row, column=col, padx=5, pady=5, sticky="nsew")

            # Sound button
            btn = tk.Button(
                slot_frame,
                text=f"Slot {i+1}\n(Empty)",
                width=18,
                height=4,
                bg=COLORS["bg_medium"],
                fg=COLORS["text_muted"],
                font=("Segoe UI", 9),
                relief=tk.FLAT,
                compound=tk.TOP,  # Image above text
                command=lambda idx=i: self._play_slot(idx),
            )
            btn.pack(fill=tk.BOTH, expand=True)
            btn.bind("<Button-3>", lambda e, idx=i: self._configure_slot(idx))

            # Progress bar canvas (thin bar at bottom)
            progress = tk.Canvas(
                slot_frame,
                height=4,
                bg=COLORS["bg_medium"],
                highlightthickness=0,
            )
            progress.pack(fill=tk.X)

            self.slot_frames[i] = slot_frame
            self.slot_buttons[i] = btn
            self.slot_progress[i] = progress

        # Configure grid weights for resizing
        for i in range(UI["grid_columns"]):
            self.grid_frame.columnconfigure(i, weight=1)
        for i in range(UI["grid_rows"]):
            self.grid_frame.rowconfigure(i, weight=1)

    def _animate_progress(self):
        """Update progress bars for playing sounds."""
        current_time = time.time()
        finished = []

        for slot_idx, play_info in self.playing_slots.items():
            elapsed = current_time - play_info["start_time"]
            duration = play_info["duration"]
            progress_ratio = min(elapsed / duration, 1.0) if duration > 0 else 1.0

            # Update progress bar
            if slot_idx in self.slot_progress:
                canvas = self.slot_progress[slot_idx]
                canvas.delete("progress")
                width = canvas.winfo_width()
                if width > 1:  # Canvas has been rendered
                    fill_width = int(width * progress_ratio)
                    canvas.create_rectangle(
                        0,
                        0,
                        fill_width,
                        4,
                        fill=COLORS["playing"],
                        outline="",
                        tags="progress",
                    )

            # Check if finished
            if progress_ratio >= 1.0:
                finished.append(slot_idx)

        # Reset finished slots
        for slot_idx in finished:
            del self.playing_slots[slot_idx]
            # Only reset button color if this slot is in current tab
            if slot_idx in self.slot_buttons:
                self._update_slot_button(slot_idx)
            # Clear progress bar
            if slot_idx in self.slot_progress:
                self.slot_progress[slot_idx].delete("progress")

        # Schedule next frame (60fps-ish)
        self.root.after(16, self._animate_progress)

    def _refresh_slot_buttons(self):
        """Refresh all slot buttons for current tab."""
        for i in range(UI["total_slots"]):
            self._update_slot_button(i)

    def _create_status_bar(self, parent):
        """Create the status bar at the bottom."""
        self.status_var = tk.StringVar(value="Ready - Select devices and click Start")
        ttk.Label(parent, textvariable=self.status_var, relief=tk.SUNKEN, anchor="w").pack(
            fill=tk.X, pady=(10, 0)
        )

    def _toggle_stream(self):
        """Start or stop the audio stream."""
        if self.mixer and self.mixer.running:
            self.mixer.stop()
            self.toggle_btn.configure(text="▶ Start", bg=COLORS["green"])
            self.status_var.set("Stopped")
        else:
            try:
                input_idx = int(self.input_var.get().split(":")[0])
                output_idx = int(self.output_var.get().split(":")[0])
                self.mixer = AudioMixer(input_idx, output_idx, sound_cache=self.sound_cache)
                # Apply PTT key if enabled and configured
                ptt_key = None
                if self.ptt_enabled_var.get():
                    ptt_key = self.ptt_key_var.get().strip()
                    if ptt_key:
                        self.mixer.set_ptt_key(ptt_key)
                self.mixer.start()
                self.toggle_btn.configure(text="⏹ Stop", bg=COLORS["red"])
                ptt_status = f" (PTT: {ptt_key})" if ptt_key else ""
                self.status_var.set(f"Running - Mic → Virtual Cable{ptt_status}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start:\n{e}")

    def _update_mic_volume(self, _=None):
        """Update microphone volume from slider."""
        if self.mixer:
            self.mixer.mic_volume = self.mic_volume_var.get() / 100.0

    def _toggle_mic_mute(self):
        """Toggle microphone mute state."""
        if self.mixer:
            self.mixer.mic_muted = self.mic_mute_var.get()

    def _toggle_monitor(self):
        """Toggle local speaker monitoring (hear sounds through speakers)."""
        if self.mixer:
            self.mixer.set_monitor_enabled(self.monitor_var.get())

    def _stop_all_sounds(self):
        """Stop all currently playing sounds."""
        if self.mixer:
            self.mixer.stop_all_sounds()

    def _record_ptt_key(self):
        """Record a key or mouse button press to use as PTT key."""
        if not HOTKEYS_AVAILABLE:
            messagebox.showwarning(
                "Keyboard Module Required",
                "The keyboard module is required for PTT functionality.\n"
                "Install it with: pip install keyboard",
            )
            return

        self.ptt_record_btn.config(text="Press key...", bg=COLORS["red"])
        self.ptt_status_label.config(text="Press key or mouse button (5s)...", fg=COLORS["blurple"])
        self.root.update()

        # Store hook references as instance variables so we can unhook them
        self._ptt_hook = None
        self._ptt_mouse_hook = None
        self._ptt_recording = True
        self._ptt_timeout_id = None

        def cleanup_hooks():
            """Clean up all hooks."""
            if self._ptt_hook:
                try:
                    keyboard.unhook(self._ptt_hook)
                except Exception:
                    pass
            if self._ptt_mouse_hook:
                try:
                    import mouse
                    mouse.unhook(self._ptt_mouse_hook)
                except Exception:
                    pass

        def cancel_recording():
            """Cancel recording after timeout."""
            if self._ptt_recording:
                self._ptt_recording = False
                cleanup_hooks()
                self.ptt_record_btn.config(text="Record Key", bg=COLORS["blurple"])
                self.ptt_status_label.config(text="Recording timed out", fg=COLORS["red"])

        def set_ptt_key(key_name: str):
            """Set the PTT key and update UI."""
            if not self._ptt_recording:
                return

            # Cancel the timeout
            if self._ptt_timeout_id:
                self.root.after_cancel(self._ptt_timeout_id)

            # Schedule UI updates on main thread (Tkinter is not thread-safe)
            def update_ui():
                self._ptt_recording = False
                cleanup_hooks()
                self.ptt_key_var.set(key_name)
                self.ptt_record_btn.config(text="Record Key", bg=COLORS["blurple"])
                self.ptt_status_label.config(text=f"PTT: {key_name}", fg=COLORS["green"])

                # Update mixer if running
                if self.mixer:
                    self.mixer.set_ptt_key(key_name)

                # Save config
                self._save_config()

            self.root.after(0, update_ui)

        def on_key(event):
            set_ptt_key(event.name)
            return False  # Stop propagation

        def on_mouse(event):
            # Only capture button down events (not up, move, etc.)
            if hasattr(event, 'event_type') and event.event_type == 'down':
                # Map mouse button names: x = mouse4, x2 = mouse5
                button = event.button
                if button == 'x':
                    button_name = 'mouse4'
                elif button == 'x2':
                    button_name = 'mouse5'
                elif button == 'left':
                    button_name = 'mouse1'
                elif button == 'right':
                    button_name = 'mouse2'
                elif button == 'middle':
                    button_name = 'mouse3'
                else:
                    button_name = f'mouse_{button}'
                set_ptt_key(button_name)

        # Hook keyboard
        self._ptt_hook = keyboard.on_press(on_key, suppress=True)
        
        # Try to hook mouse buttons (side buttons)
        try:
            import mouse
            self._ptt_mouse_hook = mouse.hook(on_mouse)
        except ImportError:
            pass  # Mouse module not available, keyboard only
        except Exception:
            pass
        
        # Set a 5 second timeout
        self._ptt_timeout_id = self.root.after(5000, cancel_recording)

    def _clear_ptt_key(self):
        """Clear the PTT key setting."""
        self.ptt_key_var.set("")
        self.ptt_status_label.config(text="PTT disabled", fg=COLORS["text_muted"])

        # Update mixer if running
        if self.mixer:
            self.mixer.set_ptt_key(None)

        # Save config
        self._save_config()

    def _get_current_tab(self) -> SoundTab:
        """Get the currently active tab."""
        if not self.tabs:
            # Create default tab if none exist
            self.tabs.append(SoundTab(name="Main", emoji="🎵"))
        return self.tabs[self.current_tab_idx]

    def _play_slot(self, slot_idx: int):
        """Play the sound assigned to a slot."""
        tab = self._get_current_tab()
        if slot_idx not in tab.slots:
            return

        slot = tab.slots[slot_idx]

        if self.mixer and self.mixer.running:
            self.mixer.play_sound(slot.file_path, slot.volume)
            self.status_var.set(f"Playing: {slot.name}")

            # Start progress tracking
            duration = self.sound_cache.get_sound_duration(slot.file_path)
            if duration > 0:
                self.playing_slots[slot_idx] = {
                    "start_time": time.time(),
                    "duration": duration,
                    "tab_idx": self.current_tab_idx,
                }
                # Change button color to playing state
                if slot_idx in self.slot_buttons:
                    self.slot_buttons[slot_idx].configure(bg=COLORS["playing"])
        else:
            self.status_var.set("Start the audio stream first!")

    def _configure_slot(self, slot_idx: int):
        """Open configuration dialog for a slot."""
        tab = self._get_current_tab()

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Configure Slot {slot_idx + 1}")
        dialog.geometry("500x400")
        dialog.configure(bg=COLORS["bg_dark"])
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        existing = tab.slots.get(slot_idx)

        # State for edited audio
        edited_audio_data = {"data": None, "sample_rate": None, "original_name": None}

        # Name field
        ttk.Label(frame, text="Name:").grid(row=0, column=0, sticky="w", pady=5)
        name_var = tk.StringVar(value=existing.name if existing else "")
        ttk.Entry(frame, textvariable=name_var, width=35).grid(row=0, column=1, pady=5)

        # File path field
        ttk.Label(frame, text="Sound File:").grid(row=1, column=0, sticky="w", pady=5)
        path_var = tk.StringVar(value=existing.file_path if existing else "")
        path_entry = ttk.Entry(frame, textvariable=path_var, width=35)
        path_entry.grid(row=1, column=1, pady=5)

        # Edit status label
        edit_status_var = tk.StringVar(value="")
        edit_status_label = tk.Label(
            frame,
            textvariable=edit_status_var,
            bg=COLORS["bg_dark"],
            fg=COLORS["green"],
            font=("Segoe UI", 8),
        )
        edit_status_label.grid(row=2, column=1, sticky="w")

        def browse():
            filetypes = [("Audio", " ".join(SUPPORTED_FORMATS))]
            fp = filedialog.askopenfilename(filetypes=filetypes)
            if fp:
                path_var.set(fp)
                if not name_var.get():
                    name_var.set(Path(fp).stem)
                # Open the sound editor
                self._open_sound_editor(fp, edited_audio_data, edit_status_var, dialog)

        def edit_current():
            """Edit the currently selected sound file."""
            current_path = path_var.get()
            if current_path and os.path.exists(current_path):
                self._open_sound_editor(current_path, edited_audio_data, edit_status_var, dialog)
            else:
                messagebox.showwarning("No File", "Please select a sound file first.")

        btn_frame_browse = ttk.Frame(frame)
        btn_frame_browse.grid(row=1, column=2, padx=5)

        ttk.Button(btn_frame_browse, text="Browse", command=browse, width=8).pack(pady=1)
        ttk.Button(btn_frame_browse, text="Edit", command=edit_current, width=8).pack(pady=1)

        # Emoji field
        ttk.Label(frame, text="Emoji:").grid(row=3, column=0, sticky="w", pady=5)
        emoji_var = tk.StringVar(value=existing.emoji if existing and existing.emoji else "")
        emoji_entry = ttk.Entry(frame, textvariable=emoji_var, width=10)
        emoji_entry.grid(row=3, column=1, sticky="w", pady=5)

        def pick_emoji():
            self._show_emoji_picker(emoji_var, dialog)

        tk.Button(
            frame,
            text="Choose Emoji",
            command=pick_emoji,
            bg=COLORS["blurple"],
            fg="white",
        ).grid(row=3, column=2, padx=5, sticky="w")

        # Image field
        ttk.Label(frame, text="Image:").grid(row=4, column=0, sticky="w", pady=5)
        image_var = tk.StringVar(
            value=existing.image_path if existing and existing.image_path else ""
        )
        image_entry = ttk.Entry(frame, textvariable=image_var, width=35)
        image_entry.grid(row=4, column=1, pady=5)

        def browse_image():
            filetypes = [("Images", " ".join(SUPPORTED_IMAGE_FORMATS))]
            fp = filedialog.askopenfilename(filetypes=filetypes)
            if fp:
                # Copy image to local storage
                local_path = self._copy_image_to_storage(fp)
                image_var.set(local_path)

        ttk.Button(frame, text="Browse", command=browse_image, width=8).grid(
            row=4, column=2, padx=5
        )

        # Volume slider
        ttk.Label(frame, text="Volume:").grid(row=5, column=0, sticky="w", pady=5)
        volume_var = tk.DoubleVar(value=(existing.volume * 100) if existing else 100)
        ttk.Scale(frame, from_=0, to=150, variable=volume_var, length=200).grid(
            row=5, column=1, sticky="w"
        )

        # Hotkey field
        ttk.Label(frame, text="Hotkey:").grid(row=6, column=0, sticky="w", pady=5)
        hotkey_var = tk.StringVar(value=existing.hotkey if existing and existing.hotkey else "")
        ttk.Entry(frame, textvariable=hotkey_var, width=20).grid(row=6, column=1, sticky="w")

        def save():
            if not path_var.get() and edited_audio_data["data"] is None:
                dialog.destroy()
                return

            source_path = path_var.get()
            local_path = source_path

            try:
                # If we have edited audio data, save it as a new file
                if (
                    edited_audio_data["data"] is not None
                    and edited_audio_data["sample_rate"] is not None
                ):
                    local_path = self.sound_cache.add_sound_data(
                        edited_audio_data["data"],
                        edited_audio_data["sample_rate"],
                        edited_audio_data["original_name"] or Path(source_path).name,
                    )
                # Otherwise copy original sound to local storage if not already there
                elif not source_path.startswith(str(Path(SOUNDS_DIR).absolute())):
                    local_path = self.sound_cache.add_sound(source_path)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to add sound:\n{e}")
                return

            tab.slots[slot_idx] = SoundSlot(
                name=name_var.get() or Path(source_path).stem,
                file_path=local_path,
                hotkey=hotkey_var.get() or None,
                volume=volume_var.get() / 100.0,
                emoji=emoji_var.get() or None,
                image_path=image_var.get() or None,
            )
            self._update_slot_button(slot_idx)
            self._register_hotkeys()
            self._save_config()
            dialog.destroy()

        def clear():
            if slot_idx in tab.slots:
                slot = tab.slots[slot_idx]
                # Check if any other slot uses this sound before removing from cache
                other_uses = any(
                    s.file_path == slot.file_path
                    for t in self.tabs
                    for idx, s in t.slots.items()
                    if not (t == tab and idx == slot_idx)
                )
                if not other_uses:
                    self.sound_cache.remove_sound(slot.file_path, delete_file=True)
                del tab.slots[slot_idx]
            self._update_slot_button(slot_idx)
            self._register_hotkeys()
            self._save_config()
            dialog.destroy()

        # Button row
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=7, column=0, columnspan=3, pady=20)

        tk.Button(
            btn_frame, text="Save", command=save, bg=COLORS["green"], fg="white", width=10
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="Clear", command=clear, bg=COLORS["red"], fg="white", width=10
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            bg=COLORS["bg_medium"],
            fg="white",
            width=10,
        ).pack(side=tk.LEFT, padx=5)

    def _copy_image_to_storage(self, source_path: str) -> str:
        """Copy an image to local storage and return the local path."""
        Path(IMAGES_DIR).mkdir(exist_ok=True)

        # Generate unique filename using hash
        with open(source_path, "rb") as f:
            file_hash = hashlib.md5(f.read()[:4096]).hexdigest()[:8]

        original_name = Path(source_path).stem
        extension = Path(source_path).suffix
        new_filename = f"{original_name}_{file_hash}{extension}"
        local_path = str(Path(IMAGES_DIR) / new_filename)

        if not os.path.exists(local_path):
            shutil.copy2(source_path, local_path)

        return local_path

    def _open_sound_editor(
        self,
        file_path: str,
        edited_audio_data: dict,
        status_var: tk.StringVar,
        parent_dialog: tk.Toplevel,
    ):
        """Open the sound editor dialog for a file."""
        try:
            # Use default system output device for preview (speakers/headphones)
            # NOT the virtual cable which routes to Discord
            output_device = None  # None = default system output

            # Create and show editor
            editor = SoundEditor(
                self.root,
                file_path,
                output_device=output_device,
            )
            result = editor.show()

            if result is not None:
                audio_data, sample_rate = result
                edited_audio_data["data"] = audio_data
                edited_audio_data["sample_rate"] = sample_rate
                edited_audio_data["original_name"] = Path(file_path).name

                # Calculate duration
                duration = len(audio_data) / sample_rate
                status_var.set(f"✓ Edited ({duration:.2f}s)")
            else:
                # User cancelled - clear edited data if any
                status_var.set("")

        except Exception as e:
            messagebox.showerror("Editor Error", f"Failed to open sound editor:\n{e}")

    def _load_slot_image(
        self, image_path: str, size: tuple = (40, 40)
    ) -> Optional[ImageTk.PhotoImage]:
        """Load and resize an image for a slot button."""
        if not PIL_AVAILABLE:
            return None

        try:
            img = Image.open(image_path)
            img.thumbnail(size, Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _update_slot_button(self, slot_idx: int):
        """Update the appearance of a slot button."""
        btn = self.slot_buttons[slot_idx]
        tab = self._get_current_tab()

        # Determine background color (playing state takes precedence)
        is_playing = slot_idx in self.playing_slots
        bg_color = COLORS["playing"] if is_playing else COLORS["blurple"]

        if slot_idx in tab.slots:
            slot = tab.slots[slot_idx]
            hk = f"\n[{slot.hotkey}]" if slot.hotkey else ""

            # Build display text with emoji
            emoji_prefix = f"{slot.emoji} " if slot.emoji else ""
            display_text = f"{emoji_prefix}{slot.name}{hk}"

            # Try to load image
            photo = None
            if slot.image_path and os.path.exists(slot.image_path):
                photo = self._load_slot_image(slot.image_path)
                if photo:
                    self.slot_images[slot_idx] = photo  # Keep reference

            if photo:
                btn.configure(
                    text=display_text,
                    image=photo,
                    compound=tk.TOP,
                    bg=bg_color,
                    fg="white",
                )
            else:
                btn.configure(
                    text=display_text,
                    image="",
                    compound=tk.TOP,
                    bg=bg_color,
                    fg="white",
                )
        else:
            # Clear image reference if exists
            if slot_idx in self.slot_images:
                del self.slot_images[slot_idx]

            btn.configure(
                text=f"Slot {slot_idx + 1}\n(Empty)",
                image="",
                compound=tk.TOP,
                bg=COLORS["bg_medium"],
                fg=COLORS["text_muted"],
            )

    def _register_hotkeys(self):
        """Register global hotkeys for all slots across all tabs."""
        if not HOTKEYS_AVAILABLE:
            return

        # Unregister existing hotkeys
        for hk in self.registered_hotkeys:
            try:
                keyboard.remove_hotkey(hk)  # type: ignore
            except Exception:
                pass
        self.registered_hotkeys.clear()

        # Register hotkeys for all tabs
        for tab_idx, tab in enumerate(self.tabs):
            for slot_idx, slot in tab.slots.items():
                if slot.hotkey:
                    try:
                        keyboard.add_hotkey(
                            slot.hotkey,
                            lambda t=tab_idx, s=slot_idx: self._play_slot_from_tab(t, s),
                        )
                        self.registered_hotkeys.append(slot.hotkey)
                    except Exception:
                        pass

    def _play_slot_from_tab(self, tab_idx: int, slot_idx: int):
        """Play a sound from a specific tab (for hotkeys)."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        tab = self.tabs[tab_idx]
        if slot_idx not in tab.slots:
            return

        slot = tab.slots[slot_idx]
        if self.mixer and self.mixer.running:
            self.mixer.play_sound(slot.file_path, slot.volume)

            # Start progress tracking (only visible if on current tab)
            duration = self.sound_cache.get_sound_duration(slot.file_path)
            if duration > 0 and tab_idx == self.current_tab_idx:
                self.playing_slots[slot_idx] = {
                    "start_time": time.time(),
                    "duration": duration,
                    "tab_idx": tab_idx,
                }

                # Update UI on main thread
                def update_ui():
                    self.status_var.set(f"Playing: {slot.name}")
                    if slot_idx in self.slot_buttons:
                        self.slot_buttons[slot_idx].configure(bg=COLORS["playing"])

                self.root.after(0, update_ui)
            else:
                # Update status on main thread
                self.root.after(0, lambda: self.status_var.set(f"Playing: {slot.name}"))

    def _save_config(self):
        """Save configuration to JSON file."""
        config = {
            "tabs": [t.to_dict() for t in self.tabs],
            "current_tab": self.current_tab_idx,
            "ptt_enabled": self.ptt_enabled_var.get(),
            "ptt_key": self.ptt_key_var.get().strip() if self.ptt_key_var.get().strip() else None,
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    def _load_config(self):
        """Load configuration from JSON file."""
        if not os.path.exists(CONFIG_FILE):
            # Create default tab
            self.tabs = [SoundTab(name="Main", emoji="🎵")]
            self._refresh_tab_bar()
            return

        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                config = json.load(f)

            # Check if using new tab format or old format
            if "tabs" in config:
                # New format with tabs
                self.tabs = [SoundTab.from_dict(t) for t in config.get("tabs", [])]
                self.current_tab_idx = config.get("current_tab", 0)
            elif "slots" in config:
                # Old format - migrate to new format
                default_tab = SoundTab(name="Main", emoji="🎵")
                for idx, data in config.get("slots", {}).items():
                    default_tab.slots[int(idx)] = SoundSlot.from_dict(data)
                self.tabs = [default_tab]
                self.current_tab_idx = 0

            # Ensure at least one tab exists
            if not self.tabs:
                self.tabs = [SoundTab(name="Main", emoji="🎵")]

            # Ensure current_tab_idx is valid
            if self.current_tab_idx >= len(self.tabs):
                self.current_tab_idx = 0

            # Load PTT settings
            ptt_key = config.get("ptt_key")
            ptt_enabled = config.get("ptt_enabled", ptt_key is not None)  # Backward compat

            if ptt_key:
                self.ptt_key_var.set(ptt_key)
                self.ptt_status_label.config(text=f"PTT: {ptt_key}", fg=COLORS["green"])

            if ptt_enabled:
                self.ptt_enabled_var.set(True)
                self.ptt_frame.grid()  # Show PTT settings

            self._refresh_tab_bar()
            self._refresh_slot_buttons()
            self._register_hotkeys()
        except Exception as e:
            print(f"Error loading config: {e}")
            # Create default tab on error
            self.tabs = [SoundTab(name="Main", emoji="🎵")]
            self._refresh_tab_bar()

    def _preload_sounds(self):
        """Preload all configured sounds into memory cache for fast playback."""
        sound_paths = []
        for tab in self.tabs:
            for slot in tab.slots.values():
                if slot.file_path:
                    sound_paths.append(slot.file_path)

        if sound_paths:
            self.sound_cache.preload_sounds(sound_paths)
            self.status_var.set(f"Ready - {len(sound_paths)} sounds cached")

    def _on_close(self):
        """Handle application close."""
        if self.mixer:
            self.mixer.stop()
        self.root.destroy()

    def run(self):
        """Start the application main loop."""
        self.root.mainloop()
