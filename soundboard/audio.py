"""
Audio mixing and playback for the Discord Soundboard.
"""

import ctypes
import hashlib
import io
import os
import queue
import shutil
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from typing import Optional, Dict, List, Tuple

from .constants import AUDIO, SOUNDS_DIR


# Windows API for mouse button simulation
def _simulate_mouse_button(button: str, press: bool = True):
    """
    Simulate mouse button press/release using Windows API.
    Works reliably for side buttons (XBUTTON1/XBUTTON2).
    """
    # Constants from Windows API
    MOUSEEVENTF_XDOWN = 0x0080
    MOUSEEVENTF_XUP = 0x0100
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    XBUTTON1 = 0x0001
    XBUTTON2 = 0x0002

    if button == "left":
        flags = MOUSEEVENTF_LEFTDOWN if press else MOUSEEVENTF_LEFTUP
        ctypes.windll.user32.mouse_event(flags, 0, 0, 0, 0)
    elif button == "right":
        flags = MOUSEEVENTF_RIGHTDOWN if press else MOUSEEVENTF_RIGHTUP
        ctypes.windll.user32.mouse_event(flags, 0, 0, 0, 0)
    elif button == "middle":
        flags = MOUSEEVENTF_MIDDLEDOWN if press else MOUSEEVENTF_MIDDLEUP
        ctypes.windll.user32.mouse_event(flags, 0, 0, 0, 0)
    elif button == "x" or button == "x1":
        # XBUTTON1 = mouse4 in some apps
        flags = MOUSEEVENTF_XDOWN if press else MOUSEEVENTF_XUP
        ctypes.windll.user32.mouse_event(flags, 0, 0, XBUTTON1, 0)
    elif button == "x2":
        # XBUTTON2 = mouse5 in some apps
        flags = MOUSEEVENTF_XDOWN if press else MOUSEEVENTF_XUP
        ctypes.windll.user32.mouse_event(flags, 0, 0, XBUTTON2, 0)


# Try to get ffmpeg path from imageio-ffmpeg (bundled ffmpeg)
try:
    import imageio_ffmpeg
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
        AudioSegment.ffprobe = FFMPEG_PATH.replace("ffmpeg", "ffprobe")
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False


