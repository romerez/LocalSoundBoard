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

from .audio import read_audio_file
from .constants import AUDIO, COLORS

# Try to get ffmpeg path from imageio-ffmpeg (bundled ffmpeg)
try:
    import imageio_ffmpeg  # type: ignore[import-untyped]

    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_PATH = None

# Try to import pydub for extended format support (M4A, AAC, WMA, etc.)
try:
    from pydub import AudioSegment

    # Configure pydub to use ffmpeg from imageio-ffmpeg if available
    if FFMPEG_PATH:
        AudioSegment.converter = FFMPEG_PATH
        AudioSegment.ffmpeg = FFMPEG_PATH
        AudioSegment.ffprobe = FFMPEG_PATH.replace("ffmpeg", "ffprobe")  # type: ignore[attr-defined]
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False


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
        self.play_lock = threading.Lock()
        self.selected_audio: Optional[np.ndarray] = None  # Prepared audio for playback

        # Canvas state
        self.canvas_width: int = 700
        self.canvas_height: int = 200
        self.dragging: Optional[str] = None  # "start", "end", or None

        # Result
        self.result: Optional[Tuple[np.ndarray, int]] = None

        self._load_audio()
        self._create_dialog()

    def _load_audio(self):
        """Load and prepare audio data."""
        try:
            data, sr = self._read_audio_file(self.file_path)

            # Resample to target sample rate if needed
            if sr != self.sample_rate:
                ratio = self.sample_rate / sr
                new_length = int(len(data) * ratio)
                indices = np.linspace(0, len(data) - 1, new_length).astype(int)
                data = data[indices]

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
        self.dialog.geometry("800x500")
        self.dialog.configure(bg=COLORS["bg_dark"])
        self.dialog.transient(self.parent)
        self.dialog.grab_set()

        # Make dialog modal
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_cancel)

        main_frame = ttk.Frame(self.dialog, padding=15)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Duration warning
        if self.duration > self.MAX_DURATION_WARNING:
            warning_frame = tk.Frame(main_frame, bg=COLORS["red"], padx=10, pady=5)
            warning_frame.pack(fill=tk.X, pady=(0, 10))
            tk.Label(
                warning_frame,
                text=f"⚠ Warning: This sound is {self.duration:.1f}s long (recommended: ≤{self.MAX_DURATION_WARNING}s). Consider trimming it.",
                bg=COLORS["red"],
                fg="white",
                font=("Segoe UI", 9, "bold"),
            ).pack()

        # Info bar
        self._create_info_bar(main_frame)

        # Waveform canvas
        self._create_waveform_canvas(main_frame)

        # Timeline
        self._create_timeline(main_frame)

        # Zoom and navigation controls
        self._create_zoom_controls(main_frame)

        # Playback controls
        self._create_playback_controls(main_frame)

        # Trim info and buttons
        self._create_action_buttons(main_frame)

        # Initial draw
        self._draw_waveform()

    def _create_info_bar(self, parent):
        """Create the info bar showing duration and selection."""
        info_frame = ttk.Frame(parent)
        info_frame.pack(fill=tk.X, pady=(0, 10))

        self.info_label = tk.Label(
            info_frame,
            text=f"Total: {self.duration:.2f}s | Selected: {self.duration:.2f}s",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=("Segoe UI", 10),
        )
        self.info_label.pack(side=tk.LEFT)

        self.trim_info_label = tk.Label(
            info_frame,
            text="Drag the green/red markers to trim",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_muted"],
            font=("Segoe UI", 9),
        )
        self.trim_info_label.pack(side=tk.RIGHT)

    def _create_waveform_canvas(self, parent):
        """Create the waveform visualization canvas."""
        canvas_frame = tk.Frame(parent, bg=COLORS["bg_medium"], padx=2, pady=2)
        canvas_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.canvas = tk.Canvas(
            canvas_frame,
            width=self.canvas_width,
            height=self.canvas_height,
            bg="#1a1d21",
            highlightthickness=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Bind mouse events
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)

    def _create_timeline(self, parent):
        """Create the timeline below the waveform."""
        self.timeline_canvas = tk.Canvas(
            parent,
            height=25,
            bg=COLORS["bg_dark"],
            highlightthickness=0,
        )
        self.timeline_canvas.pack(fill=tk.X, pady=(0, 5))

    def _create_zoom_controls(self, parent):
        """Create zoom and navigation controls."""
        zoom_frame = ttk.Frame(parent)
        zoom_frame.pack(fill=tk.X, pady=5)

        # Zoom buttons
        tk.Label(
            zoom_frame,
            text="Zoom:",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
        ).pack(side=tk.LEFT, padx=(0, 5))

        tk.Button(
            zoom_frame,
            text="−",
            command=self._zoom_out,
            bg=COLORS["bg_medium"],
            fg="white",
            width=3,
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT, padx=2)

        self.zoom_label = tk.Label(
            zoom_frame,
            text="1.0x",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            width=6,
        )
        self.zoom_label.pack(side=tk.LEFT, padx=5)

        tk.Button(
            zoom_frame,
            text="+",
            command=self._zoom_in,
            bg=COLORS["bg_medium"],
            fg="white",
            width=3,
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT, padx=2)

        tk.Button(
            zoom_frame,
            text="Fit All",
            command=self._zoom_fit,
            bg=COLORS["bg_medium"],
            fg="white",
            width=8,
        ).pack(side=tk.LEFT, padx=10)

        # Horizontal scrollbar for navigation when zoomed
        self.h_scroll = ttk.Scale(
            zoom_frame,
            from_=0,
            to=1,
            orient=tk.HORIZONTAL,
            command=self._on_scroll,
            length=200,
        )
        self.h_scroll.pack(side=tk.RIGHT, padx=10)
        self.h_scroll.set(0)

        tk.Label(
            zoom_frame,
            text="Navigate:",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
        ).pack(side=tk.RIGHT)

    def _create_playback_controls(self, parent):
        """Create playback preview controls."""
        play_frame = ttk.Frame(parent)
        play_frame.pack(fill=tk.X, pady=10)

        self.play_btn = tk.Button(
            play_frame,
            text="▶ Play Selection",
            command=self._toggle_playback,
            bg=COLORS["green"],
            fg="white",
            font=("Segoe UI", 10, "bold"),
            width=15,
        )
        self.play_btn.pack(side=tk.LEFT, padx=5)

        tk.Button(
            play_frame,
            text="⏹ Stop",
            command=self._stop_playback,
            bg=COLORS["red"],
            fg="white",
            width=8,
        ).pack(side=tk.LEFT, padx=5)

        # Selection time display
        self.selection_label = tk.Label(
            play_frame,
            text="Selection: 0.00s - 0.00s",
            bg=COLORS["bg_dark"],
            fg=COLORS["text_primary"],
            font=("Segoe UI", 10),
        )
        self.selection_label.pack(side=tk.LEFT, padx=20)

    def _create_action_buttons(self, parent):
        """Create the save/cancel action buttons."""
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X, pady=(10, 0))

        # Reset button
        tk.Button(
            btn_frame,
            text="Reset Selection",
            command=self._reset_selection,
            bg=COLORS["bg_medium"],
            fg="white",
            width=15,
        ).pack(side=tk.LEFT, padx=5)

        # Action buttons on right
        tk.Button(
            btn_frame,
            text="Cancel",
            command=self._on_cancel,
            bg=COLORS["bg_medium"],
            fg="white",
            width=10,
        ).pack(side=tk.RIGHT, padx=5)

        tk.Button(
            btn_frame,
            text="Save & Use",
            command=self._on_save,
            bg=COLORS["blurple"],
            fg="white",
            font=("Segoe UI", 10, "bold"),
            width=12,
        ).pack(side=tk.RIGHT, padx=5)

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
                text="START",
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
                text="END",
                fill=COLORS["red"],
                font=("Segoe UI", 8, "bold"),
                anchor="s",
            )

    def _draw_playback_position(self, view_start: int, view_end: int):
        """Draw the current playback position indicator."""
        with self.play_lock:
            pos = self.play_position + self.trim_start

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
            text=f"Total: {total_duration:.2f}s | Selected: {selected_duration:.2f}s"
        )

        # selection_label may not exist during initial UI construction
        if hasattr(self, "selection_label"):
            start_time = self.trim_start / self.sample_rate
            end_time = self.trim_end / self.sample_rate
            self.selection_label.config(text=f"Selection: {start_time:.2f}s - {end_time:.2f}s")

    def _on_canvas_click(self, event):
        """Handle click on the canvas to select trim markers."""
        # Calculate which marker is closest to click
        total_samples = len(self.waveform_data)
        visible_samples = int(total_samples / self.zoom_level)
        view_start = int(self.view_start * (total_samples - visible_samples))
        view_end = view_start + visible_samples
        view_range = view_end - view_start

        # Get click position in samples
        click_sample = view_start + int(event.x / self.canvas_width * view_range)

        # Check distance to markers
        start_x = (self.trim_start - view_start) / view_range * self.canvas_width
        end_x = (self.trim_end - view_start) / view_range * self.canvas_width

        # Threshold for selecting a marker
        threshold = 15

        if abs(event.x - start_x) < threshold:
            self.dragging = "start"
        elif abs(event.x - end_x) < threshold:
            self.dragging = "end"
        else:
            # Click anywhere else to set nearest marker
            if abs(click_sample - self.trim_start) < abs(click_sample - self.trim_end):
                self.dragging = "start"
            else:
                self.dragging = "end"
            self._update_marker_position(event.x)

    def _on_canvas_drag(self, event):
        """Handle dragging on the canvas."""
        if self.dragging:
            self._update_marker_position(event.x)

    def _on_canvas_release(self, event):
        """Handle mouse release."""
        self.dragging = None

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
        """Handle mouse wheel for zooming."""
        if event.delta > 0:
            self._zoom_in()
        else:
            self._zoom_out()

    def _zoom_in(self):
        """Zoom in on the waveform."""
        self.zoom_level = min(self.zoom_level * 1.5, 50.0)
        self.zoom_label.config(text=f"{self.zoom_level:.1f}x")
        self._update_scroll_range()
        self._draw_waveform()

    def _zoom_out(self):
        """Zoom out on the waveform."""
        self.zoom_level = max(self.zoom_level / 1.5, 1.0)
        self.zoom_label.config(text=f"{self.zoom_level:.1f}x")
        self._update_scroll_range()
        self._draw_waveform()

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

    def _pause_playback(self):
        """Pause playback (keep position for resume)."""
        self.is_playing = False
        self.is_paused = True
        if self.play_stream:
            try:
                self.play_stream.abort()  # Use abort for immediate stop
                self.play_stream.close()
            except Exception:
                pass
            self.play_stream = None
        self.play_btn.config(text="▶ Resume", bg=COLORS["green"])

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
            self.dialog.after(
                0, lambda: self.play_btn.config(text="▶ Play Selection", bg=COLORS["green"])
            )
            self.dialog.after(0, self._draw_waveform)

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
        """Update the playhead position during playback."""
        if self.is_playing:
            self._draw_waveform()
            self.dialog.after(50, self._update_playhead)

    def _stop_playback(self):
        """Stop playback and reset position."""
        self.is_playing = False
        self.is_paused = False
        self.play_position = 0
        if self.play_stream:
            try:
                self.play_stream.abort()  # Use abort for immediate stop
                self.play_stream.close()
            except Exception:
                pass
            self.play_stream = None
        self.play_btn.config(text="▶ Play Selection", bg=COLORS["green"])
        self._draw_waveform()

    def _reset_selection(self):
        """Reset trim selection to full audio."""
        self.trim_start = 0
        self.trim_end = len(self.waveform_data)
        self._draw_waveform()

    def _on_save(self):
        """Save the trimmed audio and close."""
        self._stop_playback()

        if self.audio_data is None:
            self.result = None
            self.dialog.destroy()
            return

        # Get trimmed audio data
        if self.audio_data.ndim > 1:
            trimmed_audio = self.audio_data[self.trim_start : self.trim_end]
        else:
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
