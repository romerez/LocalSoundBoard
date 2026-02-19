"""
GUI components for the Discord Soundboard.
"""

import hashlib
import json
import os
import shutil
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional

import customtkinter as ctk
import sounddevice as sd

from .audio import AudioMixer, SoundCache
from .constants import (
    ALL_SLOT_COLORS,
    COLORS,
    CONFIG_FILE,
    FONTS,
    IMAGES_DIR,
    SLOT_COLORS,
    SOUNDS_DIR,
    SUPPORTED_FORMATS,
    SUPPORTED_IMAGE_FORMATS,
    UI,
    get_text_color_for_bg,
)
from .editor import SoundEditor
from .models import SoundSlot, SoundTab

# Configure CustomTkinter appearance
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

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
        self.root = ctk.CTk()
        self.root.title(UI["window_title"])
        self.root.geometry(UI["window_size"])
        self.root.resizable(False, False)  # Lock window size
        self.root.configure(fg_color=COLORS["bg_darkest"])

        self.mixer: Optional[AudioMixer] = None
        self.sound_cache = SoundCache()  # Local sound storage with caching
        self.tabs: List[SoundTab] = []  # List of all tabs
        self.current_tab_idx = 0  # Currently active tab index
        self.slot_buttons: Dict[int, Any] = {}  # CTkButton instances
        self.slot_frames: Dict[int, Any] = {}  # CTkFrame instances
        self.slot_progress: Dict[int, Any] = {}  # CTkProgressBar instances
        self.slot_preview_buttons: Dict[int, Any] = {}  # CTkButton instances
        self.slot_edit_buttons: Dict[int, Any] = {}  # CTkButton instances
        self.slot_stop_buttons: Dict[int, Any] = {}  # CTkButton instances
        self.slot_bottom_frames: Dict[int, Any] = {}  # CTkFrame instances
        self.slot_images: Dict[int, Any] = {}  # Keep references to images
        self.slot_image_paths: Dict[int, str] = {}  # Track loaded image paths
        self._slot_filled_cache: Dict[int, bool] = {}  # Track filled state to skip redundant updates
        self._last_active_tab_idx: int = 0  # Track last active tab for tab bar optimization
        self.registered_hotkeys: list = []

        # Pre-create cached fonts for performance
        self._font_sm = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"])
        self._font_sm_bold = ctk.CTkFont(
            family=FONTS["family"], size=FONTS["size_sm"], weight="bold"
        )
        self._font_xs = ctk.CTkFont(size=FONTS["size_xs"])
        self._font_xl_bold = ctk.CTkFont(
            family=FONTS["family"], size=FONTS["size_xl"], weight="bold"
        )
        self.tab_buttons: List[Any] = []  # CTkButton instances

        # Playing state tracking: slot_idx -> {start_time, duration, tab_idx}
        self.playing_slots: Dict[int, Dict] = {}

        # Preview state tracking: slot_idx -> {start_time, duration}
        self.preview_slots: Dict[int, Dict] = {}

        # Click / drag state machine  (IDLE → PRESSED → DRAGGING | click)
        # All fields are reset together via _reset_click_state().
        self._click_active: bool = False       # a press is being tracked
        self._click_slot: Optional[int] = None  # slot that was pressed
        self._click_tab: Optional[int] = None   # tab that was active at press
        self._click_start_x: int = 0            # screen-x of press
        self._click_start_y: int = 0            # screen-y of press
        self._click_dragging: bool = False       # drag threshold exceeded

        # Edit mode state (rearrange mode)
        self._edit_mode: bool = False
        self._dragging_slot: Optional[int] = None

        # Persistent across clicks (not reset per-click)
        self._last_play_time: float = 0.0
        self._just_stopped_slot: Optional[int] = None
        self._just_stopped_at: float = 0.0

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
        """Configure ttk styles for Discord-like appearance (legacy support)."""
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=COLORS["bg_dark"])
        style.configure("TLabel", background=COLORS["bg_dark"], foreground=COLORS["text_primary"])
        style.configure("TButton", background=COLORS["blurple"], foreground=COLORS["text_primary"])

    def _create_ui(self):
        """Build the main user interface."""
        main_frame = ctk.CTkFrame(self.root, fg_color=COLORS["bg_darkest"], corner_radius=0)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=UI["padding"], pady=UI["padding"])

        self._create_device_section(main_frame)
        self._create_tab_bar(main_frame)
        self._create_soundboard_section(main_frame)
        self._create_status_bar(main_frame)

    def _create_device_section(self, parent):
        """Create the collapsible audio device selection and PTT section."""
        # Header frame for collapse toggle
        header_frame = ctk.CTkFrame(parent, fg_color="transparent")
        header_frame.pack(fill=tk.X, pady=(0, 8))

        self.audio_options_expanded = tk.BooleanVar(value=False)

        self.toggle_audio_btn = ctk.CTkButton(
            header_frame,
            text="▶ Audio Options",
            command=self._toggle_audio_options,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
            text_color=COLORS["text_secondary"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=32,
            anchor="w",
            width=140,
        )
        self.toggle_audio_btn.pack(side=tk.LEFT)

        # Collapsible content frame
        self.audio_options_frame = ctk.CTkFrame(
            parent, fg_color=COLORS["bg_dark"], corner_radius=UI["corner_radius"]
        )
        # Hidden by default

        # Inner container with padding
        device_frame = ctk.CTkFrame(self.audio_options_frame, fg_color="transparent")
        device_frame.pack(fill=tk.X, padx=12, pady=12)

        # Get available devices
        devices = sd.query_devices()
        input_devices = [
            (i, d["name"]) for i, d in enumerate(devices) if d["max_input_channels"] > 0
        ]
        output_devices = [
            (i, d["name"]) for i, d in enumerate(devices) if d["max_output_channels"] > 0
        ]

        # Device selection row
        device_row = ctk.CTkFrame(device_frame, fg_color="transparent")
        device_row.pack(fill=tk.X, pady=(0, 10))

        # Input device selector
        input_frame = ctk.CTkFrame(device_row, fg_color="transparent")
        input_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        ctk.CTkLabel(
            input_frame,
            text="🎤 Microphone",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w")

        self.input_var = tk.StringVar()
        self.input_combo = ctk.CTkComboBox(
            input_frame,
            variable=self.input_var,
            values=[f"{i}: {name}" for i, name in input_devices],
            width=280,
            height=32,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["border"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
            dropdown_fg_color=COLORS["bg_medium"],
            dropdown_hover_color=COLORS["bg_light"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
        )
        if input_devices:
            self.input_combo.set(f"{input_devices[0][0]}: {input_devices[0][1]}")
        self.input_combo.pack(anchor="w", pady=(4, 0))

        # Output device selector
        output_frame = ctk.CTkFrame(device_row, fg_color="transparent")
        output_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ctk.CTkLabel(
            output_frame,
            text="🔊 Virtual Cable Output",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w")

        self.output_var = tk.StringVar()
        self.output_combo = ctk.CTkComboBox(
            output_frame,
            variable=self.output_var,
            values=[f"{i}: {name}" for i, name in output_devices],
            width=280,
            height=32,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["border"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
            dropdown_fg_color=COLORS["bg_medium"],
            dropdown_hover_color=COLORS["bg_light"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
        )

        # Auto-select virtual cable if found
        selected_output = None
        for idx, (i, name) in enumerate(output_devices):
            if "cable" in name.lower() or "virtual" in name.lower():
                selected_output = f"{i}: {name}"
                break
        if selected_output:
            self.output_combo.set(selected_output)
        elif output_devices:
            self.output_combo.set(f"{output_devices[0][0]}: {output_devices[0][1]}")
        self.output_combo.pack(anchor="w", pady=(4, 0))

        # Controls row (Start button, PTT, etc.)
        controls_row = ctk.CTkFrame(device_frame, fg_color="transparent")
        controls_row.pack(fill=tk.X, pady=(0, 10))

        # Start/Stop button
        self.toggle_btn = ctk.CTkButton(
            controls_row,
            text="▶ Start Stream",
            command=self._toggle_stream,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_md"], weight="bold"),
            corner_radius=UI["button_corner_radius"],
            height=36,
            width=140,
        )
        self.toggle_btn.pack(side=tk.LEFT, padx=(0, 15))

        # PTT checkbox
        self.ptt_enabled_var = tk.BooleanVar(value=False)
        self.ptt_checkbox = ctk.CTkCheckBox(
            controls_row,
            text="Push-to-Talk",
            variable=self.ptt_enabled_var,
            command=self._toggle_ptt_visibility,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.ptt_checkbox.pack(side=tk.LEFT, padx=(0, 15))

        # Mic mute
        self.mic_mute_var = tk.BooleanVar(value=False)
        self.mic_mute_checkbox = ctk.CTkCheckBox(
            controls_row,
            text="Mute Mic",
            variable=self.mic_mute_var,
            command=self._toggle_mic_mute,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.mic_mute_checkbox.pack(side=tk.LEFT, padx=(0, 15))

        # Monitor
        self.monitor_var = tk.BooleanVar(value=True)
        self.monitor_checkbox = ctk.CTkCheckBox(
            controls_row,
            text="🔊 Monitor",
            variable=self.monitor_var,
            command=self._toggle_monitor,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.monitor_checkbox.pack(side=tk.LEFT, padx=(0, 15))

        # Auto-start
        self.auto_start_var = tk.BooleanVar(value=True)
        self.auto_start_checkbox = ctk.CTkCheckBox(
            controls_row,
            text="Auto-Start",
            variable=self.auto_start_var,
            command=self._save_config,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.auto_start_checkbox.pack(side=tk.LEFT)

        # PTT key frame (hidden by default)
        self.ptt_frame = ctk.CTkFrame(device_frame, fg_color="transparent")
        # Hidden initially - will be shown via pack when needed

        ctk.CTkLabel(
            self.ptt_frame,
            text="PTT Key:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.ptt_key_var = tk.StringVar(value="")
        self.ptt_entry = ctk.CTkEntry(
            self.ptt_frame,
            textvariable=self.ptt_key_var,
            width=100,
            height=28,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["border"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
        )
        self.ptt_entry.pack(side=tk.LEFT, padx=(0, 8))

        self.ptt_record_btn = ctk.CTkButton(
            self.ptt_frame,
            text="⏺ Record Key",
            command=self._record_ptt_key,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=28,
            width=100,
        )
        self.ptt_record_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.ptt_clear_btn = ctk.CTkButton(
            self.ptt_frame,
            text="Clear",
            command=self._clear_ptt_key,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=28,
            width=60,
        )
        self.ptt_clear_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.ptt_status_label = ctk.CTkLabel(
            self.ptt_frame,
            text="",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
        )
        self.ptt_status_label.pack(side=tk.LEFT, padx=10)

        # Mic volume row
        mic_row = ctk.CTkFrame(device_frame, fg_color="transparent")
        mic_row.pack(fill=tk.X, pady=(5, 0))

        ctk.CTkLabel(
            mic_row,
            text="Mic Volume:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.mic_volume_var = tk.DoubleVar(value=100)
        self.mic_volume_slider = ctk.CTkSlider(
            mic_row,
            from_=0,
            to=150,
            variable=self.mic_volume_var,
            command=self._update_mic_volume,
            width=200,
            height=16,
            fg_color=COLORS["bg_light"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["text_primary"],
            button_hover_color=COLORS["blurple"],
        )
        self.mic_volume_slider.pack(side=tk.LEFT, padx=(0, 10))

    def _toggle_audio_options(self):
        """Toggle the audio options visibility."""
        if self.audio_options_expanded.get():
            self.audio_options_frame.pack_forget()
            self.toggle_audio_btn.configure(text="▶ Audio Options")
            self.audio_options_expanded.set(False)
        else:
            self.audio_options_frame.pack(
                fill=tk.X, pady=(0, 8), after=self.toggle_audio_btn.master
            )
            self.toggle_audio_btn.configure(text="▼ Audio Options")
            self.audio_options_expanded.set(True)

    def _create_tab_bar(self, parent):
        """Create the tab bar with tabs and + button."""
        self.tab_bar_frame = ctk.CTkFrame(parent, fg_color="transparent", height=40)
        self.tab_bar_frame.pack(fill=tk.X, pady=(0, 8))
        self.tab_bar_frame.pack_propagate(False)

        # Container for tab buttons
        self.tabs_container = ctk.CTkFrame(self.tab_bar_frame, fg_color="transparent")
        self.tabs_container.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Stop All button (always visible)
        self.stop_all_btn = ctk.CTkButton(
            self.tab_bar_frame,
            text="⏹ Stop All",
            command=self._stop_all_sounds,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            corner_radius=UI["button_corner_radius"],
            height=32,
            width=90,
        )
        self.stop_all_btn.pack(side=tk.RIGHT, padx=(8, 0))

        # Add tab button
        self.add_tab_btn = ctk.CTkButton(
            self.tab_bar_frame,
            text="+",
            command=self._add_new_tab,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_lg"], weight="bold"),
            corner_radius=UI["button_corner_radius"],
            height=32,
            width=36,
        )
        self.add_tab_btn.pack(side=tk.RIGHT, padx=(8, 0))

    def _refresh_tab_bar(self):
        """Refresh the tab bar buttons.
        
        Optimization: If the number of tabs hasn't changed, just update existing
        button properties instead of destroying and recreating all buttons.
        This significantly reduces lag when switching tabs.
        """
        # Check if we need to recreate buttons (tab count changed)
        if len(self.tab_buttons) != len(self.tabs):
            # Tab count changed - must recreate all buttons
            for btn in self.tab_buttons:
                btn.destroy()
            self.tab_buttons.clear()

            for idx, tab in enumerate(self.tabs):
                display_name = f"{tab.emoji} {tab.name}" if tab.emoji else tab.name
                is_active = idx == self.current_tab_idx

                btn = ctk.CTkButton(
                    self.tabs_container,
                    text=display_name,
                    command=lambda i=idx: self._switch_tab(i),
                    fg_color=COLORS["blurple"] if is_active else COLORS["bg_medium"],
                    hover_color=COLORS["blurple_hover"] if is_active else COLORS["bg_light"],
                    text_color=COLORS["text_primary"],
                    font=self._font_sm_bold if is_active else self._font_sm,
                    corner_radius=UI["button_corner_radius"],
                    height=32,
                )
                btn.pack(side=tk.LEFT, padx=(0, 4))
                btn.bind("<Button-3>", lambda e, i=idx: self._configure_tab(i))
                self.tab_buttons.append(btn)
        else:
            # Same tab count - just update existing buttons (much faster)
            # Only update the tabs that need visual changes (previously active and newly active)
            for idx, (btn, tab) in enumerate(zip(self.tab_buttons, self.tabs)):
                is_active = idx == self.current_tab_idx
                was_active = getattr(self, '_last_active_tab_idx', -1) == idx
                
                # Only reconfigure if this tab's active state changed
                if is_active or was_active:
                    display_name = f"{tab.emoji} {tab.name}" if tab.emoji else tab.name
                    btn.configure(
                        text=display_name,
                        fg_color=COLORS["blurple"] if is_active else COLORS["bg_medium"],
                        hover_color=COLORS["blurple_hover"] if is_active else COLORS["bg_light"],
                        font=self._font_sm_bold if is_active else self._font_sm,
                    )
            
            # Track which tab was active for next comparison
            self._last_active_tab_idx = self.current_tab_idx

    def _switch_tab(self, tab_idx: int):
        """Switch to a different tab, or move slot if in edit mode."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        # Check if we're in edit mode with a selected slot - move it to this tab
        if self._edit_mode and self._dragging_slot is not None and tab_idx != self.current_tab_idx:
            source_idx = self._dragging_slot
            source_tab = self._click_tab
            self._dragging_slot = None

            if source_idx is not None and source_tab is not None:
                self._move_slot_to_tab(source_idx, source_tab, tab_idx)
            return

        # Don't switch to current tab
        if tab_idx == self.current_tab_idx:
            return

        # Cancel any ongoing interaction
        self._reset_click_state()
        if self._edit_mode:
            self._exit_edit_mode(skip_refresh=True)  # Skip refresh, we'll do it below

        # Clear progress bars before switching (sounds from other tabs shouldn't show here)
        for slot_idx in self.slot_progress:
            self.slot_progress[slot_idx].set(0)

        self.current_tab_idx = tab_idx
        self._refresh_tab_bar()
        self._refresh_slot_buttons()

    def _add_new_tab(self):
        """Add a new tab."""
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("New Tab")
        dialog.geometry("400x220")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.after(10, lambda: dialog.focus_force())

        frame = ctk.CTkFrame(dialog, fg_color=COLORS["bg_dark"], corner_radius=0)
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # Name field
        ctk.CTkLabel(frame, text="Tab Name:", text_color=COLORS["text_primary"]).grid(
            row=0, column=0, sticky="w", pady=10
        )
        name_var = tk.StringVar(value=f"Tab {len(self.tabs) + 1}")
        ctk.CTkEntry(
            frame,
            textvariable=name_var,
            width=200,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=0, column=1, pady=10)

        # Emoji field
        ctk.CTkLabel(frame, text="Emoji:", text_color=COLORS["text_primary"]).grid(
            row=1, column=0, sticky="w", pady=10
        )
        emoji_var = tk.StringVar(value="")
        ctk.CTkEntry(
            frame,
            textvariable=emoji_var,
            width=80,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=1, column=1, sticky="w", pady=10)

        # Emoji picker button
        def pick_emoji():
            self._show_emoji_picker(emoji_var, dialog)

        ctk.CTkButton(
            frame,
            text="Choose Emoji",
            command=pick_emoji,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=100,
        ).grid(row=1, column=2, padx=10)

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
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.grid(row=2, column=0, columnspan=3, pady=25)

        ctk.CTkButton(
            btn_frame,
            text="Create",
            command=save,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)

    def _configure_tab(self, tab_idx: int):
        """Configure or delete a tab."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        tab = self.tabs[tab_idx]

        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Edit Tab: {tab.name}")
        dialog.geometry("400x250")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.after(10, lambda: dialog.focus_force())

        frame = ctk.CTkFrame(dialog, fg_color=COLORS["bg_dark"], corner_radius=0)
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # Name field
        ctk.CTkLabel(frame, text="Tab Name:", text_color=COLORS["text_primary"]).grid(
            row=0, column=0, sticky="w", pady=10
        )
        name_var = tk.StringVar(value=tab.name)
        ctk.CTkEntry(
            frame,
            textvariable=name_var,
            width=200,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=0, column=1, pady=10)

        # Emoji field
        ctk.CTkLabel(frame, text="Emoji:", text_color=COLORS["text_primary"]).grid(
            row=1, column=0, sticky="w", pady=10
        )
        emoji_var = tk.StringVar(value=tab.emoji or "")
        ctk.CTkEntry(
            frame,
            textvariable=emoji_var,
            width=80,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=1, column=1, sticky="w", pady=10)

        # Emoji picker button
        def pick_emoji():
            self._show_emoji_picker(emoji_var, dialog)

        ctk.CTkButton(
            frame,
            text="Choose Emoji",
            command=pick_emoji,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=100,
        ).grid(row=1, column=2, padx=10)

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
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.grid(row=2, column=0, columnspan=3, pady=25)

        ctk.CTkButton(
            btn_frame,
            text="Save",
            command=save,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Delete Tab",
            command=delete,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)

    def _show_emoji_picker(self, target_var: tk.StringVar, parent):
        """Show emoji picker dialog - minimal set with native tk for colored emojis."""
        # Minimal emoji set for instant loading - only the most useful ones
        EMOJIS = [
            # Row 1: Faces
            "😀", "😂", "🤣", "😊", "😍", "🤩", "😎", "🤓", "😜", "😏",
            "😢", "😭", "😤", "😡", "🤬", "😱", "🤮", "🥵", "🥶", "😴",
            # Row 2: More faces & creatures
            "🤔", "🤫", "🤥", "😇", "🥰", "😈", "👿", "💀", "👻", "👽",
            "🤖", "💩", "🤡", "👹", "👺", "🙄", "😬", "🥺", "😵", "🤯",
            # Row 3: Gestures
            "👍", "👎", "👊", "✊", "🤛", "🤜", "👏", "🙌", "👐", "🤝",
            "🙏", "✌️", "🤟", "🤘", "👌", "🤌", "👈", "👉", "👆", "👇",
            # Row 4: Music & Sound
            "🎵", "🎶", "🎤", "🎧", "🎷", "🎸", "🎹", "🎺", "🥁", "🔔",
            "🔊", "🔉", "🔈", "🔇", "📢", "📣", "💬", "💭", "🗯️", "💥",
            # Row 5: Hearts & Stars
            "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "💔", "❣️",
            "💕", "💖", "💗", "💘", "💝", "⭐", "🌟", "✨", "💫", "🔥",
            # Row 6: Activities & Games
            "🎮", "🕹️", "🎲", "🎯", "🎳", "🏆", "🥇", "🥈", "🥉", "⚽",
            "🏀", "🏈", "⚾", "🎾", "🏓", "🎱", "🎰", "🃏", "🀄", "🎴",
            # Row 7: Animals
            "🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼", "🐯", "🦁",
            "🐮", "🐷", "🐸", "🐵", "🐔", "🐧", "🦆", "🦅", "🦉", "🐺",
            # Row 8: Signs & Symbols
            "✅", "❌", "⚠️", "🚫", "⛔", "❓", "❗", "💯", "🔴", "🟢",
            "🔵", "🟡", "🟠", "🟣", "⚫", "⚪", "▶️", "⏸️", "⏹️", "🔄",
        ]

        picker = tk.Toplevel(parent)
        picker.title("Choose Emoji")
        picker.geometry("380x340")
        picker.transient(parent)
        picker.grab_set()
        picker.resizable(False, False)
        picker.configure(bg=COLORS["bg_dark"])
        picker.after(10, lambda: picker.focus_force())

        def cleanup_and_select(emoji_val):
            target_var.set(emoji_val)
            picker.destroy()

        # Title
        title = tk.Label(
            picker,
            text="Select an Emoji",
            font=("Segoe UI", 11, "bold"),
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
        )
        title.pack(pady=(10, 5))

        # Emoji grid frame
        grid_frame = tk.Frame(picker, bg=COLORS["bg_dark"])
        grid_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Create emoji buttons - native tk.Label renders colored emojis on Windows
        for idx, emoji_char in enumerate(EMOJIS):
            row = idx // 10
            col = idx % 10
            lbl = tk.Label(
                grid_frame,
                text=emoji_char,
                font=("Segoe UI Emoji", 20),
                bg=COLORS["bg_medium"],
                cursor="hand2",
                width=2,
                height=1,
                relief="flat",
            )
            lbl.grid(row=row, column=col, padx=2, pady=2)
            lbl.bind("<Button-1>", lambda e, em=emoji_char: cleanup_and_select(em))
            lbl.bind("<Enter>", lambda e, l=lbl: l.configure(bg=COLORS["bg_light"]))
            lbl.bind("<Leave>", lambda e, l=lbl: l.configure(bg=COLORS["bg_medium"]))

        # Button frame at bottom
        btn_frame = tk.Frame(picker, bg=COLORS["bg_dark"])
        btn_frame.pack(fill=tk.X, pady=10, padx=10)

        clear_btn = tk.Button(
            btn_frame,
            text="Clear",
            font=("Segoe UI", 10),
            bg=COLORS["red"],
            fg="white",
            activebackground=COLORS["red_hover"],
            activeforeground="white",
            relief="flat",
            cursor="hand2",
            width=10,
            command=lambda: cleanup_and_select(""),
        )
        clear_btn.pack(side=tk.LEFT, padx=5)

        cancel_btn = tk.Button(
            btn_frame,
            text="Cancel",
            font=("Segoe UI", 10),
            bg=COLORS["bg_medium"],
            fg="white",
            activebackground=COLORS["bg_light"],
            activeforeground="white",
            relief="flat",
            cursor="hand2",
            width=10,
            command=picker.destroy,
        )
        cancel_btn.pack(side=tk.RIGHT, padx=5)

    def _toggle_ptt_visibility(self):
        """Show or hide PTT settings based on checkbox."""
        if self.ptt_enabled_var.get():
            self.ptt_frame.pack(fill=tk.X, pady=(8, 0))
            # Re-enable PTT in mixer if running
            if self.mixer:
                ptt_key = self.ptt_key_var.get().strip()
                if ptt_key:
                    self.mixer.set_ptt_key(ptt_key)
        else:
            self.ptt_frame.pack_forget()
            # Disable PTT in mixer if running
            if self.mixer:
                self.mixer.set_ptt_key(None)

        # Save config
        self._save_config()

    def _create_soundboard_section(self, parent):
        """Create the soundboard grid section with scrolling."""
        # Modern card-style container
        board_frame = ctk.CTkFrame(
            parent,
            fg_color=COLORS["bg_dark"],
            corner_radius=UI["corner_radius"],
        )
        board_frame.pack(fill=tk.BOTH, expand=True)

        # Header with label and edit mode button
        header_frame = ctk.CTkFrame(board_frame, fg_color="transparent")
        header_frame.pack(fill=tk.X, padx=12, pady=(8, 4))

        header_label = ctk.CTkLabel(
            header_frame,
            text="Soundboard",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            text_color=COLORS["text_secondary"],
        )
        header_label.pack(side=tk.LEFT)

        # Edit/Move mode button
        self.edit_mode_btn = ctk.CTkButton(
            header_frame,
            text="↔ Move",
            font=self._font_xs,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            text_color=COLORS["text_muted"],
            corner_radius=4,
            width=70,
            height=22,
            command=self._toggle_edit_mode,
        )
        self.edit_mode_btn.pack(side=tk.RIGHT)

        # Create scrollable canvas (using tk.Canvas for scroll support)
        canvas_frame = ctk.CTkFrame(board_frame, fg_color="transparent")
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.slots_canvas = tk.Canvas(
            canvas_frame,
            bg=COLORS["bg_dark"],
            highlightthickness=0,
            bd=0,
        )
        self.slots_scrollbar = ctk.CTkScrollbar(
            canvas_frame,
            orientation="vertical",
            command=self.slots_canvas.yview,
            fg_color=COLORS["bg_medium"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
        )
        self.slots_canvas.configure(yscrollcommand=self.slots_scrollbar.set)

        # Pack canvas (scrollbar will be shown/hidden dynamically based on content)
        self.slots_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Frame inside canvas for the grid
        self.grid_frame = ctk.CTkFrame(self.slots_canvas, fg_color=COLORS["bg_dark"])
        self.grid_frame_window = self.slots_canvas.create_window(
            (0, 0), window=self.grid_frame, anchor="nw"
        )

        # Bind resize events
        self.grid_frame.bind("<Configure>", self._on_grid_configure)
        self.slots_canvas.bind("<Configure>", self._on_canvas_configure)

        # Enable mousewheel scrolling only when hovering over the soundboard area
        self.slots_canvas.bind("<Enter>", self._on_canvas_enter)
        self.slots_canvas.bind("<Leave>", self._on_canvas_leave)

        # Create initial slots (will be populated in _refresh_slot_buttons)
        self._create_slot_widgets()

    def _on_grid_configure(self, event):
        """Update scroll region when grid changes size."""
        self.slots_canvas.configure(scrollregion=self.slots_canvas.bbox("all"))
        # Show/hide scrollbar based on content height vs canvas height
        self._update_scrollbar_visibility()

    def _on_canvas_configure(self, event):
        """Update grid frame width to match canvas width."""
        # Make grid frame at least as wide as canvas
        self.slots_canvas.itemconfig(self.grid_frame_window, width=event.width)
        # Show/hide scrollbar based on content height vs canvas height
        self._update_scrollbar_visibility()

    def _update_scrollbar_visibility(self):
        """Show scrollbar only when content exceeds visible area."""
        # Get the bounding box of content
        bbox = self.slots_canvas.bbox("all")
        if bbox is None:
            return
        content_height = bbox[3] - bbox[1]
        canvas_height = self.slots_canvas.winfo_height()

        # Canvas returns 1 before it's properly mapped - ignore these cases
        if canvas_height <= 1:
            return

        # Add small buffer (5px) to prevent edge cases where it flickers
        if content_height > canvas_height + 5:
            # Content is larger than canvas - show scrollbar
            if not self.slots_scrollbar.winfo_ismapped():
                self.slots_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        else:
            # Content fits - hide scrollbar
            if self.slots_scrollbar.winfo_ismapped():
                self.slots_scrollbar.pack_forget()

    def _on_canvas_enter(self, event):
        """Enable scrolling when mouse enters the soundboard area."""
        self.slots_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_canvas_leave(self, event):
        """Disable scrolling when mouse leaves the soundboard area."""
        self.slots_canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        """Handle mouse wheel scrolling."""
        if not self.slots_scrollbar.winfo_ismapped():
            return
        self.slots_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _create_slot_widgets(self):
        """Dynamically create slot widgets based on current tab content."""
        # Clear existing slots and all associated state
        for frame in self.slot_frames.values():
            frame.destroy()
        self.slot_frames.clear()
        self.slot_buttons.clear()
        self.slot_progress.clear()
        self.slot_preview_buttons.clear()
        self.slot_edit_buttons.clear()
        self.slot_stop_buttons.clear()
        self.slot_bottom_frames.clear()
        self._slot_filled_cache.clear()  # Reset filled state cache when recreating widgets

        # Calculate how many slots we need: max slot index + 1 extra empty, minimum 12
        tab = self._get_current_tab()
        max_idx = max(tab.slots.keys()) if tab.slots else -1
        num_slots = max(max_idx + 2, UI["total_slots"])  # +2 to have at least 1 empty

        # Fixed slot dimensions - sized to fit 4 columns nicely in window
        SLOT_WIDTH = 180
        SLOT_HEIGHT = 110  # Increased for better text visibility
        BOTTOM_HEIGHT = 32  # Height for progress bar and buttons

        # Create sound slot compound widgets (button + progress bar)
        for i in range(num_slots):
            row, col = divmod(i, UI["grid_columns"])

            # Container frame for button and progress bar - modern card style
            slot_frame = ctk.CTkFrame(
                self.grid_frame,
                fg_color=COLORS["bg_medium"],
                corner_radius=UI["slot_corner_radius"],
                width=SLOT_WIDTH,
                height=SLOT_HEIGHT + BOTTOM_HEIGHT,
            )
            slot_frame.grid(row=row, column=col, padx=4, pady=4)
            slot_frame.grid_propagate(False)  # Lock frame size
            slot_frame.pack_propagate(False)  # Lock frame size

            # Bottom row: progress bar + edit button + preview button (pack first so it's at bottom)
            bottom_frame = ctk.CTkFrame(slot_frame, fg_color="transparent", height=BOTTOM_HEIGHT)
            bottom_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(0, 6))

            # Stop button (hidden by default, shown when playing)
            def make_stop_handler(slot_id):
                def handler(event=None):
                    self._stop_slot_with_flag(slot_id)
                    return "break"

                return handler

            stop_btn = ctk.CTkButton(
                bottom_frame,
                text="⏹",
                fg_color=COLORS["red"],
                hover_color=COLORS["red_hover"],
                font=self._font_sm,
                corner_radius=4,
                width=28,
                height=24,
                cursor="hand2",
            )
            stop_handler = make_stop_handler(i)
            stop_btn.configure(command=stop_handler)
            # Use CTkButton's bind() which stores bindings and reapplies them
            # when internal canvas/label children are recreated on first display.
            stop_btn.bind("<Button-1>", stop_handler)
            # Don't pack initially - only show when playing

            # Preview button (speaker icon) - pack RIGHT first
            preview_btn = ctk.CTkButton(
                bottom_frame,
                text="🔊",
                fg_color=COLORS["bg_light"],
                hover_color=COLORS["bg_lighter"],
                text_color=COLORS["text_muted"],
                font=self._font_xs,
                corner_radius=4,
                width=28,
                height=24,
                command=lambda idx=i: self._preview_slot(idx),
            )
            preview_btn.pack(side=tk.RIGHT, padx=(2, 0))

            # Edit button (pencil icon)
            edit_btn = ctk.CTkButton(
                bottom_frame,
                text="✏️",
                fg_color=COLORS["bg_light"],
                hover_color=COLORS["bg_lighter"],
                text_color=COLORS["text_muted"],
                font=self._font_xs,
                corner_radius=4,
                width=28,
                height=24,
                command=lambda idx=i: self._configure_slot(idx),
            )
            edit_btn.pack(side=tk.RIGHT, padx=(2, 0))

            # Progress bar using CTkProgressBar
            progress = ctk.CTkProgressBar(
                bottom_frame,
                height=6,
                fg_color=COLORS["bg_light"],
                progress_color=COLORS["playing"],
                corner_radius=3,
            )
            progress.set(0)
            progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

            # Sound button - modern card-style button
            #
            # command= is the PRIMARY click mechanism.  It is the only callback
            # that reliably survives CTkButton._draw() (which re-binds <Button-1>
            # on the internal canvas, wiping any raw canvas.bind() we add).
            # CTkButton stores _command as a property and calls it from _clicked().
            btn = ctk.CTkButton(
                slot_frame,
                text="+",
                fg_color="transparent",
                hover_color=COLORS["bg_light"],
                text_color=COLORS["text_muted"],
                font=self._font_xl_bold,
                corner_radius=UI["slot_corner_radius"],
                anchor="center",
                cursor="hand2",
                command=lambda idx=i: self._on_slot_command(idx),
            )
            btn.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=(0, 0))

            # Right-click context menu binding (drag removed from main button)
            btn.bind("<Button-3>", lambda e, idx=i: self._show_quick_popup(e, idx))

            self.slot_frames[i] = slot_frame
            self.slot_buttons[i] = btn
            self.slot_progress[i] = progress
            self.slot_preview_buttons[i] = preview_btn
            self.slot_edit_buttons[i] = edit_btn
            self.slot_stop_buttons[i] = stop_btn
            self.slot_bottom_frames[i] = bottom_frame

        # Schedule scrollbar visibility update after layout is complete
        self.root.after(10, self._update_scrollbar_visibility)

    def _animate_progress(self):
        """Update progress bars for playing sounds."""
        current_time = time.time()
        finished = []

        for slot_idx, play_info in self.playing_slots.items():
            elapsed = current_time - play_info["start_time"]
            duration = play_info["duration"]
            progress_ratio = min(elapsed / duration, 1.0) if duration > 0 else 1.0

            # Only update progress bar if this slot's sound is from the current tab
            if play_info["tab_idx"] == self.current_tab_idx:
                if slot_idx in self.slot_progress:
                    # Just update the value - color is set when playback starts
                    self.slot_progress[slot_idx].set(progress_ratio)

            # Check if finished
            if progress_ratio >= 1.0:
                finished.append(slot_idx)

        # Reset finished playing slots
        for slot_idx in finished:
            tab_idx = self.playing_slots[slot_idx]["tab_idx"]
            del self.playing_slots[slot_idx]
            # Only reset button color if this slot is in current tab AND from current tab
            if slot_idx in self.slot_buttons and tab_idx == self.current_tab_idx:
                self._update_slot_button(slot_idx)
            # Clear progress bar only if from current tab
            if slot_idx in self.slot_progress and tab_idx == self.current_tab_idx:
                self.slot_progress[slot_idx].set(0)
            # Hide stop button
            if slot_idx in self.slot_stop_buttons and tab_idx == self.current_tab_idx:
                self.slot_stop_buttons[slot_idx].pack_forget()

        # Handle preview slots (same logic but with green color)
        preview_finished = []

        for slot_idx, play_info in self.preview_slots.items():
            elapsed = current_time - play_info["start_time"]
            duration = play_info["duration"]
            progress_ratio = min(elapsed / duration, 1.0) if duration > 0 else 1.0

            # Only update progress bar if this slot's sound is from the current tab
            if play_info["tab_idx"] == self.current_tab_idx:
                if slot_idx in self.slot_progress:
                    # Just update the value - color is set when preview starts
                    self.slot_progress[slot_idx].set(progress_ratio)

            # Check if finished
            if progress_ratio >= 1.0:
                preview_finished.append(slot_idx)

        # Reset finished preview slots
        for slot_idx in preview_finished:
            tab_idx = self.preview_slots[slot_idx]["tab_idx"]
            del self.preview_slots[slot_idx]
            # Only reset button color if this slot is in current tab AND from current tab
            if slot_idx in self.slot_buttons and tab_idx == self.current_tab_idx:
                self._update_slot_button(slot_idx)
            # Clear progress bar only if from current tab
            if slot_idx in self.slot_progress and tab_idx == self.current_tab_idx:
                self.slot_progress[slot_idx].set(0)

        # Schedule next frame (20fps is enough for progress bars)
        self.root.after(50, self._animate_progress)

    def _refresh_slot_buttons(self):
        """Refresh all slot buttons for current tab."""
        # Check if we need to recreate the grid (more/fewer slots needed)
        tab = self._get_current_tab()
        max_idx = max(tab.slots.keys()) if tab.slots else -1
        needed_slots = max(max_idx + 2, UI["total_slots"])

        if len(self.slot_frames) != needed_slots:
            # Need to recreate slots
            self._create_slot_widgets()

        # Update all slot buttons
        for i in self.slot_buttons.keys():
            self._update_slot_button(i)

    def _create_status_bar(self, parent):
        """Create the status bar at the bottom."""
        status_frame = ctk.CTkFrame(
            parent, fg_color=COLORS["bg_dark"], corner_radius=UI["button_corner_radius"], height=28
        )
        status_frame.pack(fill=tk.X, pady=(8, 0))
        status_frame.pack_propagate(False)

        self.status_var = tk.StringVar(value="Ready - Select devices and click Start")
        self.status_label = ctk.CTkLabel(
            status_frame,
            textvariable=self.status_var,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            anchor="w",
        )
        self.status_label.pack(fill=tk.X, padx=10, pady=4)

    def _toggle_stream(self):
        """Start or stop the audio stream."""
        if self.mixer and self.mixer.running:
            self.mixer.stop()
            self.toggle_btn.configure(
                text="▶ Start Stream", fg_color=COLORS["green"], hover_color=COLORS["green_hover"]
            )
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
                # Save device selection
                self._save_config()
                self.toggle_btn.configure(
                    text="⏹ Stop Stream", fg_color=COLORS["red"], hover_color=COLORS["red_hover"]
                )
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

        # Clear playing state and hide stop buttons
        for slot_idx in list(self.playing_slots.keys()):
            tab_idx = self.playing_slots[slot_idx].get("tab_idx")
            del self.playing_slots[slot_idx]
            if tab_idx == self.current_tab_idx:
                self._update_slot_button(slot_idx)
                if slot_idx in self.slot_progress:
                    self.slot_progress[slot_idx].set(0)
                if slot_idx in self.slot_stop_buttons:
                    self.slot_stop_buttons[slot_idx].pack_forget()

        self.status_var.set("Stopped all sounds")

    def _stop_slot(self, slot_idx: int):
        """Stop the sound playing in a specific slot."""
        # Get the tab this slot belongs to for sound_id
        if slot_idx in self.playing_slots:
            tab_idx = self.playing_slots[slot_idx].get("tab_idx", self.current_tab_idx)
        else:
            tab_idx = self.current_tab_idx

        sound_id = f"{tab_idx}_{slot_idx}"

        # Stop only this specific sound in the mixer
        if self.mixer:
            self.mixer.stop_sound(sound_id)

        # Clear this slot's playing state
        if slot_idx in self.playing_slots:
            del self.playing_slots[slot_idx]

        # Update UI for this slot
        if tab_idx == self.current_tab_idx:
            self._update_slot_button(slot_idx)
            if slot_idx in self.slot_progress:
                self.slot_progress[slot_idx].set(0)
            if slot_idx in self.slot_stop_buttons:
                self.slot_stop_buttons[slot_idx].pack_forget()

        self.status_var.set("Stopped")

    def _stop_slot_with_flag(self, slot_idx: int):
        """Stop a slot and record the time so _handle_slot_click ignores re-plays."""
        self._just_stopped_slot = slot_idx
        self._just_stopped_at = time.time()
        self._stop_slot(slot_idx)
        # If a press is active on this slot, cancel it
        if self._click_active and self._click_slot == slot_idx:
            self._reset_click_state()

    def _record_ptt_key(self):
        """Record a key or mouse button press to use as PTT key."""
        if not HOTKEYS_AVAILABLE:
            messagebox.showwarning(
                "Keyboard Module Required",
                "The keyboard module is required for PTT functionality.\n"
                "Install it with: pip install keyboard",
            )
            return

        self.ptt_record_btn.configure(text="Press key...", fg_color=COLORS["red"])
        self.ptt_status_label.configure(
            text="Press key or mouse button (5s)...", text_color=COLORS["blurple"]
        )
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
                    import mouse  # type: ignore[import-untyped]

                    mouse.unhook(self._ptt_mouse_hook)
                except Exception:
                    pass

        def cancel_recording():
            """Cancel recording after timeout."""
            if self._ptt_recording:
                self._ptt_recording = False
                cleanup_hooks()
                self.ptt_record_btn.configure(text="⏺ Record Key", fg_color=COLORS["blurple"])
                self.ptt_status_label.configure(
                    text="Recording timed out", text_color=COLORS["red"]
                )

        def set_ptt_key(key_name: str):
            """Set the PTT key and update UI."""
            if not self._ptt_recording:
                return

            # Cancel the timeout
            if self._ptt_timeout_id:
                try:
                    self.root.after_cancel(self._ptt_timeout_id)
                except RuntimeError:
                    pass

            # Schedule UI updates on main thread (Tkinter is not thread-safe)
            def update_ui():
                self._ptt_recording = False
                cleanup_hooks()
                self.ptt_key_var.set(key_name)
                self.ptt_record_btn.configure(text="⏺ Record Key", fg_color=COLORS["blurple"])
                self.ptt_status_label.configure(text=f"PTT: {key_name}", text_color=COLORS["green"])

                # Update mixer if running
                if self.mixer:
                    self.mixer.set_ptt_key(key_name)

                # Save config
                self._save_config()

            try:
                self.root.after(0, update_ui)
            except RuntimeError:
                pass

        def on_key(event):
            set_ptt_key(event.name)
            return False  # Stop propagation

        def on_mouse_event(event):
            """Handle mouse button events using mouse library."""
            # Check if it's a button event (has event_type and button attributes)
            event_type = getattr(event, "event_type", None)
            button = getattr(event, "button", None)

            if event_type == "down" and button:
                # Map mouse button names: x = mouse5, x2 = mouse4 (to match Discord)
                if button == "x":
                    button_name = "mouse5"
                elif button == "x2":
                    button_name = "mouse4"
                elif button == "left":
                    button_name = "mouse1"
                elif button == "right":
                    button_name = "mouse2"
                elif button == "middle":
                    button_name = "mouse3"
                else:
                    button_name = f"mouse_{button}"
                set_ptt_key(button_name)

        # Hook keyboard (no suppress to avoid blocking keyboard)
        self._ptt_hook = keyboard.on_press(on_key)

        # Try to hook mouse buttons using mouse library
        try:
            import mouse  # type: ignore[import-untyped]

            self._ptt_mouse_hook = mouse.hook(on_mouse_event)
        except ImportError:
            pass  # mouse module not available
        except Exception:
            pass

        # Set a 5 second timeout
        self._ptt_timeout_id = self.root.after(5000, cancel_recording)

    def _clear_ptt_key(self):
        """Clear the PTT key setting."""
        self.ptt_key_var.set("")
        self.ptt_status_label.configure(text="PTT disabled", text_color=COLORS["text_muted"])

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

    def _show_stop_button(self, slot_idx: int):
        """Show the stop button for a playing slot.

        command= was already set at creation time — just pack/unpack.
        """
        if slot_idx not in self.slot_stop_buttons or slot_idx not in self.slot_progress:
            return
        stop_btn = self.slot_stop_buttons[slot_idx]
        progress = self.slot_progress[slot_idx]
        stop_btn.pack_forget()
        stop_btn.pack(side=tk.LEFT, padx=(0, 4), before=progress)

    def _is_widget_inside(self, widget, parent) -> bool:
        """Check if *widget* is *parent* or a descendant of *parent*."""
        if widget is None or parent is None:
            return False
        current = widget
        while current is not None:
            if current == parent:
                return True
            try:
                current = current.master
            except Exception:
                break
        return False

    # ─────────────────────────────────────────────────────────
    # Slot click / drag state machine
    #
    # Click-to-play: via command= callback (survives CTkButton._draw())
    # Drag: via drag handle at top of slot (hold 2 seconds to enable)
    #
    # The drag handle approach separates click-to-play from drag,
    # making it clear that holding the grip icon enables reordering.
    # ─────────────────────────────────────────────────────────

    _DRAG_THRESHOLD = 5  # pixels before actual drag movement starts

    def _reset_click_state(self):
        """Return to IDLE — cancel any press/drag in progress."""
        self._click_active = False
        self._click_slot = None
        self._click_tab = None
        self._click_start_x = 0
        self._click_start_y = 0
        self._click_dragging = False

    # ---------- Edit Mode (phone-like rearrange with wiggle animation) ----------

    def _toggle_edit_mode(self):
        """Toggle edit/rearrange mode - slots wiggle and can be dragged."""
        if self._edit_mode:
            self._exit_edit_mode()
        else:
            self._enter_edit_mode()

    def _enter_edit_mode(self):
        """Enter edit mode - show edit indicators on slots."""
        self._edit_mode = True
        self._dragging_slot = None
        self._edit_mode_frames: set = set()  # Track which frames have edit indicator

        # Update edit mode button
        if hasattr(self, 'edit_mode_btn'):
            self.edit_mode_btn.configure(
                text="✓ Done",
                fg_color=COLORS["green"],
            )

        # Change cursor
        self.root.configure(cursor="fleur")

        # Show edit mode indicator on all filled slots
        tab = self._get_current_tab()
        for slot_idx, frame in self.slot_frames.items():
            if slot_idx in tab.slots:
                frame.configure(
                    fg_color=COLORS["blurple"],
                    border_width=2,
                    border_color=COLORS["text_muted"],
                )
                self._edit_mode_frames.add(slot_idx)

    def _exit_edit_mode(self, skip_refresh: bool = False):
        """Exit edit mode - reset visuals."""
        self._edit_mode = False
        self._dragging_slot = None

        # Update edit mode button
        if hasattr(self, 'edit_mode_btn'):
            self.edit_mode_btn.configure(
                text="↔ Move",
                fg_color=COLORS["bg_light"],
            )

        # Reset cursor
        self.root.configure(cursor="")

        # Reset only frames that had edit indicator (optimization)
        frames_to_reset = getattr(self, '_edit_mode_frames', set())
        for slot_idx in frames_to_reset:
            if slot_idx in self.slot_frames:
                self.slot_frames[slot_idx].configure(
                    fg_color=COLORS["bg_medium"],
                    border_width=0,
                )
        self._edit_mode_frames = set()

        # Reset slot appearances (skip if caller will refresh anyway)
        if not skip_refresh:
            self._refresh_slot_buttons()

    # ---------- command= callback (PRIMARY click mechanism) ----------

    def _on_slot_command(self, slot_idx: int):
        """CTkButton command= callback — fires on press, survives _draw().

        This is the ONLY reliable click callback for CTkButton because
        _draw() re-binds <Button-1> on the internal canvas, wiping any
        raw canvas.bind() handlers.  command= is stored as a property
        and is immune to _draw().
        """
        # In edit mode, clicking a slot selects it for dragging
        if self._edit_mode:
            if self._dragging_slot is None:
                # Select this slot for dragging
                tab = self._get_current_tab()
                if slot_idx in tab.slots:
                    self._dragging_slot = slot_idx
                    self._click_tab = self.current_tab_idx
                    # Highlight the selected slot
                    self.slot_buttons[slot_idx].configure(fg_color=COLORS["green"])
            elif self._dragging_slot == slot_idx:
                # Clicking same slot - deselect
                self._dragging_slot = None
                self._refresh_slot_buttons()
            else:
                # Clicking different slot - swap them
                source_idx = self._dragging_slot
                self._dragging_slot = None
                self._swap_slots(source_idx, slot_idx)
            return

        self._handle_slot_click(slot_idx)

    # ---------- high-level action handlers ----------

    def _handle_slot_click(self, slot_idx: int):
        """A confirmed click on a slot — play or configure."""
        now = time.time()

        if now - self._last_play_time < 0.15:
            return

        if (self._just_stopped_slot == slot_idx
                and now - self._just_stopped_at < 0.2):
            self._just_stopped_slot = None
            return
        self._just_stopped_slot = None

        self._last_play_time = now
        self._play_slot(slot_idx)

    def _handle_slot_drop(self, event, source_idx: int, source_tab: int):
        """A confirmed drag-drop — swap slots or move across tabs."""
        widget = self.root.winfo_containing(event.x_root, event.y_root)

        for idx, tab_btn in enumerate(self.tab_buttons):
            if self._is_widget_inside(widget, tab_btn):
                if idx != source_tab:
                    self._move_slot_to_tab(source_idx, source_tab, idx)
                return

        for slot_idx, btn in self.slot_buttons.items():
            if self._is_widget_inside(widget, btn):
                if slot_idx != source_idx and source_tab == self.current_tab_idx:
                    self._swap_slots(source_idx, slot_idx)
                return

    def _swap_slots(self, idx1: int, idx2: int):
        """Swap two slots within the current tab."""
        tab = self._get_current_tab()
        slot1 = tab.slots.get(idx1)
        slot2 = tab.slots.get(idx2)

        if slot1 is not None and slot2 is not None:
            # Both have content - swap
            tab.slots[idx1] = slot2
            tab.slots[idx2] = slot1
        elif slot1 is not None:
            # Only slot1 has content - move to slot2
            tab.slots[idx2] = slot1
            del tab.slots[idx1]
        # else: slot1 is empty, nothing to move

        self._save_config()
        self._refresh_slot_buttons()

    def _move_slot_to_tab(self, slot_idx: int, from_tab_idx: int, to_tab_idx: int):
        """Move a slot from one tab to another."""
        if from_tab_idx >= len(self.tabs) or to_tab_idx >= len(self.tabs):
            return

        from_tab = self.tabs[from_tab_idx]
        to_tab = self.tabs[to_tab_idx]

        if slot_idx not in from_tab.slots:
            return

        slot = from_tab.slots[slot_idx]

        # Find first empty slot in target tab
        target_idx = 0
        while target_idx in to_tab.slots:
            target_idx += 1

        # Move the slot
        to_tab.slots[target_idx] = slot
        del from_tab.slots[slot_idx]

        self._save_config()
        self._refresh_slot_buttons()
        self.status_var.set(f"Moved '{slot.name}' to {to_tab.emoji or ''} {to_tab.name}")

    def _play_slot(self, slot_idx: int):
        """Play the sound assigned to a slot, or open config for empty slots."""
        tab = self._get_current_tab()
        if slot_idx not in tab.slots:
            # Empty slot - open configuration to add a sound
            self._configure_slot(slot_idx)
            return

        slot = tab.slots[slot_idx]

        if self.mixer and self.mixer.running:
            # Create unique sound_id combining tab and slot
            sound_id = f"{self.current_tab_idx}_{slot_idx}"
            # play_sound returns the duration, avoiding a second cache lookup
            duration = self.mixer.play_sound(
                slot.file_path, slot.volume, slot.speed, slot.preserve_pitch, sound_id
            )
            self.status_var.set(f"Playing: {slot.name}")

            # Start progress tracking
            if duration > 0:
                self.playing_slots[slot_idx] = {
                    "start_time": time.time(),
                    "duration": duration,
                    "tab_idx": self.current_tab_idx,
                }
                # Change button color to playing state
                if slot_idx in self.slot_buttons:
                    self.slot_buttons[slot_idx].configure(fg_color=COLORS["playing"])
                # Set progress bar color for playing state
                if slot_idx in self.slot_progress:
                    self.slot_progress[slot_idx].configure(progress_color=COLORS["playing"])
                # Show stop button (on left side, before progress bar)
                self._show_stop_button(slot_idx)
        else:
            self.status_var.set("Start the audio stream first!")

    def _preview_slot(self, slot_idx: int):
        """Preview a sound through default speakers (without streaming to Discord)."""
        tab = self._get_current_tab()
        if slot_idx not in tab.slots:
            self.status_var.set("No sound in this slot")
            return

        slot = tab.slots[slot_idx]

        # Get cached audio data
        data = self.sound_cache.get_sound_data(slot.file_path)
        if data is None:
            self.status_var.set("Failed to load sound for preview")
            return

        try:
            # Stop any currently playing preview
            sd.stop()

            # Calculate duration
            duration = len(data) / self.sound_cache.sample_rate

            # Play through default speakers (not the virtual cable)
            sd.play(data * slot.volume, samplerate=self.sound_cache.sample_rate, device=None)
            self.status_var.set(f"Preview: {slot.name}")

            # Track preview progress
            if duration > 0:
                self.preview_slots[slot_idx] = {
                    "start_time": time.time(),
                    "duration": duration,
                    "tab_idx": self.current_tab_idx,
                }
                # Change button color to preview state (green)
                if slot_idx in self.slot_buttons:
                    self.slot_buttons[slot_idx].configure(fg_color=COLORS["preview"])
                # Set progress bar color for preview state
                if slot_idx in self.slot_progress:
                    self.slot_progress[slot_idx].configure(progress_color=COLORS["preview"])
        except Exception as e:
            self.status_var.set(f"Preview error: {e}")

    def _show_quick_popup(self, event, slot_idx: int):
        """Show a quick popup for volume/speed adjustment next to the clicked slot."""
        tab = self._get_current_tab()

        # If slot is empty, open the full configure dialog instead
        if slot_idx not in tab.slots:
            self._configure_slot(slot_idx)
            return

        slot = tab.slots[slot_idx]

        # Create popup window positioned near the click
        popup = ctk.CTkToplevel(self.root)
        popup.title("Quick Edit")
        popup.overrideredirect(True)  # Remove window decorations
        popup.attributes("-topmost", True)

        # Position popup near the click location
        x = event.x_root + 10
        y = event.y_root + 10
        popup.geometry(f"280x220+{x}+{y}")

        # Main frame with rounded corners
        main_frame = ctk.CTkFrame(popup, fg_color=COLORS["bg_medium"], corner_radius=12)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # Header with slot name
        header = ctk.CTkLabel(
            main_frame,
            text=slot.name[:20] + "…" if len(slot.name) > 20 else slot.name,
            text_color=COLORS["text_primary"],
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        )
        header.pack(fill=tk.X, padx=12, pady=(12, 8))

        # Volume control
        vol_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        vol_frame.pack(fill=tk.X, padx=12, pady=4)

        ctk.CTkLabel(
            vol_frame,
            text="🔊 Volume:",
            text_color=COLORS["text_primary"],
            width=80,
            anchor="w",
        ).pack(side=tk.LEFT)

        volume_var = tk.IntVar(value=int(slot.volume * 100))
        volume_slider = ctk.CTkSlider(
            vol_frame,
            from_=0,
            to=150,
            variable=volume_var,
            width=120,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
            button_hover_color=COLORS["blurple_hover"],
        )
        volume_slider.pack(side=tk.LEFT, padx=5)

        ctk.CTkButton(
            vol_frame,
            text="↺",
            command=lambda: volume_var.set(100),
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=28,
            height=28,
        ).pack(side=tk.RIGHT)

        # Speed control
        speed_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        speed_frame.pack(fill=tk.X, padx=12, pady=4)

        ctk.CTkLabel(
            speed_frame,
            text="⚡ Speed:",
            text_color=COLORS["text_primary"],
            width=80,
            anchor="w",
        ).pack(side=tk.LEFT)

        speed_var = tk.IntVar(value=int(slot.speed * 100))
        speed_slider = ctk.CTkSlider(
            speed_frame,
            from_=50,
            to=200,
            variable=speed_var,
            width=120,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
            button_hover_color=COLORS["blurple_hover"],
        )
        speed_slider.pack(side=tk.LEFT, padx=5)

        ctk.CTkButton(
            speed_frame,
            text="↺",
            command=lambda: speed_var.set(100),
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=28,
            height=28,
        ).pack(side=tk.RIGHT)

        # Preserve pitch checkbox
        pitch_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        pitch_frame.pack(fill=tk.X, padx=12, pady=4)

        preserve_pitch_var = tk.BooleanVar(value=slot.preserve_pitch)
        pitch_check = ctk.CTkCheckBox(
            pitch_frame,
            text="🎵 Preserve pitch",
            variable=preserve_pitch_var,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            text_color=COLORS["text_primary"],
        )
        pitch_check.pack(side=tk.LEFT)

        # Button frame
        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(fill=tk.X, padx=12, pady=(8, 12))

        def apply_changes():
            """Apply the volume/speed/pitch changes."""
            slot.volume = volume_var.get() / 100.0
            slot.speed = speed_var.get() / 100.0
            slot.preserve_pitch = preserve_pitch_var.get()
            self._save_config()
            popup.destroy()

        def open_full_edit():
            """Open the full configure dialog."""
            popup.destroy()
            self._configure_slot(slot_idx)

        ctk.CTkButton(
            btn_frame,
            text="Apply",
            command=apply_changes,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=70,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            btn_frame,
            text="More...",
            command=open_full_edit,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=70,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            btn_frame,
            text="✕",
            command=popup.destroy,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=32,
        ).pack(side=tk.RIGHT, padx=2)

        # Close popup when clicking outside
        def on_focus_out(event):
            try:
                if popup.winfo_exists():
                    popup.destroy()
            except tk.TclError:
                pass

        popup.bind("<FocusOut>", on_focus_out)
        popup.focus_set()

    def _configure_slot(self, slot_idx: int):
        """Open configuration dialog for a slot."""
        tab = self._get_current_tab()

        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Configure Slot {slot_idx + 1}")
        dialog.geometry("550x480")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.after(10, lambda: dialog.focus_force())

        frame = ctk.CTkFrame(dialog, fg_color=COLORS["bg_dark"], corner_radius=0)
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        existing = tab.slots.get(slot_idx)

        # State for edited audio
        edited_audio_data = {"data": None, "sample_rate": None, "original_name": None}

        # Name field
        ctk.CTkLabel(frame, text="Name:", text_color=COLORS["text_primary"]).grid(
            row=0, column=0, sticky="w", pady=8
        )
        name_var = tk.StringVar(value=existing.name if existing else "")
        ctk.CTkEntry(
            frame,
            textvariable=name_var,
            width=250,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=0, column=1, pady=8)

        # File path field
        ctk.CTkLabel(frame, text="Sound File:", text_color=COLORS["text_primary"]).grid(
            row=1, column=0, sticky="w", pady=8
        )
        path_var = tk.StringVar(value=existing.file_path if existing else "")
        ctk.CTkEntry(
            frame,
            textvariable=path_var,
            width=250,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=1, column=1, pady=8)

        # Edit status label
        edit_status_var = tk.StringVar(value="")
        edit_status_label = ctk.CTkLabel(
            frame,
            textvariable=edit_status_var,
            text_color=COLORS["green"],
            font=ctk.CTkFont(size=11),
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

        btn_frame_browse = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame_browse.grid(row=1, column=2, padx=10)

        ctk.CTkButton(
            btn_frame_browse,
            text="Browse",
            command=browse,
            width=70,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
        ).pack(pady=2)
        ctk.CTkButton(
            btn_frame_browse,
            text="Edit",
            command=edit_current,
            width=70,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
        ).pack(pady=2)

        # Emoji field
        ctk.CTkLabel(frame, text="Emoji:", text_color=COLORS["text_primary"]).grid(
            row=3, column=0, sticky="w", pady=8
        )
        emoji_var = tk.StringVar(value=existing.emoji if existing and existing.emoji else "")
        ctk.CTkEntry(
            frame,
            textvariable=emoji_var,
            width=80,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=3, column=1, sticky="w", pady=8)

        def pick_emoji():
            self._show_emoji_picker(emoji_var, dialog)

        ctk.CTkButton(
            frame,
            text="Choose Emoji",
            command=pick_emoji,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=100,
        ).grid(row=3, column=2, padx=10, sticky="w")

        # Image field
        ctk.CTkLabel(frame, text="Image:", text_color=COLORS["text_primary"]).grid(
            row=4, column=0, sticky="w", pady=8
        )
        image_var = tk.StringVar(
            value=existing.image_path if existing and existing.image_path else ""
        )
        ctk.CTkEntry(
            frame,
            textvariable=image_var,
            width=250,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=4, column=1, pady=8)

        def browse_image():
            filetypes = [("Images", " ".join(SUPPORTED_IMAGE_FORMATS))]
            fp = filedialog.askopenfilename(filetypes=filetypes)
            if fp:
                # Copy image to local storage
                local_path = self._copy_image_to_storage(fp)
                image_var.set(local_path)

        ctk.CTkButton(
            frame,
            text="Browse",
            command=browse_image,
            width=70,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
        ).grid(row=4, column=2, padx=10)

        # Volume slider
        ctk.CTkLabel(frame, text="Volume:", text_color=COLORS["text_primary"]).grid(
            row=5, column=0, sticky="w", pady=8
        )
        volume_var = tk.DoubleVar(value=(existing.volume * 100) if existing else 100)
        ctk.CTkSlider(
            frame,
            from_=0,
            to=150,
            variable=volume_var,
            width=200,
            fg_color=COLORS["bg_medium"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
        ).grid(row=5, column=1, sticky="w")

        # Hotkey field
        ctk.CTkLabel(frame, text="Hotkey:", text_color=COLORS["text_primary"]).grid(
            row=6, column=0, sticky="w", pady=8
        )
        hotkey_var = tk.StringVar(value=existing.hotkey if existing and existing.hotkey else "")
        ctk.CTkEntry(
            frame,
            textvariable=hotkey_var,
            width=150,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=6, column=1, sticky="w")

        # Speed slider
        ctk.CTkLabel(frame, text="Speed:", text_color=COLORS["text_primary"]).grid(
            row=7, column=0, sticky="w", pady=8
        )
        speed_var = tk.DoubleVar(value=(existing.speed * 100) if existing else 100)
        speed_frame = ctk.CTkFrame(frame, fg_color="transparent")
        speed_frame.grid(row=7, column=1, sticky="w")
        ctk.CTkSlider(
            speed_frame,
            from_=50,
            to=200,
            variable=speed_var,
            width=150,
            fg_color=COLORS["bg_medium"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
        ).pack(side=tk.LEFT)
        speed_label = ctk.CTkLabel(
            speed_frame, text="100%", width=50, text_color=COLORS["text_primary"]
        )
        speed_label.pack(side=tk.LEFT, padx=5)

        def update_speed_label(*args):
            speed_label.configure(text=f"{int(speed_var.get())}%")

        speed_var.trace("w", update_speed_label)
        update_speed_label()

        # Color dropdown
        ctk.CTkLabel(frame, text="Color:", text_color=COLORS["text_primary"]).grid(
            row=8, column=0, sticky="w", pady=8
        )
        color_names = list(ALL_SLOT_COLORS.keys())

        # Find existing color name
        existing_color_name = "Default"
        if existing and existing.color:
            for name, hex_val in ALL_SLOT_COLORS.items():
                if hex_val.lower() == existing.color.lower():
                    existing_color_name = name
                    break

        color_var = tk.StringVar(value=existing_color_name)
        color_dropdown = ctk.CTkComboBox(
            frame,
            variable=color_var,
            values=color_names,
            width=150,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
            dropdown_fg_color=COLORS["bg_medium"],
            dropdown_hover_color=COLORS["bg_light"],
            state="readonly",
        )
        color_dropdown.grid(row=8, column=1, sticky="w")

        # Color preview (using a small CTkFrame as color swatch)
        color_preview = ctk.CTkFrame(
            frame,
            width=30,
            height=20,
            fg_color=ALL_SLOT_COLORS[existing_color_name],
            corner_radius=4,
        )
        color_preview.grid(row=8, column=2, padx=10, sticky="w")

        def update_color_preview(*args):
            selected = color_var.get()
            if selected in ALL_SLOT_COLORS:
                color_preview.configure(fg_color=ALL_SLOT_COLORS[selected])

        color_var.trace("w", update_color_preview)

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
                else:
                    # Check if already in sounds folder (handle both relative and absolute paths)
                    source_abs = str(Path(source_path).absolute())
                    sounds_abs = str(Path(SOUNDS_DIR).absolute())
                    is_already_local = (
                        source_path.startswith(SOUNDS_DIR + "/")
                        or source_path.startswith(SOUNDS_DIR + "\\")
                        or source_abs.startswith(sounds_abs)
                    )
                    if not is_already_local:
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
                color=ALL_SLOT_COLORS.get(color_var.get()),
                speed=speed_var.get() / 100.0,
                preserve_pitch=existing.preserve_pitch if existing else True,
            )
            self._refresh_slot_buttons()  # Refresh to create new empty slots if needed
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
            self._refresh_slot_buttons()  # Refresh to adjust slot count
            self._register_hotkeys()
            self._save_config()
            dialog.destroy()

        # Button row
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.grid(row=9, column=0, columnspan=3, pady=25)

        ctk.CTkButton(
            btn_frame,
            text="Save",
            command=save,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Clear",
            command=clear,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)

    def _copy_image_to_storage(self, source_path: str) -> str:
        """Copy an image to local storage and return the local path."""
        Path(IMAGES_DIR).mkdir(exist_ok=True)

        # Generate unique filename using hash
        with open(source_path, "rb") as f:
            file_hash = hashlib.md5(f.read(4096)).hexdigest()[:8]

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
        """Update the appearance of a slot button.
        
        Optimization: Preview/edit buttons only change appearance based on whether
        the slot is filled or empty. We cache this state and skip their configure()
        calls when the filled state hasn't changed, reducing redundant redraws.
        """
        btn = self.slot_buttons[slot_idx]
        slot_frame = self.slot_frames[slot_idx]
        tab = self._get_current_tab()

        # Determine background color (playing/preview state takes precedence, but only for current tab)
        is_playing = (
            slot_idx in self.playing_slots
            and self.playing_slots[slot_idx].get("tab_idx") == self.current_tab_idx
        )
        is_previewing = (
            slot_idx in self.preview_slots
            and self.preview_slots[slot_idx].get("tab_idx") == self.current_tab_idx
        )

        # Get custom slot color or default
        slot = tab.slots.get(slot_idx)
        default_color = slot.color if slot and slot.color else COLORS["blurple"]

        if is_playing:
            bg_color = COLORS["playing"]
            frame_color = COLORS["bg_light"]
        elif is_previewing:
            bg_color = COLORS["preview"]
            frame_color = COLORS["bg_light"]
        else:
            bg_color = default_color if slot_idx in tab.slots else "transparent"
            frame_color = COLORS["bg_medium"]

        # Update frame color
        slot_frame.configure(fg_color=frame_color)

        # Check if filled state changed (for preview/edit button optimization)
        is_filled = slot_idx in tab.slots
        was_filled = self._slot_filled_cache.get(slot_idx)
        filled_state_changed = was_filled != is_filled
        self._slot_filled_cache[slot_idx] = is_filled

        if is_filled:
            slot = tab.slots[slot_idx]
            hk = f"\n[{slot.hotkey}]" if slot.hotkey else ""

            # Truncate name if too long (max ~18 chars per line, 2 lines)
            max_name_len = 32
            display_name = (
                slot.name[:max_name_len] + "…" if len(slot.name) > max_name_len else slot.name
            )

            # Build display text with emoji
            emoji_prefix = f"{slot.emoji} " if slot.emoji else ""
            display_text = f"{emoji_prefix}{display_name}{hk}"

            # Use cached image if available and path hasn't changed
            photo = None
            image_path = slot.image_path if slot.image_path and os.path.exists(slot.image_path) else None
            if image_path:
                # Check if we already have this image cached for this slot
                if (
                    slot_idx in self.slot_images
                    and self.slot_image_paths.get(slot_idx) == image_path
                ):
                    photo = self.slot_images[slot_idx]
                else:
                    # Load and cache the image
                    photo = self._load_slot_image(image_path)
                    if photo:
                        self.slot_images[slot_idx] = photo
                        self.slot_image_paths[slot_idx] = image_path

            hover_color = COLORS["bg_lighter"] if not is_playing and not is_previewing else bg_color
            
            btn.configure(
                text=display_text,
                image=photo,
                fg_color=bg_color,
                hover_color=hover_color,
                text_color=COLORS["text_primary"],
                font=self._font_sm,
            )

            # Only update preview/edit buttons if filled state changed (optimization)
            if filled_state_changed:
                if slot_idx in self.slot_preview_buttons:
                    self.slot_preview_buttons[slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_primary"],
                    )
                if slot_idx in self.slot_edit_buttons:
                    self.slot_edit_buttons[slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_primary"],
                    )
        else:
            # Clear image reference if exists
            if slot_idx in self.slot_images:
                del self.slot_images[slot_idx]
            if slot_idx in self.slot_image_paths:
                del self.slot_image_paths[slot_idx]

            btn.configure(
                text="+",
                image=None,
                fg_color="transparent",
                hover_color=COLORS["bg_light"],
                text_color=COLORS["text_muted"],
                font=self._font_xl_bold,
            )

            # Only update preview/edit buttons if filled state changed (optimization)
            if filled_state_changed:
                if slot_idx in self.slot_preview_buttons:
                    self.slot_preview_buttons[slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_muted"],
                    )
                if slot_idx in self.slot_edit_buttons:
                    self.slot_edit_buttons[slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_muted"],
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
            # Create unique sound_id combining tab and slot
            sound_id = f"{tab_idx}_{slot_idx}"
            # play_sound returns the duration, avoiding a second cache lookup
            duration = self.mixer.play_sound(
                slot.file_path, slot.volume, slot.speed, slot.preserve_pitch, sound_id
            )

            # Always track playing state so progress shows when switching tabs
            if duration > 0:
                self.playing_slots[slot_idx] = {
                    "start_time": time.time(),
                    "duration": duration,
                    "tab_idx": tab_idx,
                }

                def update_ui():
                    self.status_var.set(f"Playing: {slot.name}")
                    if tab_idx == self.current_tab_idx:
                        if slot_idx in self.slot_buttons:
                            self.slot_buttons[slot_idx].configure(fg_color=COLORS["playing"])
                        self._show_stop_button(slot_idx)

                try:
                    self.root.after(0, update_ui)
                except RuntimeError:
                    pass
            else:
                try:
                    self.root.after(0, lambda: self.status_var.set(f"Playing: {slot.name}"))
                except RuntimeError:
                    pass

    def _save_config(self):
        """Save configuration to JSON file using atomic write to prevent corruption."""
        config = {
            "tabs": [t.to_dict() for t in self.tabs],
            "current_tab": self.current_tab_idx,
            "ptt_enabled": self.ptt_enabled_var.get(),
            "ptt_key": self.ptt_key_var.get().strip() if self.ptt_key_var.get().strip() else None,
            "input_device": self.input_var.get() if self.input_var.get() else None,
            "output_device": self.output_var.get() if self.output_var.get() else None,
            "auto_start": self.auto_start_var.get() if hasattr(self, "auto_start_var") else True,
            "monitor_enabled": self.monitor_var.get() if hasattr(self, "monitor_var") else True,
        }

        # Atomic write: write to temp file first, then rename
        temp_file = CONFIG_FILE + ".tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            os.replace(temp_file, CONFIG_FILE)
        except Exception as e:
            print(f"Error saving config: {e}")
            # Clean up temp file if it exists
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

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
                # Always start on first tab (index 0) for consistent behavior
                self.current_tab_idx = 0
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
                self.ptt_status_label.configure(text=f"PTT: {ptt_key}", text_color=COLORS["green"])

            if ptt_enabled:
                self.ptt_enabled_var.set(True)
                self.ptt_frame.pack(fill=tk.X, pady=(8, 0))  # Show PTT settings

            # Load saved device selections
            saved_input = config.get("input_device")
            saved_output = config.get("output_device")

            if saved_input:
                # Check if the saved device is still in the available devices list
                current_values = self.input_combo.cget("values")
                if saved_input in current_values:
                    self.input_combo.set(saved_input)

            if saved_output:
                current_values = self.output_combo.cget("values")
                if saved_output in current_values:
                    self.output_combo.set(saved_output)

            # Load auto-start and monitor settings (default to True for new users)
            auto_start = config.get("auto_start", True)
            monitor_enabled = config.get("monitor_enabled", True)

            self.auto_start_var.set(auto_start)
            self.monitor_var.set(monitor_enabled)

            self._refresh_tab_bar()
            self._refresh_slot_buttons()
            self._register_hotkeys()

            # Auto-start the stream if enabled and devices are selected
            if auto_start and self.input_var.get() and self.output_var.get():
                self.root.after(100, self._auto_start_stream)

        except Exception as e:
            print(f"Error loading config: {e}")
            # Create default tab on error
            self.tabs = [SoundTab(name="Main", emoji="🎵")]
            self._refresh_tab_bar()

    def _auto_start_stream(self):
        """Auto-start the audio stream after config load."""
        if not self.mixer or not self.mixer.running:
            self._toggle_stream()
            # Apply monitor setting after stream starts
            if hasattr(self, "monitor_var") and self.mixer:
                self.mixer.set_monitor_enabled(self.monitor_var.get())

    def _preload_sounds(self):
        """Preload all configured sounds into memory cache in a background thread."""
        sound_paths = []
        for tab in self.tabs:
            for slot in tab.slots.values():
                if slot.file_path:
                    sound_paths.append(slot.file_path)

        if sound_paths:
            self.status_var.set(f"Loading {len(sound_paths)} sounds...")

            def _do_preload():
                self.sound_cache.preload_sounds(sound_paths)
                try:
                    self.root.after(
                        0, lambda: self.status_var.set(f"Ready - {len(sound_paths)} sounds cached")
                    )
                except RuntimeError:
                    pass  # Main loop not running (app closing or not started yet)

            threading.Thread(target=_do_preload, daemon=True).start()
        else:
            self.status_var.set("Ready")

    def _on_close(self):
        """Handle application close."""
        if self.mixer:
            self.mixer.stop()
        self.root.destroy()

    def run(self):
        """Start the application main loop."""
        self.root.mainloop()