class SoundCache:
    """
    Manages local sound storage and in-memory caching for optimal performance.

    - Copies sounds to a local folder for persistence
    - Pre-loads audio data into memory at the target sample rate
    - Provides O(1) lookup for cached audio
    """

    def __init__(self, sample_rate: Optional[int] = None):
        self.sample_rate = sample_rate or AUDIO["sample_rate"]
        self.sounds_dir = Path(SOUNDS_DIR)
        self._cache: Dict[str, np.ndarray] = {}  # filepath -> resampled audio data
        self._lock = threading.Lock()

        # Ensure sounds directory exists
        self.sounds_dir.mkdir(exist_ok=True)

    def add_sound(self, source_path: str) -> str:
        """
        Copy a sound file to the local sounds folder and cache it.

        Returns the new local path (relative to sounds folder).
        """
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        # Generate unique filename using hash to avoid conflicts
        file_hash = self._hash_file(source_path)[:8]
        dest_name = f"{source.stem}_{file_hash}{source.suffix}"
        dest_path = self.sounds_dir / dest_name

        # Copy if not already in sounds folder
        if not dest_path.exists():
            shutil.copy2(source_path, dest_path)

        # Pre-load into cache
        self._load_into_cache(str(dest_path))

        return str(dest_path)

    def add_sound_data(self, audio_data: np.ndarray, sample_rate: int, original_name: str) -> str:
        """
        Save trimmed/edited audio data to a new file and cache it.

        Args:
            audio_data: The numpy array of audio samples
            sample_rate: The sample rate of the audio
            original_name: The original filename (used for naming)

        Returns the new local path.
        """
        # Generate unique filename
        import time

        timestamp = str(int(time.time() * 1000))[-8:]
        stem = Path(original_name).stem
        dest_name = f"{stem}_{timestamp}.wav"
        dest_path = self.sounds_dir / dest_name

        # Save as WAV file
        sf.write(str(dest_path), audio_data, sample_rate)

        # Cache the audio data directly (already at correct sample rate)
        with self._lock:
            self._cache[str(dest_path)] = audio_data.copy()

        return str(dest_path)

    def _hash_file(self, file_path: str) -> str:
        """Generate a short hash for a file to create unique names."""
        hasher = hashlib.md5()
        with open(file_path, "rb") as f:
            # Read in chunks for large files
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _load_into_cache(self, file_path: str) -> np.ndarray:
        """Load and resample audio file, caching the result."""
        with self._lock:
            if file_path in self._cache:
                return self._cache[file_path]

        try:
            data, sr = self._read_audio_file(file_path)

            # Resample if needed (do this once, not on every play)
            if sr != self.sample_rate:
                ratio = self.sample_rate / sr
                new_length = int(len(data) * ratio)
                indices = np.linspace(0, len(data) - 1, new_length).astype(int)
                data = data[indices]

            with self._lock:
                self._cache[file_path] = data

            return data
        except Exception as e:
            print(f"Error loading sound into cache: {e}")
            raise

    def _read_audio_file(self, file_path: str) -> Tuple[np.ndarray, int]:
        """
        Read an audio file, using pydub as fallback for formats
        that soundfile doesn't support (M4A, AAC, WMA, etc.).
        """
        ext = Path(file_path).suffix.lower()

        # Formats that soundfile usually handles well (but may fail on some files)
        soundfile_formats = {".wav", ".flac", ".ogg", ".aiff", ".aif"}

        # Try soundfile first for known supported formats
        if ext in soundfile_formats:
            try:
                data, sr = sf.read(file_path, dtype="float32")
                return data, sr
            except Exception:
                # Fall through to pydub fallback
                pass

        # Try soundfile for other formats (MP3 support varies)
        try:
            data, sr = sf.read(file_path, dtype="float32")
            return data, sr
        except Exception:
            pass

        # Fallback to pydub for OGG, M4A, AAC, WMA, WebM, etc.
        if PYDUB_AVAILABLE:
            try:
                audio = AudioSegment.from_file(file_path)

                # Convert to mono or keep stereo
                channels = audio.channels
                sr = audio.frame_rate

                # Get raw samples as numpy array
                samples = np.array(audio.get_array_of_samples(), dtype=np.float32)

                # Normalize to [-1.0, 1.0] range
                max_val = float(2 ** (audio.sample_width * 8 - 1))
                samples = samples / max_val

                # Reshape for stereo
                if channels == 2:
                    samples = samples.reshape((-1, 2))

                return samples, sr
            except Exception as e:
                raise RuntimeError(
                    f"Failed to load audio file '{file_path}'. " f"Format may require ffmpeg: {e}"
                )
        else:
            raise RuntimeError(
                f"Cannot load '{ext}' files. Install pydub and ffmpeg for extended format support: "
                f"pip install pydub"
            )
            raise

    def get_sound_data(self, file_path: str) -> Optional[np.ndarray]:
        """
        Get pre-loaded audio data for a sound file.

        Returns cached data if available, otherwise loads and caches it.
        """
        # Get cached data reference under lock, but copy OUTSIDE lock
        # This reduces lock contention when clicking rapidly
        cached_data = None
        with self._lock:
            if file_path in self._cache:
                cached_data = self._cache[file_path]

        if cached_data is not None:
            return cached_data.copy()

        # Not in cache, try to load
        try:
            return self._load_into_cache(file_path).copy()
        except Exception as e:
            print(f"Failed to load sound data for {file_path}: {e}")
            return None

    def preload_sounds(self, file_paths: List[str]):
        """Pre-load multiple sounds into cache (call on startup)."""
        for path in file_paths:
            if path and os.path.exists(path):
                try:
                    self._load_into_cache(path)
                except Exception as e:
                    print(f"Failed to preload {path}: {e}")

    def remove_sound(self, file_path: str, delete_file: bool = True):
        """Remove a sound from cache and optionally delete the file."""
        with self._lock:
            if file_path in self._cache:
                del self._cache[file_path]

        if delete_file:
            path = Path(file_path)
            if path.exists() and path.parent == self.sounds_dir:
                try:
                    path.unlink()
                except Exception as e:
                    print(f"Failed to delete sound file: {e}")

    def clear_cache(self):
        """Clear the in-memory cache (files remain on disk)."""
        with self._lock:
            self._cache.clear()

    def is_cached(self, file_path: str) -> bool:
        """Check if a sound is already in the cache."""
        with self._lock:
            return file_path in self._cache

    def get_sound_duration(self, file_path: str) -> float:
        """Get the duration of a sound in seconds (without copying data)."""
        with self._lock:
            if file_path in self._cache:
                return len(self._cache[file_path]) / self.sample_rate

        # Not cached - try to load it first
        data = self.get_sound_data(file_path)
        if data is not None:
            return len(data) / self.sample_rate
        return 0.0


