"""
GUI components for the Discord Soundboard.
"""

import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, Optional

import sounddevice as sd

from .audio import AudioMixer, SoundCache
from .constants import COLORS, CONFIG_FILE, SOUNDS_DIR, SUPPORTED_FORMATS, UI
from .editor import SoundEditor
from .models import SoundSlot

# Try to import keyboard for global hotkeys
try:
    import keyboard

    HOTKEYS_AVAILABLE = True
except ImportError:
    HOTKEYS_AVAILABLE = False


class SoundboardApp:
    """Main GUI application for the soundboard."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(UI["window_title"])
        self.root.geometry(UI["window_size"])
        self.root.configure(bg=COLORS["bg_dark"])

        self.mixer: Optional[AudioMixer] = None
        self.sound_cache = SoundCache()  # Local sound storage with caching
        self.sound_slots: Dict[int, SoundSlot] = {}
        self.slot_buttons: Dict[int, tk.Button] = {}
        self.registered_hotkeys: list = []

        self._setup_styles()
        self._create_ui()
        self._load_config()
        self._preload_sounds()  # Preload all sounds into memory

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
        self._create_mic_section(main_frame)
        self._create_soundboard_section(main_frame)
        self._create_status_bar(main_frame)

    def _create_device_section(self, parent):
        """Create the audio device selection section."""
        device_frame = ttk.LabelFrame(parent, text="Audio Devices", padding=10)
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
            device_frame, textvariable=self.input_var, width=50, state="readonly"
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
            device_frame, textvariable=self.output_var, width=50, state="readonly"
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

        # Start/Stop button
        self.toggle_btn = tk.Button(
            device_frame,
            text="▶ Start",
            command=self._toggle_stream,
            bg=COLORS["green"],
            fg="white",
            font=("Segoe UI", 10, "bold"),
            width=15,
        )
        self.toggle_btn.grid(row=0, column=2, rowspan=2, padx=20)

    def _create_mic_section(self, parent):
        """Create the microphone control section."""
        mic_frame = ttk.LabelFrame(parent, text="Microphone", padding=10)
        mic_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(mic_frame, text="Mic Volume:").pack(side=tk.LEFT, padx=5)

        self.mic_volume_var = tk.DoubleVar(value=100)
        ttk.Scale(
            mic_frame,
            from_=0,
            to=150,
            variable=self.mic_volume_var,
            command=self._update_mic_volume,
            length=200,
        ).pack(side=tk.LEFT, padx=5)

        self.mic_mute_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            mic_frame,
            text="Mute Mic",
            variable=self.mic_mute_var,
            command=self._toggle_mic_mute,
            bg=COLORS["bg_dark"],
            fg="white",
            selectcolor=COLORS["red"],
        ).pack(side=tk.LEFT, padx=20)

        tk.Button(
            mic_frame,
            text="Stop All Sounds",
            command=self._stop_all_sounds,
            bg=COLORS["red"],
            fg="white",
        ).pack(side=tk.RIGHT, padx=5)

    def _create_soundboard_section(self, parent):
        """Create the soundboard grid section."""
        board_frame = ttk.LabelFrame(
            parent, text="Soundboard (Right-click to configure)", padding=10
        )
        board_frame.pack(fill=tk.BOTH, expand=True)

        self.grid_frame = ttk.Frame(board_frame)
        self.grid_frame.pack(fill=tk.BOTH, expand=True)

        # Create sound slot buttons
        for i in range(UI["total_slots"]):
            row, col = divmod(i, UI["grid_columns"])
            btn = tk.Button(
                self.grid_frame,
                text=f"Slot {i+1}\n(Empty)",
                width=18,
                height=4,
                bg=COLORS["bg_medium"],
                fg=COLORS["text_muted"],
                font=("Segoe UI", 9),
                relief=tk.FLAT,
                command=lambda idx=i: self._play_slot(idx),
            )
            btn.grid(row=row, column=col, padx=5, pady=5, sticky="nsew")
            btn.bind("<Button-3>", lambda e, idx=i: self._configure_slot(idx))
            self.slot_buttons[i] = btn

        # Configure grid weights for resizing
        for i in range(UI["grid_columns"]):
            self.grid_frame.columnconfigure(i, weight=1)
        for i in range(UI["grid_rows"]):
            self.grid_frame.rowconfigure(i, weight=1)

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
                self.mixer.start()
                self.toggle_btn.configure(text="⏹ Stop", bg=COLORS["red"])
                self.status_var.set("Running - Mic → Virtual Cable")
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

    def _stop_all_sounds(self):
        """Stop all currently playing sounds."""
        if self.mixer:
            self.mixer.stop_all_sounds()

    def _play_slot(self, slot_idx: int):
        """Play the sound assigned to a slot."""
        if slot_idx not in self.sound_slots:
            return

        slot = self.sound_slots[slot_idx]
        if self.mixer and self.mixer.running:
            self.mixer.play_sound(slot.file_path, slot.volume)
            self.status_var.set(f"Playing: {slot.name}")
        else:
            self.status_var.set("Start the audio stream first!")

    def _configure_slot(self, slot_idx: int):
        """Open configuration dialog for a slot."""
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Configure Slot {slot_idx + 1}")
        dialog.geometry("450x300")
        dialog.configure(bg=COLORS["bg_dark"])
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        existing = self.sound_slots.get(slot_idx)

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

        # Volume slider
        ttk.Label(frame, text="Volume:").grid(row=3, column=0, sticky="w", pady=5)
        volume_var = tk.DoubleVar(value=(existing.volume * 100) if existing else 100)
        ttk.Scale(frame, from_=0, to=150, variable=volume_var, length=200).grid(
            row=3, column=1, sticky="w"
        )

        # Hotkey field
        ttk.Label(frame, text="Hotkey:").grid(row=4, column=0, sticky="w", pady=5)
        hotkey_var = tk.StringVar(value=existing.hotkey if existing and existing.hotkey else "")
        ttk.Entry(frame, textvariable=hotkey_var, width=20).grid(row=4, column=1, sticky="w")

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

            self.sound_slots[slot_idx] = SoundSlot(
                name=name_var.get() or Path(source_path).stem,
                file_path=local_path,
                hotkey=hotkey_var.get() or None,
                volume=volume_var.get() / 100.0,
            )
            self._update_slot_button(slot_idx)
            self._register_hotkeys()
            self._save_config()
            dialog.destroy()

        def clear():
            if slot_idx in self.sound_slots:
                slot = self.sound_slots[slot_idx]
                # Check if any other slot uses this sound before removing from cache
                other_uses = any(
                    s.file_path == slot.file_path
                    for idx, s in self.sound_slots.items()
                    if idx != slot_idx
                )
                if not other_uses:
                    self.sound_cache.remove_sound(slot.file_path, delete_file=True)
                del self.sound_slots[slot_idx]
            self._update_slot_button(slot_idx)
            self._save_config()
            dialog.destroy()

        # Button row
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=5, column=0, columnspan=3, pady=20)

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

    def _update_slot_button(self, slot_idx: int):
        """Update the appearance of a slot button."""
        btn = self.slot_buttons[slot_idx]
        if slot_idx in self.sound_slots:
            slot = self.sound_slots[slot_idx]
            hk = f"\n[{slot.hotkey}]" if slot.hotkey else ""
            btn.configure(text=f"{slot.name}{hk}", bg=COLORS["blurple"], fg="white")
        else:
            btn.configure(
                text=f"Slot {slot_idx + 1}\n(Empty)",
                bg=COLORS["bg_medium"],
                fg=COLORS["text_muted"],
            )

    def _register_hotkeys(self):
        """Register global hotkeys for all slots."""
        if not HOTKEYS_AVAILABLE:
            return

        # Unregister existing hotkeys
        for hk in self.registered_hotkeys:
            try:
                keyboard.remove_hotkey(hk)  # type: ignore
            except Exception:
                pass
        self.registered_hotkeys.clear()

        # Register new hotkeys
        for slot_idx, slot in self.sound_slots.items():
            if slot.hotkey:
                try:
                    keyboard.add_hotkey(slot.hotkey, lambda idx=slot_idx: self._play_slot(idx))  # type: ignore
                    self.registered_hotkeys.append(slot.hotkey)
                except Exception:
                    pass

    def _save_config(self):
        """Save configuration to JSON file."""
        config = {"slots": {str(i): s.to_dict() for i, s in self.sound_slots.items()}}
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)

    def _load_config(self):
        """Load configuration from JSON file."""
        if not os.path.exists(CONFIG_FILE):
            return

        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)

            for idx, data in config.get("slots", {}).items():
                self.sound_slots[int(idx)] = SoundSlot.from_dict(data)
                self._update_slot_button(int(idx))

            self._register_hotkeys()
        except Exception as e:
            print(f"Error loading config: {e}")

    def _preload_sounds(self):
        """Preload all configured sounds into memory cache for fast playback."""
        sound_paths = [slot.file_path for slot in self.sound_slots.values() if slot.file_path]
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
