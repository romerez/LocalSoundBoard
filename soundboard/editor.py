"""
Sound Editor with waveform visualization and trimming for the Discord Soundboard.
"""

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf

from .audio import read_audio_file, _resample_audio
from .constants import AUDIO, COLORS


class SoundEditor:
    """
    Sound editor dialog with waveform visualization, trimming, zoom, and preview.

    Features:
    - Visual waveform display showing amplitude
    - Draggable start/end markers for trimming
    - Zoom in/out for precise editing
    - Preview playback of selected portion
    - Warning for sounds longer than 5 seconds
    """

    MAX_DURATION_WARNING = 5.0  # Warn if sound is longer than 5 seconds

    def __init__(
        self,
        parent: tk.Tk,
        file_path: str,
        on_save: Optional[Callable[[np.ndarray, int], None]] = None,
        output_device: Optional[int] = None,
        preloaded_audio: Optional[Tuple[np.ndarray, int]] = None,
    ):
        self.parent = parent
        self.file_path = file_path
        self.on_save = on_save
        self.output_device = output_device

        # Audio data
        self.audio_data: Optional[np.ndarray] = None
        self.sample_rate: int = AUDIO["sample_rate"]
        self.duration: float = 0.0

        # Trim points (in samples)
        self.trim_start: int = 0
        self.trim_end: int = 0

        # Zoom state
        self.zoom_level: float = 1.0
        self.view_start: float = 0.0  # Start position of view (0.0 to 1.0)

        # Playback state
        self.is_playing: bool = False
        self.is_paused: bool = False
        self.play_stream: Optional[sd.OutputStream] = None
        self.play_position: int = 0
        self.play_start_sample: int = (
            0  # Absolute sample where playback started (frozen at play-start)
        )
        self.play_lock = threading.Lock()
        self.selected_audio: Optional[np.ndarray] = None  # Prepared audio for playback

        # Canvas state
        self.canvas_width: int = 700
        self.canvas_height: int = 200
        self.dragging: Optional[str] = None  # "start", "end", or None

        # Marker undo history — list of (trim_start, trim_end) snapshots
        # A snapshot is pushed BEFORE each drag starts so Ctrl+Z can restore it.
        self._marker_history: list = []

        # Multi-cut state — when active, the user captures N segments in one
        # editing session and gets back a list of (audio, sample_rate, title) tuples
        # via `multi_results`. The single-cut `result` is set to the FIRST
        # captured segment for backward compatibility with callers that only
        # read `.result` / the on_save callback.
        self.multi_mode: bool = False
        self.multi_total: int = 0
        self.multi_current: int = 0  # 1-indexed: which segment is being defined
        self.multi_results: list = []  # list[Tuple[np.ndarray, int, str]]

        # Result
        self.result: Optional[Tuple[np.ndarray, int]] = None

        if preloaded_audio is not None:
            data, sr = preloaded_audio
            self._set_audio_data(data, sr)
        else:
            self._load_audio()
        self._create_dialog()

    @classmethod
    def prepare_audio(
        cls,
        file_path: str,
        target_sample_rate: int = AUDIO["sample_rate"],
    ) -> Tuple[np.ndarray, int]:
        """Read and resample audio without touching Tk widgets."""
        data, sr = read_audio_file(file_path)
        if sr != target_sample_rate:
            data = _resample_audio(data, sr, target_sample_rate)
            sr = target_sample_rate
        return data, sr

    def _set_audio_data(self, data: np.ndarray, sample_rate: int):
        """Install decoded audio into the editor state."""
        self.sample_rate = sample_rate

        # Convert stereo to mono for visualization (keep original for playback)
        if data.ndim > 1:
            self.audio_data = data
            self.waveform_data = np.mean(data, axis=1)
        else:
            self.audio_data = data
            self.waveform_data = data

        self.duration = len(self.waveform_data) / self.sample_rate
        self.trim_start = 0
        self.trim_end = len(self.waveform_data)

    def _load_audio(self):
        """Load and prepare audio data."""
        try:
            self._set_audio_data(*self.prepare_audio(self.file_path, self.sample_rate))
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load audio:\n{e}")
            raise

    def _read_audio_file(self, file_path: str) -> Tuple[np.ndarray, int]:
        """
        Read an audio file. Delegates to shared read_audio_file function
        which handles pydub fallback for OGG, M4A, AAC, WMA, etc.
        """
        return read_audio_file(file_path)

    def _create_dialog(self):
        """Create the editor dialog window."""
        self.dialog = tk.Toplevel(self.parent)
        self.dialog.title(f"Edit Sound - {Path(self.file_path).name}")

        # Calculate screen-relative size for dynamic sizing
        screen_w = self.parent.winfo_screenwidth()
        screen_h = self.parent.winfo_screenheight()
        dialog_w = min(950, int(screen_w * 0.7))
        dialog_h = min(680, int(screen_h * 0.75))

        self.dialog.geometry(f"{dialog_w}x{dialog_h}")
        self.dialog.minsize(700, 500)
        self.dialog.configure(
            bg=COLORS["bg_darkest"] if "bg_darkest" in COLORS else COLORS["bg_dark"]
        )
        self.dialog.transient(self.parent)
        self.dialog.grab_set()

        # Center the dialog on the parent window
        self.dialog.update_idletasks()
        parent_x = self.parent.winfo_x()
        parent_y = self.parent.winfo_y()
        parent_w = self.parent.winfo_width()
        parent_h = self.parent.winfo_height()
        x = parent_x + (parent_w - dialog_w) // 2
        y = parent_y + (parent_h - dialog_h) // 2
        # Ensure dialog stays on screen
        x = max(0, min(x, screen_w - dialog_w))
        y = max(0, min(y, screen_h - dialog_h))
        self.dialog.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")

        # Make dialog modal
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_cancel)
        # Undo trim markers
        self.dialog.bind("<Control-z>", self._undo_marker)
        # Spacebar toggles play/pause (works regardless of focused widget)
        self.dialog.bind("<space>", self._on_space_key)
        self.dialog.bind("<KeyPress-space>", self._on_space_key)

        # Main container with padding
        main_frame = tk.Frame(self.dialog, bg=COLORS["bg_dark"], padx=20, pady=15)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Duration warning (if applicable)
        if self.duration > self.MAX_DURATION_WARNING:
            warning_frame = tk.Frame(main_frame, bg="#DA373C", padx=12, pady=8)
            warning_frame.pack(fill=tk.X, pady=(0, 12))
            tk.Label(
                warning_frame,
                text=f"⚠️ This sound is {self.duration:.1f}s long. Consider trimming to ≤{self.MAX_DURATION_WARNING}s for best results.",
                bg="#DA373C",
                fg="white",
                font=("Segoe UI", 10, "bold"),
            ).pack()

        # Info bar
        self._create_info_bar(main_frame)

        # Waveform canvas
        self._create_waveform_canvas(main_frame)

        # Timeline
        self._create_timeline(main_frame)

        # Controls container (zoom + playback in one row)
        controls_frame = tk.Frame(main_frame, bg=COLORS["bg_dark"])
        controls_frame.pack(fill=tk.X, pady=10)

        # Zoom controls on left
        self._create_zoom_controls(controls_frame)

        # Playback controls on right
        self._create_playback_controls(controls_frame)

        # Action buttons at bottom
        self._create_action_buttons(main_frame)

        # Initial draw
        self._draw_waveform()

    def _create_info_bar(self, parent):
        """Create the info bar showing duration and selection."""
        info_frame = tk.Frame(parent, bg=COLORS["bg_dark"])
        info_frame.pack(fill=tk.X, pady=(0, 12))

        self.info_label = tk.Label(
            info_frame,
            text=f"📊 Total: {self.duration:.2f}s | Selected: {self.duration:.2f}s",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=("Segoe UI", 11, "bold"),
        )
        self.info_label.pack(side=tk.LEFT)

        self.trim_info_label = tk.Label(
            info_frame,
            text="💡 Drag the green (start) and red (end) markers to trim",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_muted"],
            font=("Segoe UI", 9),
        )
        self.trim_info_label.pack(side=tk.RIGHT)

    def _create_waveform_canvas(self, parent):
        """Create the waveform visualization canvas."""
        # Container with border
        canvas_container = tk.Frame(parent, bg=COLORS["bg_light"], padx=2, pady=2)
        canvas_container.pack(fill=tk.BOTH, expand=True, pady=8)

        self.canvas = tk.Canvas(
            canvas_container,
            width=self.canvas_width,
            height=self.canvas_height,
            bg="#12141a",  # Darker background for better contrast
            highlightthickness=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Bind mouse events
        self.canvas.bind("<Button-1>", self._on_canvas_left_click)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Button-3>", self._on_canvas_right_click)
        self.canvas.bind("<B3-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-3>", self._on_canvas_release)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)

    def _create_timeline(self, parent):
        """Create the timeline below the waveform."""
        self.timeline_canvas = tk.Canvas(
            parent,
            height=28,
            bg=COLORS["bg_dark"],
            highlightthickness=0,
        )
        self.timeline_canvas.pack(fill=tk.X, pady=(0, 8))

    def _create_zoom_controls(self, parent):
        """Create zoom and navigation controls."""
        zoom_frame = tk.Frame(parent, bg=COLORS["bg_dark"])
        zoom_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Zoom label
        tk.Label(
            zoom_frame,
            text="🔍 Zoom:",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=("Segoe UI", 10),
        ).pack(side=tk.LEFT, padx=(0, 8))

        tk.Button(
            zoom_frame,
            text="−",
            command=self._zoom_out,
            bg=COLORS["bg_medium"],
            fg="white",
            activebackground=COLORS["bg_light"],
            activeforeground="white",
            width=3,
            font=("Segoe UI", 12, "bold"),
            relief="flat",
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=2)

        self.zoom_label = tk.Label(
            zoom_frame,
            text="1.0x",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=("Segoe UI", 10, "bold"),
            width=6,
        )
        self.zoom_label.pack(side=tk.LEFT, padx=5)

        tk.Button(
            zoom_frame,
            text="+",
            command=self._zoom_in,
            bg=COLORS["bg_medium"],
            fg="white",
            activebackground=COLORS["bg_light"],
            activeforeground="white",
            width=3,
            font=("Segoe UI", 12, "bold"),
            relief="flat",
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=2)

        tk.Button(
            zoom_frame,
            text="⟲ Fit All",
            command=self._zoom_fit,
            bg=COLORS["bg_medium"],
            fg="white",
            activebackground=COLORS["bg_light"],
            activeforeground="white",
            font=("Segoe UI", 9),
            width=9,
            relief="flat",
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=12)

        # Navigation label and scroll
        tk.Label(
            zoom_frame,
            text="📍 Navigate:",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=("Segoe UI", 10),
        ).pack(side=tk.LEFT, padx=(10, 5))

        # Horizontal scrollbar for navigation when zoomed
        self.h_scroll = ttk.Scale(
            zoom_frame,
            from_=0,
            to=1,
            orient=tk.HORIZONTAL,
            command=self._on_scroll,
            length=180,
        )
        self.h_scroll.pack(side=tk.LEFT, padx=5)
        self.h_scroll.set(0)

    def _create_playback_controls(self, parent):
        """Create playback preview controls."""
        play_frame = tk.Frame(parent, bg=COLORS["bg_dark"])
        play_frame.pack(side=tk.RIGHT)

        self.play_btn = tk.Button(
            play_frame,
            text="▶ Play Selection",
            command=self._toggle_playback,
            bg=COLORS["green"],
            fg="white",
            activebackground="#1E8E4D",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            width=15,
            relief="flat",
            cursor="hand2",
            pady=4,
        )
        self.play_btn.pack(side=tk.LEFT, padx=5)

        tk.Button(
            play_frame,
            text="⏹ Stop",
            command=self._stop_playback,
            bg=COLORS["red"],
            fg="white",
            activebackground="#B52F33",
            activeforeground="white",
            width=8,
            relief="flat",
            cursor="hand2",
            pady=4,
        ).pack(side=tk.LEFT, padx=5)

        # Selection time display
        self.selection_label = tk.Label(
            play_frame,
            text="📍 0.00s - 0.00s",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=("Segoe UI", 10),
        )
        self.selection_label.pack(side=tk.LEFT, padx=15)

    def _create_action_buttons(self, parent):
        """Create the save/cancel action buttons."""
        # Separator line
        separator = tk.Frame(parent, bg=COLORS["bg_light"], height=1)
        separator.pack(fill=tk.X, pady=(15, 12))

        # Multi-cut status banner (hidden until multi-cut mode is active).
        # Shows progress like "📑 Multi-Cut: capturing 2 of 5" plus a hint,
        # and lets the user bump the total cut count up/down on the fly.
        self._multi_banner = tk.Frame(parent, bg="#5865F2", padx=12, pady=6)
        self._multi_banner_label = tk.Label(
            self._multi_banner,
            text="",
            bg="#5865F2",
            fg="white",
            font=("Segoe UI", 10, "bold"),
        )
        self._multi_banner_label.pack(side=tk.LEFT)

        # Live ± controls on the right side of the banner.
        self._multi_minus_btn = tk.Button(
            self._multi_banner,
            text="➖",
            command=lambda: self._adjust_multi_total(-1),
            bg="#4752C4",
            fg="white",
            activebackground="#3C45A5",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            width=3,
            relief="flat",
            cursor="hand2",
            bd=0,
        )
        self._multi_minus_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self._multi_plus_btn = tk.Button(
            self._multi_banner,
            text="➕",
            command=lambda: self._adjust_multi_total(+1),
            bg="#4752C4",
            fg="white",
            activebackground="#3C45A5",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            width=3,
            relief="flat",
            cursor="hand2",
            bd=0,
        )
        self._multi_plus_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self._multi_total_label = tk.Label(
            self._multi_banner,
            text="",
            bg="#5865F2",
            fg="white",
            font=("Segoe UI", 10, "bold"),
        )
        self._multi_total_label.pack(side=tk.RIGHT, padx=(12, 0))
        # Banner is NOT packed yet — shown only in multi-cut mode.

        btn_frame = tk.Frame(parent, bg=COLORS["bg_dark"])
        btn_frame.pack(fill=tk.X)

        # Reset button on left
        tk.Button(
            btn_frame,
            text="🔄 Reset Selection",
            command=self._reset_selection,
            bg=COLORS["bg_medium"],
            fg="white",
            activebackground=COLORS["bg_light"],
            activeforeground="white",
            font=("Segoe UI", 10),
            width=16,
            relief="flat",
            cursor="hand2",
            pady=5,
        ).pack(side=tk.LEFT)

        # Multi-cut entry button — opens the "how many?" prompt.
        self._multi_btn = tk.Button(
            btn_frame,
            text="📑 Multi-Cut",
            command=self._start_multi_cut,
            bg=COLORS["bg_medium"],
            fg="white",
            activebackground=COLORS["bg_light"],
            activeforeground="white",
            font=("Segoe UI", 10),
            width=12,
            relief="flat",
            cursor="hand2",
            pady=5,
        )
        self._multi_btn.pack(side=tk.LEFT, padx=(8, 0))

        # Action buttons on right
        self._cancel_btn = tk.Button(
            btn_frame,
            text="Cancel",
            command=self._on_cancel,
            bg=COLORS["bg_medium"],
            fg="white",
            activebackground=COLORS["bg_light"],
            activeforeground="white",
            font=("Segoe UI", 10),
            width=10,
            relief="flat",
            cursor="hand2",
            pady=5,
        )
        self._cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))

        self._save_btn = tk.Button(
            btn_frame,
            text="✅ Save & Use",
            command=self._on_save,
            bg=COLORS["blurple"],
            fg="white",
            activebackground=COLORS.get("blurple_hover", "#4752C4"),
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            width=14,
            relief="flat",
            cursor="hand2",
            pady=5,
        )
        self._save_btn.pack(side=tk.RIGHT)

        # Capture button — only visible during multi-cut mode. Replaces the
        # role of "Save & Use" while capturing segments.
        self._capture_btn = tk.Button(
            btn_frame,
            text="",
            command=self._capture_multi_segment,
            bg=COLORS["green"],
            fg="white",
            activebackground="#1E8E4D",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            width=18,
            relief="flat",
            cursor="hand2",
            pady=5,
        )
        # Not packed — shown only in multi-cut mode.

    def _draw_waveform(self):
        """Draw the waveform visualization."""
        self.canvas.delete("all")

        # Get current canvas size
        self.canvas_width = self.canvas.winfo_width() or 700
        self.canvas_height = self.canvas.winfo_height() or 200

        if self.waveform_data is None or len(self.waveform_data) == 0:
            return

        # Calculate visible range based on zoom and scroll
        total_samples = len(self.waveform_data)
        visible_samples = int(total_samples / self.zoom_level)
        start_sample = int(self.view_start * (total_samples - visible_samples))
        end_sample = start_sample + visible_samples

        # Get the visible portion of waveform
        visible_data = self.waveform_data[start_sample:end_sample]

        if len(visible_data) == 0:
            return

        # Downsample for display
        samples_per_pixel = max(1, len(visible_data) // self.canvas_width)
        num_bars = min(self.canvas_width, len(visible_data))

        center_y = self.canvas_height // 2
        max_height = self.canvas_height // 2 - 10

        # Draw background grid
        for i in range(0, self.canvas_height, 20):
            self.canvas.create_line(0, i, self.canvas_width, i, fill="#2a2d31", width=1)

        # Draw center line
        self.canvas.create_line(0, center_y, self.canvas_width, center_y, fill="#3a3d41", width=1)

        # Draw waveform bars
        for i in range(num_bars):
            sample_start = int(i * len(visible_data) / num_bars)
            sample_end = int((i + 1) * len(visible_data) / num_bars)

            if sample_start >= len(visible_data):
                break

            chunk = visible_data[sample_start:sample_end]
            if len(chunk) == 0:
                continue

            # Get max amplitude for this chunk
            amplitude = np.max(np.abs(chunk))
            bar_height = int(amplitude * max_height)

            x = i

            # Check if this bar is in the selection
            actual_sample = start_sample + sample_start
            in_selection = self.trim_start <= actual_sample < self.trim_end

            # Color based on selection
            if in_selection:
                color = COLORS["blurple"]
            else:
                color = "#4a4d51"

            # Draw the bar (from center up and down)
            if bar_height > 0:
                self.canvas.create_line(
                    x, center_y - bar_height, x, center_y + bar_height, fill=color, width=1
                )

        # Draw trim markers
        self._draw_trim_markers(start_sample, end_sample)

        # Draw playback position if playing
        if self.is_playing:
            self._draw_playback_position(start_sample, end_sample)

        # Update timeline
        self._draw_timeline(start_sample, end_sample)

        # Update info labels
        self._update_info_labels()

    def _draw_trim_markers(self, view_start: int, view_end: int):
        """Draw the start and end trim markers."""
        view_range = view_end - view_start

        # Start marker (green)
        if view_start <= self.trim_start < view_end:
            x = int((self.trim_start - view_start) / view_range * self.canvas_width)
            self.canvas.create_line(
                x, 0, x, self.canvas_height, fill=COLORS["green"], width=2, tags="start_marker"
            )
            # Draw handle
            self.canvas.create_polygon(
                x - 8, 0, x + 8, 0, x, 15, fill=COLORS["green"], tags="start_marker"
            )
            self.canvas.create_text(
                x,
                self.canvas_height - 5,
                text="◀ START (L)",
                fill=COLORS["green"],
                font=("Segoe UI", 8, "bold"),
                anchor="s",
            )

        # End marker (red)
        if view_start < self.trim_end <= view_end:
            x = int((self.trim_end - view_start) / view_range * self.canvas_width)
            self.canvas.create_line(
                x, 0, x, self.canvas_height, fill=COLORS["red"], width=2, tags="end_marker"
            )
            # Draw handle
            self.canvas.create_polygon(
                x - 8, 0, x + 8, 0, x, 15, fill=COLORS["red"], tags="end_marker"
            )
            self.canvas.create_text(
                x,
                self.canvas_height - 5,
                text="END (R) ▶",
                fill=COLORS["red"],
                font=("Segoe UI", 8, "bold"),
                anchor="s",
            )

    def _draw_playback_position(self, view_start: int, view_end: int):
        """Draw the current playback position indicator."""
        with self.play_lock:
            # Use play_start_sample (frozen at play-start) so that dragging
            # trim markers while playing does NOT affect the needle position.
            pos = self.play_position + self.play_start_sample

        view_range = view_end - view_start
        if view_start <= pos < view_end:
            x = int((pos - view_start) / view_range * self.canvas_width)
            self.canvas.create_line(
                x, 0, x, self.canvas_height, fill="white", width=2, tags="playhead"
            )

    def _draw_timeline(self, view_start: int, view_end: int):
        """Draw time markers on the timeline."""
        self.timeline_canvas.delete("all")

        width = self.timeline_canvas.winfo_width() or self.canvas_width
        view_duration = (view_end - view_start) / self.sample_rate
        start_time = view_start / self.sample_rate

        # Determine time interval based on zoom
        if view_duration <= 1:
            interval = 0.1
        elif view_duration <= 5:
            interval = 0.5
        elif view_duration <= 15:
            interval = 1.0
        else:
            interval = 2.0

        # Draw time markers
        t = (start_time // interval + 1) * interval
        while t < start_time + view_duration:
            x = int((t - start_time) / view_duration * width)
            self.timeline_canvas.create_line(x, 0, x, 8, fill=COLORS["text_muted"], width=1)
            self.timeline_canvas.create_text(
                x, 15, text=f"{t:.1f}s", fill=COLORS["text_muted"], font=("Segoe UI", 8)
            )
            t += interval

    def _update_info_labels(self):
        """Update the info labels with current selection info."""
        total_duration = self.duration
        selected_duration = (self.trim_end - self.trim_start) / self.sample_rate

        self.info_label.config(
            text=f"📊 Total: {total_duration:.2f}s | Selected: {selected_duration:.2f}s"
        )

        # selection_label may not exist during initial UI construction
        if hasattr(self, "selection_label"):
            start_time = self.trim_start / self.sample_rate
            end_time = self.trim_end / self.sample_rate
            self.selection_label.config(text=f"📍 {start_time:.2f}s - {end_time:.2f}s")

    def _on_canvas_left_click(self, event):
        """Left click sets / drags the START marker."""
        # Save snapshot before starting drag
        self._marker_history.append((self.trim_start, self.trim_end))
        self.dragging = "start"
        self._update_marker_position(event.x)

    def _on_canvas_right_click(self, event):
        """Right click sets / drags the END marker."""
        # Save snapshot before starting drag
        self._marker_history.append((self.trim_start, self.trim_end))
        self.dragging = "end"
        self._update_marker_position(event.x)

    def _on_canvas_click(self, event):
        """Legacy handler — kept for safety, delegates to left-click."""
        self._on_canvas_left_click(event)

    def _on_canvas_drag(self, event):
        """Handle dragging on the canvas."""
        if self.dragging:
            self._update_marker_position(event.x)

    def _on_canvas_release(self, event):
        """Handle mouse release."""
        self.dragging = None

    def _undo_marker(self, event=None):
        """Restore the previous trim marker positions (Ctrl+Z)."""
        if not self._marker_history:
            return
        self.trim_start, self.trim_end = self._marker_history.pop()
        self._draw_waveform()
        self._update_info_labels()

    def _update_marker_position(self, x: int):
        """Update the position of the dragged marker."""
        total_samples = len(self.waveform_data)
        visible_samples = int(total_samples / self.zoom_level)
        view_start = int(self.view_start * (total_samples - visible_samples))
        view_end = view_start + visible_samples
        view_range = view_end - view_start

        # Convert x position to sample position
        sample_pos = view_start + int(x / self.canvas_width * view_range)
        sample_pos = max(0, min(sample_pos, total_samples))

        if self.dragging == "start":
            # Start can't go past end
            self.trim_start = min(sample_pos, self.trim_end - 1)
        elif self.dragging == "end":
            # End can't go before start
            self.trim_end = max(sample_pos, self.trim_start + 1)

        self._draw_waveform()

    def _on_canvas_resize(self, event):
        """Handle canvas resize."""
        self.canvas_width = event.width
        self.canvas_height = event.height
        self._draw_waveform()

    def _on_mouse_wheel(self, event):
        """Handle mouse wheel: Ctrl+scroll = zoom centered on cursor, plain scroll = pan left/right."""
        if event.state & 0x0004:  # Ctrl held → zoom
            if event.delta > 0:
                self._zoom_in_at(event.x)
            else:
                self._zoom_out_at(event.x)
        else:  # No modifier → pan horizontally
            # scroll up (delta>0) = go back (left), scroll down = go forward (right)
            self._pan_view(-1 if event.delta > 0 else 1)

    def _pan_view(self, direction: int):
        """Pan the waveform view left (direction=-1) or right (direction=1)."""
        if self.zoom_level <= 1.0:
            return
        total = len(self.waveform_data)
        if total == 0:
            return
        visible = total / self.zoom_level
        scrollable = total - visible
        if scrollable <= 0:
            return
        # Step = 15% of the currently visible window, converted to view_start units
        step = (visible * 0.15) / scrollable
        self.view_start = max(0.0, min(1.0, self.view_start + direction * step))
        self.h_scroll.set(self.view_start)
        self._draw_waveform()

    def _zoom_in_at(self, cursor_x: int):
        """Zoom in centered on the given X position."""
        if self.zoom_level >= 50.0:
            return

        total_samples = len(self.waveform_data)
        if total_samples == 0:
            return

        # Calculate which sample is under the cursor before zoom
        visible_samples = int(total_samples / self.zoom_level)
        view_start_sample = int(self.view_start * max(1, total_samples - visible_samples))
        cursor_ratio = cursor_x / max(1, self.canvas_width)
        cursor_sample = view_start_sample + int(cursor_ratio * visible_samples)

        # Apply zoom
        old_zoom = self.zoom_level
        self.zoom_level = min(self.zoom_level * 1.5, 50.0)

        # Calculate new visible samples after zoom
        new_visible_samples = int(total_samples / self.zoom_level)

        # Adjust view_start so cursor_sample stays at the same relative position
        new_view_start_sample = cursor_sample - int(cursor_ratio * new_visible_samples)
        new_view_start_sample = max(
            0, min(new_view_start_sample, total_samples - new_visible_samples)
        )

        # Convert to normalized position (0.0 to 1.0)
        if total_samples > new_visible_samples:
            self.view_start = new_view_start_sample / (total_samples - new_visible_samples)
        else:
            self.view_start = 0.0

        self.view_start = max(0.0, min(1.0, self.view_start))
        self.h_scroll.set(self.view_start)
        self.zoom_label.config(text=f"{self.zoom_level:.1f}x")
        self._draw_waveform()

    def _zoom_out_at(self, cursor_x: int):
        """Zoom out centered on the given X position."""
        if self.zoom_level <= 1.0:
            return

        total_samples = len(self.waveform_data)
        if total_samples == 0:
            return

        # Calculate which sample is under the cursor before zoom
        visible_samples = int(total_samples / self.zoom_level)
        view_start_sample = int(self.view_start * max(1, total_samples - visible_samples))
        cursor_ratio = cursor_x / max(1, self.canvas_width)
        cursor_sample = view_start_sample + int(cursor_ratio * visible_samples)

        # Apply zoom
        old_zoom = self.zoom_level
        self.zoom_level = max(self.zoom_level / 1.5, 1.0)

        # Calculate new visible samples after zoom
        new_visible_samples = int(total_samples / self.zoom_level)

        # If zoom is 1.0 (fit all), reset view
        if self.zoom_level <= 1.0:
            self.view_start = 0.0
            self.h_scroll.set(0)
            self.zoom_label.config(text="1.0x")
            self._draw_waveform()
            return

        # Adjust view_start so cursor_sample stays at the same relative position
        new_view_start_sample = cursor_sample - int(cursor_ratio * new_visible_samples)
        new_view_start_sample = max(
            0, min(new_view_start_sample, total_samples - new_visible_samples)
        )

        # Convert to normalized position (0.0 to 1.0)
        if total_samples > new_visible_samples:
            self.view_start = new_view_start_sample / (total_samples - new_visible_samples)
        else:
            self.view_start = 0.0

        self.view_start = max(0.0, min(1.0, self.view_start))
        self.h_scroll.set(self.view_start)
        self.zoom_label.config(text=f"{self.zoom_level:.1f}x")
        self._draw_waveform()

    def _zoom_in(self):
        """Zoom in on the waveform (centered on view)."""
        # Use center of canvas as cursor position
        self._zoom_in_at(self.canvas_width // 2)

    def _zoom_out(self):
        """Zoom out on the waveform (centered on view)."""
        # Use center of canvas as cursor position
        self._zoom_out_at(self.canvas_width // 2)

    def _zoom_fit(self):
        """Reset zoom to fit all."""
        self.zoom_level = 1.0
        self.view_start = 0.0
        self.h_scroll.set(0)
        self.zoom_label.config(text="1.0x")
        self._draw_waveform()

    def _update_scroll_range(self):
        """Update the scroll range based on zoom level."""
        if self.zoom_level <= 1.0:
            self.view_start = 0.0
            self.h_scroll.set(0)

    def _on_scroll(self, value):
        """Handle scroll bar changes."""
        self.view_start = float(value)
        self._draw_waveform()

    def _toggle_playback(self):
        """Toggle playback of selected portion."""
        if self.is_playing:
            self._pause_playback()
        else:
            self._start_playback()

    def _on_space_key(self, event):
        """Spacebar shortcut: play/pause. Ignored when typing in an Entry."""
        try:
            focused = self.dialog.focus_get()
            if isinstance(focused, (tk.Entry, tk.Text)):
                return None
        except Exception:
            pass
        self._toggle_playback()
        return "break"

    def _pause_playback(self):
        """Pause playback (keep position for resume)."""
        # Set flags first - callback will stop producing audio immediately
        self.is_playing = False
        self.is_paused = True

        # Update button immediately for responsive UI
        self.play_btn.config(text="▶ Resume", bg=COLORS["green"])

        # Close stream in background thread to avoid UI freeze
        stream = self.play_stream
        self.play_stream = None

        if stream:

            def close_stream():
                try:
                    stream.abort()
                    stream.close()
                except Exception:
                    pass

            threading.Thread(target=close_stream, daemon=True).start()

    def _prepare_audio_for_playback(self):
        """Prepare the selected audio data for playback."""
        if self.audio_data is None:
            return

        audio_slice = self.audio_data[self.trim_start : self.trim_end]

        if audio_slice.ndim == 1:
            # Convert mono to stereo
            self.selected_audio = np.column_stack([audio_slice, audio_slice]).astype(np.float32)
        elif audio_slice.ndim > 1 and audio_slice.shape[1] == 1:
            # Single-channel 2D array
            self.selected_audio = np.column_stack([audio_slice[:, 0], audio_slice[:, 0]]).astype(
                np.float32
            )
        elif audio_slice.ndim > 1 and audio_slice.shape[1] >= 2:
            # Already stereo (or more), take first 2 channels
            self.selected_audio = np.ascontiguousarray(audio_slice[:, :2], dtype=np.float32)
        else:
            self.selected_audio = np.column_stack([audio_slice, audio_slice]).astype(np.float32)

    def _start_playback(self):
        """Start or resume playing the selected portion."""
        if self.audio_data is None:
            return

        # If not paused, prepare new audio and reset position
        if not self.is_paused:
            self._prepare_audio_for_playback()
            self.play_position = 0
            self.play_start_sample = self.trim_start  # Freeze the absolute start offset

        self.is_playing = True
        self.is_paused = False
        self.play_btn.config(text="⏸ Pause", bg=COLORS["blurple"])

        # Use instance variable for callback closure
        selected_audio = self.selected_audio
        if selected_audio is None:
            self.is_playing = False
            return

        def audio_callback(outdata, frames, time, status):
            with self.play_lock:
                if not self.is_playing:
                    outdata.fill(0)
                    raise sd.CallbackStop()

                remaining = len(selected_audio) - self.play_position
                if remaining <= 0:
                    outdata.fill(0)
                    self.is_playing = False
                    raise sd.CallbackStop()

                chunk_size = min(frames, remaining)
                outdata[:chunk_size] = selected_audio[
                    self.play_position : self.play_position + chunk_size
                ]
                if chunk_size < frames:
                    outdata[chunk_size:] = 0

                self.play_position += chunk_size

        def on_finished():
            self.is_playing = False
            self.is_paused = False
            self.play_position = 0
            # Check if dialog still exists before updating UI
            try:
                if self.dialog.winfo_exists():
                    self.dialog.after(
                        0, lambda: self._safe_update_play_btn("▶ Play Selection", COLORS["green"])
                    )
                    self.dialog.after(0, self._draw_waveform)
            except Exception:
                pass  # Dialog was destroyed, ignore

        try:
            self.play_stream = sd.OutputStream(
                device=self.output_device,
                samplerate=self.sample_rate,
                channels=2,
                callback=audio_callback,
                finished_callback=on_finished,
            )
            self.play_stream.start()

            # Start playhead update
            self._update_playhead()

        except Exception as e:
            self.is_playing = False
            self.is_paused = False
            self.play_btn.config(text="▶ Play Selection", bg=COLORS["green"])
            messagebox.showerror("Playback Error", f"Failed to start playback:\n{e}")

    def _update_playhead(self):
        """Update only the playhead position during playback (avoids full redraw)."""
        if self.is_playing:
            self.canvas.delete("playhead")
            total_samples = len(self.waveform_data)
            visible_samples = int(total_samples / self.zoom_level)
            view_start = int(self.view_start * (total_samples - visible_samples))
            view_end = view_start + visible_samples
            self._draw_playback_position(view_start, view_end)
            self._update_info_labels()
            self.dialog.after(50, self._update_playhead)

    def _safe_update_play_btn(self, text: str, bg_color: str):
        """Safely update play button text and color, handling destroyed widgets."""
        try:
            if hasattr(self, "play_btn") and self.play_btn.winfo_exists():
                self.play_btn.config(text=text, bg=bg_color)
        except Exception:
            pass  # Widget was destroyed

    def _stop_playback(self):
        """Stop playback and reset position."""
        # Set flags first - callback will stop producing audio immediately
        self.is_playing = False
        self.is_paused = False
        self.play_position = 0

        # Update button and waveform immediately for responsive UI
        self.play_btn.config(text="▶ Play Selection", bg=COLORS["green"])
        self._draw_waveform()

        # Close stream in background thread to avoid UI freeze
        stream = self.play_stream
        self.play_stream = None

        if stream:

            def close_stream():
                try:
                    stream.abort()
                    stream.close()
                except Exception:
                    pass

            threading.Thread(target=close_stream, daemon=True).start()

    def _reset_selection(self):
        """Reset trim selection to full audio."""
        self.trim_start = 0
        self.trim_end = len(self.waveform_data)
        self._draw_waveform()

    # ------------------------------------------------------------------
    # Multi-cut: capture N segments from the same source in one session.
    # ------------------------------------------------------------------

    def _start_multi_cut(self):
        """Prompt for the number of cuts, then enter multi-cut mode."""
        if self.audio_data is None:
            return

        # Build a tiny modal that uses an integer Spinbox (arrow up/down) so the
        # UX matches what the user described.
        prompt = tk.Toplevel(self.dialog)
        prompt.title("Multi-Cut")
        prompt.configure(bg=COLORS["bg_dark"])
        prompt.transient(self.dialog)
        prompt.grab_set()
        prompt.resizable(False, False)

        tk.Label(
            prompt,
            text="How many cuts do you want to make?",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=("Segoe UI", 11, "bold"),
            padx=20,
            pady=12,
        ).pack()

        count_var = tk.IntVar(value=2)
        spin = tk.Spinbox(
            prompt,
            from_=2,
            to=50,
            textvariable=count_var,
            width=6,
            font=("Segoe UI", 14, "bold"),
            justify="center",
            bg=COLORS["bg_medium"],
            fg="white",
            insertbackground="white",
            relief="flat",
            buttonbackground=COLORS["bg_light"],
        )
        spin.pack(pady=(0, 12))

        btn_row = tk.Frame(prompt, bg=COLORS["bg_dark"])
        btn_row.pack(pady=(0, 12), padx=20)

        def cancel():
            prompt.destroy()

        def ok():
            try:
                n = int(count_var.get())
            except (tk.TclError, ValueError):
                n = 0
            if n < 2:
                messagebox.showwarning("Multi-Cut", "Pick at least 2 cuts.", parent=prompt)
                return
            prompt.destroy()
            self._enter_multi_cut_mode(n)

        tk.Button(
            btn_row,
            text="Cancel",
            command=cancel,
            bg=COLORS["bg_medium"],
            fg="white",
            font=("Segoe UI", 10),
            width=10,
            relief="flat",
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=5)

        tk.Button(
            btn_row,
            text="Start",
            command=ok,
            bg=COLORS["blurple"],
            fg="white",
            font=("Segoe UI", 10, "bold"),
            width=10,
            relief="flat",
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=5)

        # Center prompt over editor dialog
        prompt.update_idletasks()
        ex = self.dialog.winfo_rootx()
        ey = self.dialog.winfo_rooty()
        ew = self.dialog.winfo_width()
        eh = self.dialog.winfo_height()
        pw = prompt.winfo_width()
        ph = prompt.winfo_height()
        prompt.geometry(f"+{ex + (ew - pw) // 2}+{ey + (eh - ph) // 3}")

        spin.focus_set()
        try:
            spin.selection_range(0, "end")  # type: ignore[arg-type]
        except Exception:
            pass
        prompt.bind("<Return>", lambda e: ok())
        prompt.bind("<Escape>", lambda e: cancel())

    def _enter_multi_cut_mode(self, total: int):
        """Switch the editor into multi-cut mode for `total` segments."""
        self.multi_mode = True
        self.multi_total = total
        self.multi_current = 1
        self.multi_results = []

        # Reset the selection so the user starts clean for cut 1.
        self.trim_start = 0
        self.trim_end = min(len(self.waveform_data), self.sample_rate)  # default 1s window

        # Hide the normal Save button, show Capture button + banner.
        self._save_btn.pack_forget()
        self._multi_btn.pack_forget()
        self._capture_btn.pack(side=tk.RIGHT)
        self._multi_banner.pack(fill=tk.X, pady=(0, 10), before=self._capture_btn.master)

        self._update_multi_banner()
        self._draw_waveform()

    def _update_multi_banner(self):
        """Refresh the multi-cut banner + capture button label."""
        if not self.multi_mode:
            return
        self._multi_banner_label.config(
            text=(
                f"📑 Multi-Cut: capturing cut {self.multi_current} of {self.multi_total}  "
                f"— left-click to set START, right-click to set END, "
                f"SPACE to preview, then click ✓ Capture"
            )
        )
        self._capture_btn.config(
            text=f"✓ Capture cut {self.multi_current}/{self.multi_total}"
        )
        # Update ± controls. Minus is disabled when reducing further would
        # drop below the current cut (or below the 2-cut minimum).
        try:
            self._multi_total_label.config(text=f"Total: {self.multi_total}")
            min_total = max(2, self.multi_current)
            self._multi_minus_btn.config(
                state=(tk.NORMAL if self.multi_total > min_total else tk.DISABLED)
            )
            self._multi_plus_btn.config(
                state=(tk.NORMAL if self.multi_total < 50 else tk.DISABLED)
            )
        except (AttributeError, tk.TclError):
            pass

    def _adjust_multi_total(self, delta: int):
        """Bump `multi_total` by +/- 1 while in multi-cut mode.

        Lower bound: `max(2, multi_current)` so the user can't drop below
        the cut they're currently working on. Upper bound: 50 to match the
        Spinbox prompt limit.
        """
        if not self.multi_mode:
            return
        new_total = self.multi_total + delta
        min_total = max(2, self.multi_current)
        if new_total < min_total or new_total > 50:
            return
        self.multi_total = new_total
        self._update_multi_banner()

    def _prompt_multi_cut_title(self) -> Optional[str]:
        """Prompt for the current multi-cut title. Returns None if cancelled."""
        prompt = tk.Toplevel(self.dialog)
        prompt.title("Cut Title")
        prompt.configure(bg=COLORS["bg_dark"])
        prompt.transient(self.dialog)
        prompt.grab_set()
        prompt.resizable(False, False)

        result: list[Optional[str]] = [None]
        default_title = f"{Path(self.file_path).stem} {self.multi_current}"
        title_var = tk.StringVar(value=default_title)

        tk.Label(
            prompt,
            text=f"Title for cut {self.multi_current}:",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=("Segoe UI", 11, "bold"),
            padx=20,
            pady=10,
        ).pack(anchor="w")

        entry = tk.Entry(
            prompt,
            textvariable=title_var,
            width=34,
            font=("Segoe UI", 12),
            bg=COLORS["bg_medium"],
            fg="white",
            insertbackground="white",
            relief="flat",
        )
        entry.pack(fill=tk.X, padx=20, pady=(0, 12))

        btn_row = tk.Frame(prompt, bg=COLORS["bg_dark"])
        btn_row.pack(pady=(0, 12), padx=20, anchor="e")

        def close():
            result[0] = None
            prompt.destroy()

        def ok():
            title = title_var.get().strip()
            if not title:
                messagebox.showwarning(
                    "Multi-Cut",
                    "Please enter a title for this cut.",
                    parent=prompt,
                )
                return
            result[0] = title
            prompt.destroy()

        tk.Button(
            btn_row,
            text="Cancel",
            command=close,
            bg=COLORS["bg_medium"],
            fg="white",
            font=("Segoe UI", 10),
            width=10,
            relief="flat",
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=5)

        tk.Button(
            btn_row,
            text="Use Title",
            command=ok,
            bg=COLORS["blurple"],
            fg="white",
            font=("Segoe UI", 10, "bold"),
            width=10,
            relief="flat",
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=5)

        prompt.update_idletasks()
        ex = self.dialog.winfo_rootx()
        ey = self.dialog.winfo_rooty()
        ew = self.dialog.winfo_width()
        eh = self.dialog.winfo_height()
        pw = prompt.winfo_width()
        ph = prompt.winfo_height()
        prompt.geometry(f"+{ex + (ew - pw) // 2}+{ey + (eh - ph) // 3}")

        prompt.protocol("WM_DELETE_WINDOW", close)
        prompt.bind("<Return>", lambda _e: ok())
        prompt.bind("<Escape>", lambda _e: close())
        entry.focus_set()
        try:
            entry.selection_range(0, "end")
        except Exception:
            pass

        self.dialog.wait_window(prompt)
        return result[0]

    def _capture_multi_segment(self):
        """Capture the current selection as the next multi-cut segment."""
        if not self.multi_mode or self.audio_data is None:
            return

        if self.trim_end <= self.trim_start:
            messagebox.showwarning(
                "Multi-Cut",
                "The selection is empty. Set start (left-click) and end (right-click) first.",
                parent=self.dialog,
            )
            return

        title = self._prompt_multi_cut_title()
        if title is None:
            return

        # Stop any preview before slicing.
        self._stop_playback()

        segment = np.ascontiguousarray(
            self.audio_data[self.trim_start : self.trim_end].copy()
        )
        self.multi_results.append((segment, self.sample_rate, title))

        # Advance or finish.
        if self.multi_current >= self.multi_total:
            # All segments captured \u2014 set single result to the FIRST cut for
            # backward compatibility, then close.
            if self.multi_results:
                first_audio, first_sr = self.multi_results[0][:2]
                self.result = (first_audio, first_sr)
                if self.on_save:
                    try:
                        self.on_save(first_audio, first_sr)
                    except Exception:
                        pass
            self.dialog.destroy()
            return

        self.multi_current += 1
        # Reset selection for the next cut so the user starts fresh.
        self.trim_start = 0
        self.trim_end = min(len(self.waveform_data), self.sample_rate)
        self._update_multi_banner()
        self._draw_waveform()

    def _on_save(self):
        """Save the trimmed audio and close."""
        self._stop_playback()

        if self.audio_data is None:
            self.result = None
            self.dialog.destroy()
            return

        trimmed_audio = self.audio_data[self.trim_start : self.trim_end]

        self.result = (trimmed_audio, self.sample_rate)

        if self.on_save:
            self.on_save(trimmed_audio, self.sample_rate)

        self.dialog.destroy()

    def _on_cancel(self):
        """Cancel and close without saving."""
        self._stop_playback()
        self.result = None
        self.dialog.destroy()

    def show(self) -> Optional[Tuple[np.ndarray, int]]:
        """Show the dialog and wait for result."""
        self.dialog.wait_window()
        return self.result


def edit_sound_file(
    parent: tk.Tk,
    file_path: str,
    output_device: Optional[int] = None,
) -> Optional[Tuple[np.ndarray, int]]:
    """
    Open the sound editor dialog for a file.

    Returns (trimmed_audio, sample_rate) if saved, None if cancelled.
    """
    try:
        editor = SoundEditor(parent, file_path, output_device=output_device)
        return editor.show()
    except Exception as e:
        messagebox.showerror("Error", f"Failed to open sound editor:\n{e}")
        return None