class AudioMixer:
    """
    Handles real-time audio processing.

    Captures microphone input, mixes it with sound effects,
    and outputs to a virtual audio device.

    Uses separate input/output streams for better device compatibility.
    """

    def __init__(
        self,
        input_device: int,
        output_device: int,
        sample_rate: Optional[int] = None,
        block_size: Optional[int] = None,
        sound_cache: Optional[SoundCache] = None,
    ):
        self.input_device = input_device
        self.output_device = output_device
        self.sample_rate = sample_rate or AUDIO["sample_rate"]
        self.block_size = block_size or AUDIO["block_size"]
        self.channels = AUDIO["channels"]
        self.sound_cache = sound_cache

        self.running = False
        self.input_stream = None
        self.output_stream = None
        self.sound_queue: queue.Queue = queue.Queue()
        self.currently_playing: List[Dict] = []
        self.lock = threading.Lock()

        # Queue for mic input (handles timing mismatches between input/output)
        self._mic_queue: queue.Queue = queue.Queue(maxsize=8)
        # Fallback buffer when queue is empty (prevents choppy audio)
        self._last_mic_data = np.zeros((self.block_size,), dtype=np.float32)

        # Mic settings
        self.mic_volume = 1.0
        self.mic_muted = False

        # PTT (Push-to-Talk) settings
        self.ptt_key: Optional[str] = None
        self.ptt_active: bool = False
        self._ptt_lock = threading.Lock()
        # PTT release debounce: wait N callback cycles after last sound finishes
        # At 48kHz with 1024 block size, each cycle is ~21ms, so 5 cycles = ~100ms
        self._ptt_release_delay = 5  # Number of empty cycles before releasing PTT
        self._ptt_release_countdown = 0  # Current countdown (0 = not counting)

        # Local monitoring (play sounds to speakers too)
        self.monitor_enabled = False
        self.monitor_stream = None
        self._monitor_queue: queue.Queue = queue.Queue()  # Queue for monitor audio blocks

    def start(self):
        """Begin audio streams (separate input and output for compatibility)."""
        if self.running:
            return

        self.running = True

        # Create separate input stream for microphone
        self.input_stream = sd.InputStream(
            device=self.input_device,
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            channels=1,
            callback=self._input_callback,
            dtype=np.float32,
        )

        # Create separate output stream for virtual cable
        self.output_stream = sd.OutputStream(
            device=self.output_device,
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            channels=self.channels,
            callback=self._output_callback,
            dtype=np.float32,
        )

        self.input_stream.start()
        self.output_stream.start()

    def stop(self):
        """End audio streams."""
        self.running = False
        if self.input_stream:
            self.input_stream.stop()
            self.input_stream.close()
            self.input_stream = None
        if self.output_stream:
            self.output_stream.stop()
            self.output_stream.close()
            self.output_stream = None
        if self.monitor_stream:
            self.monitor_stream.stop()
            self.monitor_stream.close()
            self.monitor_stream = None

    def set_monitor_enabled(self, enabled: bool):
        """Enable or disable local speaker monitoring."""
        if enabled and not self.monitor_stream and self.running:
            # Clear the monitor queue before starting
            while not self._monitor_queue.empty():
                try:
                    self._monitor_queue.get_nowait()
                except queue.Empty:
                    break
            # Start monitor stream (outputs to default device)
            self.monitor_stream = sd.OutputStream(
                device=None,  # Default speakers
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                channels=self.channels,
                callback=self._monitor_callback,
                dtype=np.float32,
            )
            self.monitor_stream.start()
            self.monitor_enabled = True
        elif not enabled and self.monitor_stream:
            # Stop monitor stream
            self.monitor_stream.stop()
            self.monitor_stream.close()
            self.monitor_stream = None
            self.monitor_enabled = False

    def _monitor_callback(self, outdata, frames, time, status):
        """Output callback for local speaker monitoring (plays mixed audio)."""
        try:
            # Get audio data from queue (non-blocking)
            audio_block = self._monitor_queue.get_nowait()
            if len(audio_block) >= frames:
                outdata[:] = audio_block[:frames]
            else:
                outdata[: len(audio_block)] = audio_block
                outdata[len(audio_block) :] = 0
        except queue.Empty:
            # No audio data available, output silence
            outdata.fill(0)

    def _input_callback(self, indata, frames, time, status):
        """Capture microphone input into queue."""
        # Extract mono channel
        mic_data = indata[:, 0].copy()

        # Try to add to queue (non-blocking)
        try:
            self._mic_queue.put_nowait(mic_data)
        except queue.Full:
            # Queue full - discard oldest and add new (prevents falling behind)
            try:
                self._mic_queue.get_nowait()
                self._mic_queue.put_nowait(mic_data)
            except queue.Empty:
                pass

    def _output_callback(self, outdata, frames, time, status):
        """
        Real-time audio mixing callback for output stream.

        Called by sounddevice for each audio block.
        Keep this minimal - no blocking operations!
        """
        # Get microphone input from queue (handles timing variations)
        try:
            mic_data = self._mic_queue.get_nowait()
            # Only use if size matches, otherwise fall back
            if len(mic_data) == frames:
                self._last_mic_data = mic_data.copy()
            else:
                mic_data = self._last_mic_data
        except queue.Empty:
            # No new data - use last known data (prevents choppy audio)
            mic_data = self._last_mic_data

        # Ensure mic_data is valid (resize fallback if needed)
        if len(mic_data) != frames:
            # Resize the fallback buffer to match current frame size
            self._last_mic_data = np.zeros(frames, dtype=np.float32)
            mic_data = self._last_mic_data

        # Initialize sounds-only buffer for monitoring
        sounds_mix = np.zeros((frames, self.channels), dtype=np.float32)

        # Process microphone input
        if self.mic_muted:
            mixed = np.zeros((frames, self.channels), dtype=np.float32)
        else:
            mic_mono = mic_data * self.mic_volume
            mixed = np.column_stack([mic_mono, mic_mono])

        # Add newly queued sounds to currently playing
        while not self.sound_queue.empty():
            try:
                sound_data = self.sound_queue.get_nowait()
                with self.lock:
                    self.currently_playing.append(sound_data)
            except queue.Empty:
                break

        # Mix all currently playing sounds
        with self.lock:
            finished = []
            for i, sound in enumerate(self.currently_playing):
                pos = sound["position"]
                data = sound["data"]
                volume = sound["volume"]
                remaining = len(data) - pos

                if remaining <= 0:
                    finished.append(i)
                    continue

                chunk_size = min(frames, remaining)
                chunk = data[pos : pos + chunk_size] * volume

                # Convert mono to stereo if needed
                if chunk.ndim == 1:
                    chunk = np.column_stack([chunk, chunk])
                elif chunk.shape[1] == 1:
                    chunk = np.column_stack([chunk[:, 0], chunk[:, 0]])

                # Pad if chunk is smaller than frame size
                if chunk_size < frames:
                    padded = np.zeros((frames, self.channels), dtype=np.float32)
                    padded[:chunk_size] = chunk
                    chunk = padded

                # Add to both main mix and sounds-only mix
                mixed += chunk
                sounds_mix += chunk
                sound["position"] += chunk_size

            # Remove finished sounds
            for i in reversed(finished):
                self.currently_playing.pop(i)

            # Check if we should start PTT release countdown (all sounds finished)
            all_sounds_finished = len(self.currently_playing) == 0

        # Handle PTT release with debounce to prevent premature release
        if all_sounds_finished and self.sound_queue.empty():
            # No sounds playing - increment or start countdown
            if self.ptt_active:
                self._ptt_release_countdown += 1
                if self._ptt_release_countdown >= self._ptt_release_delay:
                    self._release_ptt()
                    self._ptt_release_countdown = 0
        else:
            # Sounds are playing - reset countdown
            self._ptt_release_countdown = 0

        # Clip to prevent distortion and write to output
        np.clip(mixed, -1.0, 1.0, out=outdata)

        # Queue sounds-only for local speaker monitoring
        if self.monitor_enabled and np.any(sounds_mix):
            # Only queue if there's actual sound data (not silence)
            clipped = np.clip(sounds_mix, -1.0, 1.0).astype(np.float32)
            try:
                self._monitor_queue.put_nowait(clipped.copy())
            except queue.Full:
                pass  # Drop frame if queue is full

    def play_sound(self, file_path: str, volume: float = 1.0) -> float:
        """Queue a sound for playback. Uses cache if available for better performance.

        Returns the duration of the sound in seconds (0.0 if failed to play).
        """
        try:
            # Reset PTT release countdown immediately when a new sound is triggered
            # This prevents PTT from releasing while we're loading/queueing
            self._ptt_release_countdown = 0

            # Use cached audio data if available (much faster - no disk I/O)
            if self.sound_cache:
                data = self.sound_cache.get_sound_data(file_path)
                if data is not None:
                    # Queue sound FIRST, then press PTT (prevents race condition where
                    # output callback sees empty queue and releases PTT immediately)
                    self.sound_queue.put({"data": data, "position": 0, "volume": volume})
                    self._press_ptt()
                    with open("debug.log", "a") as f:
                        f.write(f"[PLAY] Queued from cache: {len(data)} samples\n")
                    return len(data) / self.sample_rate

            # Fallback: load from disk (slower)
            if not os.path.exists(file_path):
                return 0.0

            data, sr = sf.read(file_path, dtype="float32")

            # Resample if needed
            if sr != self.sample_rate:
                ratio = self.sample_rate / sr
                new_length = int(len(data) * ratio)
                indices = np.linspace(0, len(data) - 1, new_length).astype(int)
                data = data[indices]

            with open("debug.log", "a") as f:
                f.write(f"[AUDIO] Playing from disk: {len(data)} samples\n")
            # Queue sound FIRST, then press PTT
            self.sound_queue.put({"data": data, "position": 0, "volume": volume})
            self._press_ptt()
            return len(data) / self.sample_rate
        except Exception as e:
            with open("debug.log", "a") as f:
                f.write(f"[AUDIO] Error loading sound: {e}\n")
            return 0.0

    def stop_all_sounds(self):
        """Clear playback queue and stop all sounds."""
        with self.lock:
            self.currently_playing.clear()

        # Drain the queue
        while not self.sound_queue.empty():
            try:
                self.sound_queue.get_nowait()
            except queue.Empty:
                break

        # Release PTT key
        self._release_ptt()

    def set_ptt_key(self, key: Optional[str]):
        """Set the Push-to-Talk key for Discord integration."""
        self.ptt_key = key if key and key.strip() else None
        with open("debug.log", "a") as f:
            f.write(f"[PTT] Key set to: {self.ptt_key}\n")

    def _press_ptt(self):
        """Press the PTT key if configured and not already pressed."""
        if not self.ptt_key:
            return

        with self._ptt_lock:
            if not self.ptt_active:
                try:
                    with open("debug.log", "a") as f:
                        f.write(f"[PTT] Pressing: {self.ptt_key}\n")
                    # Check if it's a mouse button
                    if self.ptt_key.startswith("mouse"):
                        # Map mouse button names back to Windows button names
                        # mouse4 = x2 (XBUTTON2), mouse5 = x (XBUTTON1) - matches Discord
                        button_map = {
                            "mouse1": "left",
                            "mouse2": "right",
                            "mouse3": "middle",
                            "mouse4": "x2",
                            "mouse5": "x",
                        }
                        button = button_map.get(self.ptt_key, "x2")
                        with open("debug.log", "a") as f:
                            f.write(f"[PTT] Using Windows API to press: {button}\n")
                        _simulate_mouse_button(button, press=True)
                        with open("debug.log", "a") as f:
                            f.write(f"[PTT] Windows API mouse press done\n")
                    else:
                        import keyboard

                        keyboard.press(self.ptt_key)
                    self.ptt_active = True
                    with open("debug.log", "a") as f:
                        f.write(f"[PTT] Active = True\n")
                except Exception as e:
                    with open("debug.log", "a") as f:
                        f.write(f"Failed to press PTT key: {e}\n")

    def _release_ptt(self):
        """Release the PTT key if it's currently pressed."""
        if not self.ptt_key:
            return

        with self._ptt_lock:
            if self.ptt_active:
                try:
                    # Check if it's a mouse button
                    if self.ptt_key.startswith("mouse"):
                        # Map mouse button names back to Windows button names
                        button_map = {
                            "mouse1": "left",
                            "mouse2": "right",
                            "mouse3": "middle",
                            "mouse4": "x2",
                            "mouse5": "x",
                        }
                        button = button_map.get(self.ptt_key, "x2")
                        _simulate_mouse_button(button, press=False)
                    else:
                        import keyboard

                        keyboard.release(self.ptt_key)
                    self.ptt_active = False
                except Exception as e:
                    with open("debug.log", "a") as f:
                        f.write(f"Failed to release PTT key: {e}\n")

    def _check_ptt_release(self):
        """Check if all sounds finished and release PTT if so."""
        with self.lock:
            if len(self.currently_playing) == 0 and self.sound_queue.empty():
                self._release_ptt()
