"""
Sound Editor with waveform visualization and trimming for the Discord Soundboard.
"""

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional, Tuple

import customtkinter as ctk
import numpy as np
import sounddevice as sd
import soundfile as sf

from .audio import read_audio_file, _resample_audio, decode_audio_range
from .constants import AUDIO, COLORS, FONTS, UI


def _window_scaling(widget) -> float:
    """CTk's DPI factor for *widget*'s window (1.0 fallback).

    The editor dialogs are RAW Tk, so their fonts must be given in DEVICE
    pixels (a NEGATIVE Tk font size); a positive size is points that Tk scales
    by its own factor, not CTk's, which is why editor text never quite matched
    the rest of the app.
    """
    try:
        return float(ctk.ScalingTracker.get_window_scaling(widget.winfo_toplevel()))
    except Exception:
        pass
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:
        return 1.0


def _tk_font(scale: float, px: int, bold: bool = False) -> tuple:
    """Raw-Tk font tuple: FONTS family at ``-round(px * scale)`` device px."""
    size = -max(1, round(px * scale))
    return (FONTS["family"], size, "bold") if bold else (FONTS["family"], size)


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

    def _font(self, px: int, bold: bool = False) -> tuple:
        """Raw-Tk font in DEVICE px (see _tk_font)."""
        return _tk_font(self._s, px, bold)

    def __init__(
        self,
        parent: tk.Tk,
        file_path: str,
        on_save: Optional[Callable[[np.ndarray, int], None]] = None,
        output_device: Optional[int] = None,
        preloaded_audio: Optional[Tuple[np.ndarray, int]] = None,
        person_names: Optional[list] = None,
    ):
        self.parent = parent
        # DPI factor (CTk's) — raw-Tk fonts below are sized in device px.
        self._s = _window_scaling(parent)
        if not (0.4 <= self._s <= 8.0):
            self._s = 1.0
        self.file_path = file_path
        self.on_save = on_save
        self.output_device = output_device
        # When editing a recording, the caller can pass the list of person names
        # so each cut can be tagged to a person ("— none —" leaves it untagged).
        # The chosen person travels with each multi-cut result (4th tuple slot)
        # and as ``result_person`` for a single save.
        self.person_names: list = list(person_names) if person_names else []
        self.result_person: Optional[str] = None
        self.result_title: Optional[str] = None

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
                font=self._font(FONTS["size_xs"], True),
            ).pack()

        # Info bar
        self._create_info_bar(main_frame)

        # Bottom section — timeline, transport controls and the Save/Cancel
        # buttons — is pinned to the BOTTOM *before* the canvas is packed. With
        # the waveform canvas taking expand=True, all shrinking is absorbed by
        # the canvas, so these controls (especially Save & Use) can never be
        # pushed off-screen / clipped when the window isn't opened all the way.
        bottom = tk.Frame(main_frame, bg=COLORS["bg_dark"])
        bottom.pack(side=tk.BOTTOM, fill=tk.X)

        # Waveform canvas fills the remaining middle space.
        self._create_waveform_canvas(main_frame)

        # Timeline (just under the waveform)
        self._create_timeline(bottom)

        # Controls container (zoom + playback in one row)
        controls_frame = tk.Frame(bottom, bg=COLORS["bg_dark"])
        controls_frame.pack(fill=tk.X, pady=10)

        # Zoom controls on left
        self._create_zoom_controls(controls_frame)

        # Playback controls on right
        self._create_playback_controls(controls_frame)

        # Action buttons at the very bottom (always visible)
        self._create_action_buttons(bottom)

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
            font=self._font(FONTS["size_sm"], True),
        )
        self.info_label.pack(side=tk.LEFT)

        self.trim_info_label = tk.Label(
            info_frame,
            text="💡 Drag the green (start) and red (end) markers to trim",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_muted"],
            font=self._font(9),
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
            font=self._font(FONTS["size_xs"]),
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
            font=self._font(12, True),
            relief="flat",
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=2)

        self.zoom_label = tk.Label(
            zoom_frame,
            text="1.0x",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=self._font(FONTS["size_xs"], True),
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
            font=self._font(12, True),
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
            font=self._font(9),
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
            font=self._font(FONTS["size_xs"]),
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
            font=self._font(FONTS["size_xs"], True),
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
            font=self._font(FONTS["size_xs"]),
        )
        self.selection_label.pack(side=tk.LEFT, padx=15)

    def _create_action_buttons(self, parent):
        """Create the save/cancel action buttons."""
        # Separator line
        separator = tk.Frame(parent, bg=COLORS["bg_light"], height=1)
        separator.pack(fill=tk.X, pady=(15, 12))

        # Multi-cut status banner (hidden until multi-cut mode is active).
        # In open-ended multi-cut you keep capturing segments until you click
        # Done — so this just shows a running count + a hint, no fixed total.
        self._multi_banner = tk.Frame(parent, bg="#5865F2", padx=12, pady=6)
        self._multi_banner_label = tk.Label(
            self._multi_banner,
            text="",
            bg="#5865F2",
            fg="white",
            font=self._font(FONTS["size_xs"], True),
        )
        self._multi_banner_label.pack(side=tk.LEFT)
        # Banner is NOT packed yet — shown only in multi-cut mode.

        btn_frame = tk.Frame(parent, bg=COLORS["bg_dark"])
        btn_frame.pack(fill=tk.X)

        # Footer buttons follow the app's style spec: CTkButtons 36 px tall,
        # 6 px radius; PRIMARY (green, bold sm) is the committing action, the
        # rest are SECONDARY (bg_light). CTk scales these itself, so they
        # match every other dialog footer at any DPI.
        fh, fr = UI["footer_button_height"], UI["button_corner_radius"]
        f_sm = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"])
        f_sm_b = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold")
        secondary = dict(
            fg_color=COLORS["bg_light"], hover_color=COLORS["bg_lighter"],
            text_color="white", font=f_sm, height=fh, corner_radius=fr,
        )
        primary = dict(
            fg_color=COLORS["green"], hover_color=COLORS["green_hover"],
            text_color="white", font=f_sm_b, height=fh, corner_radius=fr,
        )

        # Reset button on left
        ctk.CTkButton(
            btn_frame, text="🔄 Reset Selection", command=self._reset_selection,
            width=150, **secondary,
        ).pack(side=tk.LEFT)

        # Multi-cut entry button — opens the "how many?" prompt.
        self._multi_btn = ctk.CTkButton(
            btn_frame, text="📑 Multi-Cut", command=self._start_multi_cut,
            width=120, **secondary,
        )
        self._multi_btn.pack(side=tk.LEFT, padx=(8, 0))

        # Action buttons on right
        self._cancel_btn = ctk.CTkButton(
            btn_frame, text="Cancel", command=self._on_cancel, width=100, **secondary,
        )
        self._cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))

        self._save_btn = ctk.CTkButton(
            btn_frame, text="✓ Save & Use", command=self._on_save, width=140, **primary,
        )
        self._save_btn.pack(side=tk.RIGHT)

        # Capture button — only visible during multi-cut mode. Grabs the current
        # selection as one segment; you can capture as many as you like.
        # Blurple accent: the frequent action, distinct from the green Done.
        self._capture_btn = ctk.CTkButton(
            btn_frame, text="", command=self._capture_multi_segment, width=170,
            fg_color=COLORS["blurple"], hover_color=COLORS["blurple_hover"],
            text_color="white", font=f_sm_b, height=fh, corner_radius=fr,
        )

        # Done button — only visible during multi-cut mode. Finishes capturing
        # and turns every captured segment into a sound. The user decides when
        # they're done instead of committing to a count up front.
        self._done_btn = ctk.CTkButton(
            btn_frame, text="✓ Done", command=self._finish_multi_cut, width=110, **primary,
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
                font=self._font(8, True),
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
                font=self._font(8, True),
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
                x, 15, text=f"{t:.1f}s", fill=COLORS["text_muted"], font=self._font(8)
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
        """Handle canvas resize — debounced.

        A live dialog resize fires <Configure> per pixel and the full waveform
        redraw (delete-all + ~one create_line per pixel column) costs tens of
        ms, so redrawing only once the size settles keeps the drag smooth.
        """
        self.canvas_width = event.width
        self.canvas_height = event.height
        aid = getattr(self, "_resize_redraw_after", None)
        if aid is not None:
            try:
                self.canvas.after_cancel(aid)
            except Exception:
                pass
        self._resize_redraw_after = self.canvas.after(80, self._redraw_after_resize)

    def _redraw_after_resize(self):
        # The timer lives on the interpreter, not the widget — it can fire
        # after the dialog was closed.
        self._resize_redraw_after = None
        try:
            if not self.canvas.winfo_exists():
                return
        except Exception:
            return
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
        """Enter open-ended multi-cut mode immediately (no count to pick)."""
        if self.audio_data is None:
            return
        self._enter_multi_cut_mode()

    def _enter_multi_cut_mode(self):
        """Switch the editor into open-ended multi-cut mode.

        The user captures as many segments as they want and clicks Done when
        finished — no need to commit to a number up front.
        """
        self.multi_mode = True
        self.multi_total = 0  # open-ended; kept only for legacy references
        self.multi_current = 1
        self.multi_results = []

        # Start with the whole clip selected so the END marker sits at the very
        # end of the audio; the user drags the markers inward to define a cut.
        self.trim_start = 0
        self.trim_end = len(self.waveform_data)

        # Hide the normal Save / Multi-Cut buttons; show Capture + Done + banner.
        self._save_btn.pack_forget()
        self._multi_btn.pack_forget()
        self._done_btn.pack(side=tk.RIGHT)
        self._capture_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._multi_banner.pack(fill=tk.X, pady=(0, 10), before=self._capture_btn.master)

        self._update_multi_banner()
        self._draw_waveform()

    def _update_multi_banner(self):
        """Refresh the multi-cut banner + capture/done button labels."""
        if not self.multi_mode:
            return
        captured = len(self.multi_results)
        self._multi_banner_label.config(
            text=(
                f"📑 Multi-Cut: {captured} captured  "
                f"— left-click sets START, right-click sets END, "
                f"SPACE previews, ✓ Capture grabs it. Click Done when finished."
            )
        )
        self._capture_btn.configure(text=f"✓ Capture (#{captured + 1})")
        try:
            self._done_btn.configure(
                text=("✓ Done" if captured == 0 else f"✓ Done ({captured})")
            )
        except (AttributeError, tk.TclError):
            pass

    _NO_PERSON = "— none —"

    def _prompt_multi_cut_title(self):
        """Prompt for a cut's title (and person, when person tagging is enabled).

        Returns a ``(title, person_or_None)`` tuple; ``(None, None)`` if cancelled.
        """
        prompt = tk.Toplevel(self.dialog)
        prompt.title("Cut Title")
        prompt.configure(bg=COLORS["bg_dark"])
        prompt.transient(self.dialog)
        prompt.grab_set()
        prompt.resizable(False, False)

        result: list[Optional[str]] = [None]
        person_result: list[Optional[str]] = [None]
        cut_no = len(self.multi_results) + 1
        default_title = f"{Path(self.file_path).stem} {cut_no}"
        title_var = tk.StringVar(value=default_title)

        tk.Label(
            prompt,
            text=f"Title for cut {cut_no}:",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=self._font(FONTS["size_sm"], True),
            padx=20,
            pady=10,
        ).pack(anchor="w")

        entry = tk.Entry(
            prompt,
            textvariable=title_var,
            width=34,
            font=self._font(12),
            bg=COLORS["bg_medium"],
            fg="white",
            insertbackground="white",
            relief="flat",
        )
        entry.pack(fill=tk.X, padx=20, pady=(0, 12))

        # Per-cut person tagging (only when the caller is editing a recording).
        person_var = tk.StringVar(value=self._NO_PERSON)
        if self.person_names:
            tk.Label(
                prompt, text="Assign to person:", bg=COLORS["bg_dark"],
                fg=COLORS["text_primary"], font=self._font(FONTS["size_sm"], True),
                padx=20,
            ).pack(anchor="w")
            options = [self._NO_PERSON] + list(self.person_names)
            om = tk.OptionMenu(prompt, person_var, *options)
            om.configure(bg=COLORS["bg_medium"], fg="white", activebackground=COLORS["bg_light"],
                         activeforeground="white", relief="flat", highlightthickness=0,
                         font=self._font(FONTS["size_sm"]))
            om["menu"].configure(bg=COLORS["bg_medium"], fg="white")
            om.pack(fill=tk.X, padx=20, pady=(0, 12))

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
            chosen = person_var.get()
            person_result[0] = None if chosen == self._NO_PERSON else chosen
            prompt.destroy()

        tk.Button(
            btn_row,
            text="Cancel",
            command=close,
            bg=COLORS["bg_medium"],
            fg="white",
            font=self._font(FONTS["size_xs"]),
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
            font=self._font(FONTS["size_xs"], True),
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
        return result[0], person_result[0]

    def _capture_multi_segment(self):
        """Capture the current selection as another multi-cut segment.

        Open-ended: this never finishes the session \u2014 the user keeps capturing
        and clicks Done when they're satisfied.
        """
        if not self.multi_mode or self.audio_data is None:
            return

        if self.trim_end <= self.trim_start:
            messagebox.showwarning(
                "Multi-Cut",
                "The selection is empty. Set start (left-click) and end (right-click) first.",
                parent=self.dialog,
            )
            return

        title, person = self._prompt_multi_cut_title()
        if title is None:
            return

        # Stop any preview before slicing.
        self._stop_playback()

        segment = np.ascontiguousarray(
            self.audio_data[self.trim_start : self.trim_end].copy()
        )
        self.multi_results.append((segment, self.sample_rate, title, person))

        # Reset to the whole clip so the END marker is back at the end of the
        # audio, ready for the next cut.
        self.trim_start = 0
        self.trim_end = len(self.waveform_data)
        self._update_multi_banner()
        self._draw_waveform()

    def _finish_multi_cut(self):
        """Finish open-ended multi-cut: turn every captured segment into a sound."""
        self._stop_playback()

        if not self.multi_results:
            # Nothing captured \u2014 treat Done as a cancel of the cut session.
            self.result = None
            self.dialog.destroy()
            return

        # The FIRST cut becomes this editor's single result (so the normal
        # caller flow handles it); the rest are returned via multi_results.
        first_audio, first_sr = self.multi_results[0][:2]
        self.result = (first_audio, first_sr)
        if self.on_save:
            try:
                self.on_save(first_audio, first_sr)
            except Exception:
                pass
        self.dialog.destroy()

    def _on_save(self):
        """Save the trimmed audio and close."""
        self._stop_playback()

        if self.audio_data is None:
            self.result = None
            self.dialog.destroy()
            return

        # When editing a recording, let a single Save also tag a person.
        if self.person_names:
            title, person = self._prompt_multi_cut_title()
            if title is None:
                return  # cancelled the save
            self.result_title = title
            self.result_person = person

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


class LongAudioPicker:
    """Coarse section picker for very long recordings.

    Shows a lightweight overview waveform of the WHOLE file (decoded at a tiny
    sample rate — a few hundred KB even for hours of audio) and lets the user
    drag a window over the part they want. Only that window is then decoded at
    full quality, so a multi-hour recording never has to be held in RAM. The
    decoded section is handed to the normal SoundEditor for precise trimming.

    Returns ``(audio, sample_rate)`` from ``show()`` (None if cancelled).
    """

    # Pull up to a 45-minute section out of a long recording in one go. The
    # selected window is decoded at full quality (≈ 1 GB for the full 45 min at
    # 48 kHz stereo) and handed to the editor for multi-cutting.
    MAX_WINDOW_SECONDS = 45 * 60.0
    # Quick-length presets, in seconds. Long recordings (calls, streams) need
    # minute-scale chunks, so these are minutes — plus a couple of short ones.
    QUICK_LENGTHS = (30, 60, 5 * 60, 15 * 60, 30 * 60, 45 * 60)
    PREVIEW_CAP_SECONDS = 20.0  # preview only the first chunk so it stays snappy

    def _font(self, px: int, bold: bool = False) -> tuple:
        """Raw-Tk font in DEVICE px (see _tk_font)."""
        return _tk_font(self._s, px, bold)

    def __init__(
        self,
        parent: tk.Tk,
        file_path: str,
        duration: float,
        peaks: np.ndarray,
        low_sr: int,
        sample_rate: int = AUDIO["sample_rate"],
        output_device: Optional[int] = None,
    ):
        self.parent = parent
        # DPI factor (CTk's) — raw-Tk fonts below are sized in device px.
        self._s = _window_scaling(parent)
        if not (0.4 <= self._s <= 8.0):
            self._s = 1.0
        self.file_path = file_path
        self.duration = max(0.01, float(duration))
        self.peaks = peaks if peaks is not None else np.zeros(1, dtype=np.float32)
        self.low_sr = max(1, int(low_sr))
        self.sample_rate = sample_rate
        self.output_device = output_device

        self.sel_start = 0.0
        # Default to a 5-minute window (long recordings want minute-scale chunks).
        self.sel_end = float(min(self.MAX_WINDOW_SECONDS, self.duration, 5 * 60.0))
        self.dragging: Optional[str] = None  # "start" | "end" | "move"
        self._move_anchor = 0.0  # selection-relative grab offset for "move"
        self.result: Optional[Tuple[np.ndarray, int]] = None

        self.canvas_width = 860
        self.canvas_height = 170
        self._col_cache: Optional[np.ndarray] = None  # per-pixel peak heights

        m = float(np.abs(self.peaks).max()) if len(self.peaks) else 0.0
        self._gain = (1.0 / m) if m > 1e-4 else 1.0

        self._preview_playing = False

        self._create_dialog()

    # ------------------------------------------------------------------ utils
    def _t2x(self, t: float) -> int:
        return int(t / self.duration * self.canvas_width)

    def _x2t(self, x: float) -> float:
        return max(0.0, min(self.duration, x / max(1, self.canvas_width) * self.duration))

    @staticmethod
    def _fmt(t: float) -> str:
        t = max(0, int(round(t)))
        h, m, s = t // 3600, (t % 3600) // 60, t % 60
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # ----------------------------------------------------------------- dialog
    def _create_dialog(self):
        self.dialog = tk.Toplevel(self.parent)
        self.dialog.title(f"Pick a section — {Path(self.file_path).name}")
        self.dialog.configure(bg=COLORS["bg_dark"])
        self.dialog.transient(self.parent)
        self.dialog.grab_set()
        self.dialog.minsize(720, 360)
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.dialog.bind("<space>", self._on_space_key)
        self.dialog.bind("<Escape>", lambda _e: self._on_cancel())

        w, h = 920, 430
        self.dialog.geometry(f"{w}x{h}")
        # Center over the app window (keeps it on the app's monitor).
        self.dialog.update_idletasks()
        px, py = self.parent.winfo_rootx(), self.parent.winfo_rooty()
        pw, ph = self.parent.winfo_width(), self.parent.winfo_height()
        self.dialog.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 3}")

        main = tk.Frame(self.dialog, bg=COLORS["bg_dark"], padx=18, pady=14)
        main.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            main,
            text=f"🎬 This recording is {self._fmt(self.duration)} long.",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=self._font(FONTS["size_md"], True),
        ).pack(anchor="w")
        tk.Label(
            main,
            text="Drag across the overview to pick the part you want, then load it for fine trimming.",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_muted"],
            font=self._font(9),
        ).pack(anchor="w", pady=(0, 10))

        canvas_box = tk.Frame(main, bg=COLORS["bg_light"], padx=2, pady=2)
        canvas_box.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(
            canvas_box,
            width=self.canvas_width,
            height=self.canvas_height,
            bg="#12141a",
            highlightthickness=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        # Quick-length row
        ql = tk.Frame(main, bg=COLORS["bg_dark"])
        ql.pack(fill=tk.X, pady=(10, 4))
        tk.Label(
            ql, text="Length:", bg=COLORS["bg_dark"], fg=COLORS["text_primary"],
            font=self._font(FONTS["size_xs"]),
        ).pack(side=tk.LEFT, padx=(0, 6))
        for secs in self.QUICK_LENGTHS:
            if secs > self.duration:
                continue
            label = f"{secs}s" if secs < 60 else f"{secs // 60}m"
            tk.Button(
                ql, text=label, command=lambda s=secs: self._set_length(s),
                bg=COLORS["bg_medium"], fg="white", activebackground=COLORS["bg_light"],
                activeforeground="white", font=self._font(9), width=5,
                relief="flat", cursor="hand2",
            ).pack(side=tk.LEFT, padx=3)

        self.play_btn = tk.Button(
            ql, text="▶ Preview", command=self._toggle_preview,
            bg=COLORS["green"], fg="white", activebackground="#1E8E4D",
            activeforeground="white", font=self._font(9, True), width=10,
            relief="flat", cursor="hand2",
        )
        self.play_btn.pack(side=tk.RIGHT)

        self.sel_label = tk.Label(
            main, text="", bg=COLORS["bg_dark"], fg=COLORS["text_primary"],
            font=self._font(FONTS["size_sm"], True),
        )
        self.sel_label.pack(anchor="w", pady=(6, 0))

        # Footer actions
        sep = tk.Frame(main, bg=COLORS["bg_light"], height=1)
        sep.pack(fill=tk.X, pady=(12, 10))
        footer = tk.Frame(main, bg=COLORS["bg_dark"])
        footer.pack(fill=tk.X)
        # Style-spec footer: secondary Cancel, primary (green) Load section.
        fh, fr = UI["footer_button_height"], UI["button_corner_radius"]
        ctk.CTkButton(
            footer, text="Cancel", command=self._on_cancel, width=100,
            fg_color=COLORS["bg_light"], hover_color=COLORS["bg_lighter"],
            text_color="white", height=fh, corner_radius=fr,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
        ).pack(side=tk.RIGHT, padx=(8, 0))
        ctk.CTkButton(
            footer, text="✓ Load section", command=self._on_load, width=150,
            fg_color=COLORS["green"], hover_color=COLORS["green_hover"],
            text_color="white", height=fh, corner_radius=fr,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
        ).pack(side=tk.RIGHT)

        self._update_labels()
        self._draw()

    # ------------------------------------------------------------------ draw
    def _build_columns(self):
        """Per-pixel peak heights for the overview (recomputed on resize)."""
        w = self.canvas_width
        n = len(self.peaks)
        cols = np.zeros(w, dtype=np.float32)
        if n > 0:
            # Map each pixel column to a slice of the low-rate peak array.
            edges = (np.linspace(0, n, w + 1)).astype(np.int64)
            for i in range(w):
                a, b = edges[i], edges[i + 1]
                if b > a:
                    cols[i] = np.abs(self.peaks[a:b]).max()
        self._col_cache = cols

    def _draw(self):
        c = self.canvas
        c.delete("all")
        self.canvas_width = c.winfo_width() or self.canvas_width
        self.canvas_height = c.winfo_height() or self.canvas_height
        if self._col_cache is None or len(self._col_cache) != self.canvas_width:
            self._build_columns()

        h = self.canvas_height
        mid = h // 2
        x0, x1 = self._t2x(self.sel_start), self._t2x(self.sel_end)

        # selection band (drawn first, under the waveform)
        c.create_rectangle(x0, 0, x1, h, fill="#26304d", outline="", width=0)

        # waveform
        gain = self._gain * (mid - 8)
        cols = self._col_cache
        for x in range(min(self.canvas_width, len(cols))):
            bar = int(cols[x] * gain)
            if bar <= 0:
                continue
            in_sel = x0 <= x <= x1
            c.create_line(x, mid - bar, x, mid + bar,
                          fill=(COLORS["blurple"] if in_sel else "#454a52"), width=1)

        c.create_line(0, mid, self.canvas_width, mid, fill="#3a3d41", width=1)

        # selection handles
        for x, col in ((x0, COLORS["green"]), (x1, COLORS["red"])):
            c.create_line(x, 0, x, h, fill=col, width=2)
            c.create_polygon(x - 7, 0, x + 7, 0, x, 13, fill=col)

        # time ticks every ~1/8 of the file
        for k in range(1, 8):
            t = self.duration * k / 8
            x = self._t2x(t)
            c.create_line(x, h - 14, x, h, fill="#2a2d31", width=1)
            c.create_text(x, h - 7, text=self._fmt(t), fill=COLORS["text_muted"],
                          font=self._font(7), anchor="s")

    def _on_canvas_resize(self, event):
        # Debounced like the main editor canvas: _build_columns slices numpy
        # per pixel column, so per-pixel redraws during a live dialog resize
        # stutter badly.
        self.canvas_width = event.width
        self.canvas_height = event.height
        self._col_cache = None
        aid = getattr(self, "_resize_redraw_after", None)
        if aid is not None:
            try:
                self.canvas.after_cancel(aid)
            except Exception:
                pass
        self._resize_redraw_after = self.canvas.after(80, self._redraw_after_resize)

    def _redraw_after_resize(self):
        self._resize_redraw_after = None
        try:
            if not self.canvas.winfo_exists():
                return
        except Exception:
            return
        self._draw()

    # ------------------------------------------------------------- selection
    def _clamp_selection(self):
        self.sel_start = max(0.0, min(self.sel_start, self.duration - 0.05))
        self.sel_end = max(self.sel_start + 0.05, min(self.sel_end, self.duration))
        if self.sel_end - self.sel_start > self.MAX_WINDOW_SECONDS:
            self.sel_end = self.sel_start + self.MAX_WINDOW_SECONDS

    def _set_length(self, secs: float):
        secs = min(secs, self.MAX_WINDOW_SECONDS, self.duration)
        self.sel_end = self.sel_start + secs
        if self.sel_end > self.duration:
            self.sel_end = self.duration
            self.sel_start = max(0.0, self.sel_end - secs)
        self._clamp_selection()
        self._update_labels()
        self._draw()

    def _on_press(self, event):
        x0, x1 = self._t2x(self.sel_start), self._t2x(self.sel_end)
        if abs(event.x - x0) <= 8:
            self.dragging = "start"
        elif abs(event.x - x1) <= 8:
            self.dragging = "end"
        elif x0 < event.x < x1:
            self.dragging = "move"
            self._move_anchor = self._x2t(event.x) - self.sel_start
        else:
            # Start a fresh selection at the click point.
            self.dragging = "end"
            self.sel_start = self._x2t(event.x)
            self.sel_end = self.sel_start + 0.05
        self._on_drag(event)

    def _on_drag(self, event):
        if not self.dragging:
            return
        t = self._x2t(event.x)
        if self.dragging == "start":
            self.sel_start = min(t, self.sel_end - 0.05)
        elif self.dragging == "end":
            self.sel_end = max(t, self.sel_start + 0.05)
        elif self.dragging == "move":
            length = self.sel_end - self.sel_start
            self.sel_start = t - self._move_anchor
            self.sel_end = self.sel_start + length
        self._clamp_selection()
        self._update_labels()
        self._draw()

    def _on_release(self, event):
        self.dragging = None

    def _update_labels(self):
        length = self.sel_end - self.sel_start
        self.sel_label.config(
            text=f"📍 {self._fmt(self.sel_start)} → {self._fmt(self.sel_end)}   "
                 f"({length:.1f}s selected)"
        )

    # -------------------------------------------------------------- preview
    def _on_space_key(self, event):
        self._toggle_preview()
        return "break"

    def _toggle_preview(self):
        if self._preview_playing:
            self._stop_preview()
            return
        try:
            # Preview only the first chunk from the start handle so a long
            # selection (up to 45 min) still previews instantly.
            length = min(self.sel_end - self.sel_start, self.PREVIEW_CAP_SECONDS)
            audio, sr = decode_audio_range(
                self.file_path, self.sel_start, length, self.sample_rate, channels=2
            )
            if len(audio) == 0:
                return
            sd.play(audio, sr, device=self.output_device)
            self._preview_playing = True
            self.play_btn.config(text="⏹ Stop", bg=COLORS["red"])
            # Auto-reset the button when playback finishes.
            ms = int(min(length, len(audio) / sr) * 1000) + 150
            self.dialog.after(ms, self._reset_preview_btn)
        except Exception as e:
            messagebox.showerror("Preview", f"Could not preview:\n{e}", parent=self.dialog)

    def _stop_preview(self):
        try:
            sd.stop()
        except Exception:
            pass
        self._reset_preview_btn()

    def _reset_preview_btn(self):
        self._preview_playing = False
        try:
            if self.play_btn.winfo_exists():
                self.play_btn.config(text="▶ Preview", bg=COLORS["green"])
        except Exception:
            pass

    # ---------------------------------------------------------------- result
    def _on_load(self):
        self._stop_preview()
        try:
            length = self.sel_end - self.sel_start
            audio, sr = decode_audio_range(
                self.file_path, self.sel_start, length, self.sample_rate, channels=2
            )
            if len(audio) == 0:
                messagebox.showwarning(
                    "Empty selection",
                    "That section decoded to no audio. Pick a different part.",
                    parent=self.dialog,
                )
                return
            self.result = (audio, sr)
        except Exception as e:
            messagebox.showerror("Load failed", f"Could not load section:\n{e}", parent=self.dialog)
            return
        self.dialog.destroy()

    def _on_cancel(self):
        self._stop_preview()
        self.result = None
        self.dialog.destroy()

    def show(self) -> Optional[Tuple[np.ndarray, int]]:
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
