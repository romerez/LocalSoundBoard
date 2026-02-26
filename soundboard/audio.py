"""
Audio mixing and playback for the Discord Soundboard.
"""

# Limit parallel threads in numpy/scipy to prevent CPU saturation
# Must be set BEFORE importing numpy
import os

os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

import ctypes
import hashlib
import io
import logging
import queue
import shutil
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from typing import Optional, Dict, List, Tuple

from .constants import AUDIO, SOUNDS_DIR

logger = logging.getLogger(__name__)


# Mouse button simulation using the mouse library (simpler, more reliable)
def _simulate_mouse_button(button: str, press: bool = True):
    """
    Simulate mouse button press/release using the mouse library.
    Maps button names to mouse library constants.
    """
    import mouse as mouse_lib

    # Map our button names to mouse library button names
    button_map = {
        "left": mouse_lib.LEFT,
        "right": mouse_lib.RIGHT,
        "middle": mouse_lib.MIDDLE,
        "x": mouse_lib.X,
        "x1": mouse_lib.X,
        "x2": mouse_lib.X2,
    }

    mouse_button = button_map.get(button)
    if mouse_button is None:
        return

    if press:
        mouse_lib.press(button=mouse_button)
    else:
        mouse_lib.release(button=mouse_button)


def _get_vk_code(key: str) -> Optional[int]:
    """
    Get virtual key code for a key using Windows API.
    Uses VkKeyScanA for characters and MapVirtualKeyA for special keys.
    Falls back to keyboard library's built-in mapping if available.
    """
    key_lower = key.lower().strip()

    # For single printable characters, use Windows API VkKeyScanA
    if len(key_lower) == 1:
        result = ctypes.windll.user32.VkKeyScanA(ord(key_lower))
        if result != -1:
            return result & 0xFF  # Low byte is the VK code

    # For special keys, use the keyboard library's internal mapping (read-only, no hooks)
    try:
        import keyboard

        # keyboard.key_to_scan_codes returns scan codes, but we need VK codes
        # Use keyboard's internal name_to_key mapping
        if hasattr(keyboard, "_winkeyboard"):
            # keyboard library stores VK codes internally
            from keyboard import _winkeyboard  # type: ignore[attr-defined]

            # Try to find the key in keyboard's internal tables
            # Note: some "names" may contain non-strings (bools), so filter them
            try:
                for vk, names in getattr(_winkeyboard, "official_virtual_keys", {}).items():
                    str_names = [n.lower() for n in names if isinstance(n, str)]
                    if key_lower in str_names:
                        return vk
            except Exception:
                pass  # Fall through to alternative method

        # Alternative: use key_to_scan_codes and convert
        scan_codes = keyboard.key_to_scan_codes(key_lower)
        if scan_codes:
            # Convert scan code to VK using Windows API
            vk = ctypes.windll.user32.MapVirtualKeyA(scan_codes[0], 1)  # MAPVK_VSC_TO_VK
            if vk:
                return vk
    except Exception:
        pass

    return None


def _simulate_key(key: str, press: bool = True):
    """
    Simulate keyboard key press/release using Windows API (SendInput).
    Uses dynamic VK code lookup - no hardcoded key mappings.
    Does NOT use the keyboard library for sending - avoids hook conflicts.
    """
    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1

    # Get virtual key code dynamically
    vk = _get_vk_code(key)

    if vk is None:
        logger.warning("Unknown key for PTT simulation: %s", key)
        return

    _simulate_key_vk(vk, press)


def _simulate_key_vk(vk: int, press: bool = True):
    """
    Simulate keyboard key press/release using Windows API (SendInput).
    Takes a VK code directly - NO keyboard library calls, just pure Windows API.
    """
    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1

    # Define INPUT structure for SendInput
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_ulong),
            ("ki", KEYBDINPUT),
            ("padding", ctypes.c_ubyte * 8),
        ]

    flags = KEYEVENTF_KEYUP if not press else 0

    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = 0
    inp.ki.dwFlags = flags
    inp.ki.time = 0
    inp.ki.dwExtraInfo = None

    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


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
        AudioSegment.ffmpeg = FFMPEG_PATH  # type: ignore[attr-defined]
        AudioSegment.ffprobe = FFMPEG_PATH.replace("ffmpeg", "ffprobe")  # type: ignore[attr-defined]
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False


# Import librosa for pitch-preserving time stretch
# Set thread limits BEFORE importing to prevent CPU saturation
try:
    import librosa

    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False

# Lock to serialize librosa operations (prevents CPU saturation from concurrent calls)
_librosa_lock = threading.Lock()


def read_audio_file(file_path: str) -> Tuple[np.ndarray, int]:
    """
    Read an audio file, using pydub as fallback for formats
    that soundfile doesn't support well (OGG, M4A, AAC, WMA, etc.).

    Returns:
        Tuple of (audio_data as numpy array, sample_rate)

    Raises:
        RuntimeError if the file cannot be loaded
    """
    ext = Path(file_path).suffix.lower()

    # Formats that soundfile usually handles well (but may fail on some OGG files)
    soundfile_formats = {".wav", ".flac", ".aiff", ".aif"}

    # Try soundfile first for known reliable formats
    if ext in soundfile_formats:
        try:
            data, sr = sf.read(file_path, dtype="float32")
            return data, sr
        except Exception:
            pass  # Fall through to pydub fallback

    # Try soundfile for other formats (might work for some MP3s)
    try:
        data, sr = sf.read(file_path, dtype="float32")
        return data, sr
    except Exception:
        pass  # Fall through to pydub fallback

    # Fallback to pydub for OGG, M4A, AAC, WMA, WebM, etc.
    if PYDUB_AVAILABLE:
        try:
            audio = AudioSegment.from_file(file_path)

            # Get audio properties
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


def _resample_audio(data: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """
    Resample audio using numpy linear interpolation (fastest, no external dependencies).

    Quality is lower than FFT-based methods but guaranteed fast (~5ms for typical audio).

    Args:
        data: Audio data as numpy array (mono or stereo)
        orig_sr: Original sample rate
        target_sr: Target sample rate

    Returns:
        Resampled audio data
    """
    if orig_sr == target_sr:
        return data

    # Pure numpy linear interpolation - guaranteed fast, no parallelization issues
    ratio = target_sr / orig_sr
    new_length = int(len(data) * ratio)
    old_indices = np.arange(len(data))
    new_indices = np.linspace(0, len(data) - 1, new_length)

    if data.ndim == 1:
        return np.interp(new_indices, old_indices, data).astype(np.float32)
    else:
        result = np.zeros((new_length, data.shape[1]), dtype=np.float32)
        for ch in range(data.shape[1]):
            result[:, ch] = np.interp(new_indices, old_indices, data[:, ch])
        return result


def _apply_fade_out(data: np.ndarray, sample_rate: int, fade_ms: int = 30) -> np.ndarray:
    """
    Apply a short fade-out to the end of audio IN-PLACE.

    Caller must ensure data is already a copy if the original must be preserved
    (e.g. get_sound_data() already returns copies, and _apply_speed creates new arrays).
    """
    fade_samples = int(sample_rate * fade_ms / 1000)

    if fade_samples <= 0 or len(data) < fade_samples:
        return data

    fade_curve = np.linspace(1.0, 0.0, fade_samples).astype(np.float32)

    if data.ndim == 1:
        data[-fade_samples:] *= fade_curve
    else:
        for ch in range(data.shape[1]):
            data[-fade_samples:, ch] *= fade_curve

    return data


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
                data = _resample_audio(data, sr, self.sample_rate)

            with self._lock:
                self._cache[file_path] = data

            return data
        except Exception as e:
            print(f"Error loading sound into cache: {e}")
            raise

    def _read_audio_file(self, file_path: str) -> Tuple[np.ndarray, int]:
        """
        Read an audio file. Delegates to module-level read_audio_file function.
        """
        return read_audio_file(file_path)

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
        self._cached_ptt_vk: Optional[int] = None  # Pre-cached VK code for keyboard keys
        # PTT command queue - audio callback puts commands here, background thread executes
        self._ptt_queue: queue.Queue = queue.Queue()
        self._ptt_thread: Optional[threading.Thread] = None
        # PTT release debounce: wait N callback cycles after last sound finishes
        # At 48kHz with 1024 block size, each cycle is ~21ms
        # Discord has audio processing buffers, so we need ~300ms to ensure
        # the end of sounds isn't cut off when PTT releases
        self._ptt_release_delay = 15  # ~300ms delay before releasing PTT
        self._ptt_release_countdown = 0  # Current countdown (0 = not counting)

        # Local monitoring (play sounds to speakers too)
        self.monitor_enabled = False
        self.monitor_stream = None
        self._monitor_queue: queue.Queue = queue.Queue()  # Queue for monitor audio blocks

        # Shutdown flag - signals background threads to abort
        self._shutting_down = False

        # Start PTT worker thread
        self._start_ptt_thread()

    def _start_ptt_thread(self):
        """Start the background thread that processes PTT commands."""

        def ptt_worker():
            """Process PTT commands from queue in background thread."""
            while not self._shutting_down:
                try:
                    # Wait for command with timeout so we can check shutdown flag
                    cmd = self._ptt_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if cmd == "press":
                    self._do_ptt_press()
                elif cmd == "release":
                    self._do_ptt_release()
                elif cmd == "force_release":
                    self._do_ptt_release()
                elif cmd == "stop":
                    break

        self._ptt_thread = threading.Thread(target=ptt_worker, daemon=True)
        self._ptt_thread.start()

    def _do_ptt_press(self):
        """Actually press the PTT key. Called from PTT worker thread only."""
        if not self.ptt_key or self.ptt_active:
            return
        try:
            logger.debug("PTT pressing: %s", self.ptt_key)
            if self.ptt_key.startswith("mouse"):
                button = self.PTT_BUTTON_MAP.get(self.ptt_key, "x2")
                _simulate_mouse_button(button, press=True)
            elif self._cached_ptt_vk is not None:
                _simulate_key_vk(self._cached_ptt_vk, press=True)
            else:
                logger.warning("No cached VK code for PTT key: %s", self.ptt_key)
                return
            self.ptt_active = True
        except Exception as e:
            logger.error("Failed to press PTT key: %s", e)

    def _do_ptt_release(self):
        """Actually release the PTT key. Called from PTT worker thread only."""
        if not self.ptt_key or not self.ptt_active:
            return
        try:
            if self.ptt_key.startswith("mouse"):
                button = self.PTT_BUTTON_MAP.get(self.ptt_key, "x2")
                _simulate_mouse_button(button, press=False)
            elif self._cached_ptt_vk is not None:
                _simulate_key_vk(self._cached_ptt_vk, press=False)
            else:
                return
            self.ptt_active = False
        except Exception as e:
            logger.error("Failed to release PTT key: %s", e)

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
        """End audio streams. Non-blocking - uses abort() for faster shutdown."""
        self._shutting_down = True
        self.running = False

        # CRITICAL: Release PTT key first to prevent Windows UI freeze
        self._force_release_ptt()

        # Use abort() instead of stop() for faster, non-blocking shutdown
        if self.input_stream:
            try:
                self.input_stream.abort()
                self.input_stream.close()
            except Exception:
                pass
            self.input_stream = None
        if self.output_stream:
            try:
                self.output_stream.abort()
                self.output_stream.close()
            except Exception:
                pass
            self.output_stream = None
        if self.monitor_stream:
            try:
                self.monitor_stream.abort()
                self.monitor_stream.close()
            except Exception:
                pass
            self.monitor_stream = None

        # Clear any pending sounds
        with self.lock:
            self.currently_playing.clear()
        while not self.sound_queue.empty():
            try:
                self.sound_queue.get_nowait()
            except queue.Empty:
                break

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
                # Skip paused sounds
                if sound.get("paused", False):
                    continue

                # Handle loop delay phase
                if sound.get("in_delay", False):
                    delay_remaining = sound["loop_delay_samples"] - sound["delay_position"]
                    if delay_remaining <= 0:
                        # Delay finished, reset for next loop iteration
                        sound["in_delay"] = False
                        sound["position"] = 0
                        sound["delay_position"] = 0
                        # Decrement loops_remaining if not infinite
                        if sound.get("loops_remaining", 0) > 0:
                            sound["loops_remaining"] -= 1
                    else:
                        # Still in delay - add silence
                        sound["delay_position"] += frames
                        continue

                pos = sound["position"]
                data = sound["data"]
                volume = sound["volume"]
                remaining = len(data) - pos

                if remaining <= 0:
                    # Sound finished this iteration
                    if sound.get("loop", False):
                        # Check if more loops remain
                        loops_remaining = sound.get("loops_remaining", -1)
                        if loops_remaining != 0:  # -1 = infinite, >0 = more loops
                            # Start delay phase if configured
                            if sound.get("loop_delay_samples", 0) > 0:
                                sound["in_delay"] = True
                                sound["delay_position"] = 0
                            else:
                                # No delay, reset immediately
                                sound["position"] = 0
                                if loops_remaining > 0:
                                    sound["loops_remaining"] -= 1
                            continue
                    # Not looping or no more loops - mark as finished
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

        # Apply soft clipping to allow volume boost above 100% to sound louder
        # This soft limiter preserves normal audio but compresses peaks above 1.0
        # instead of hard clipping, so volume boost actually increases loudness
        outdata[:] = self._soft_clip(mixed)

        # Queue sounds-only for local speaker monitoring
        if self.monitor_enabled and np.any(sounds_mix):
            # Only queue if there's actual sound data (not silence)
            clipped = self._soft_clip(sounds_mix).astype(np.float32)
            try:
                self._monitor_queue.put_nowait(clipped.copy())
            except queue.Full:
                pass  # Drop frame if queue is full

    def play_sound(
        self,
        file_path: str,
        volume: float = 1.0,
        speed: float = 1.0,
        preserve_pitch: bool = True,
        sound_id: Optional[str] = None,
        loop: bool = False,
        loop_count: int = 0,
        loop_delay: float = 0.0,
    ) -> float:
        """Queue a sound for playback. Uses cache if available for better performance.

        Args:
            file_path: Path to the audio file
            volume: Playback volume (0.0 to 1.5)
            speed: Playback speed (0.5 to 2.0, where 1.0 is normal)
            preserve_pitch: If True and speed != 1.0, use librosa time-stretch (preserves pitch)
            sound_id: Optional identifier for this sound (used to stop individual sounds)
            loop: If True, sound will loop
            loop_count: Number of times to loop (0 = infinite)
            loop_delay: Delay between loops in seconds

        Returns duration in seconds (0.0 if failed).
        """
        # Abort if shutting down
        if self._shutting_down:
            return 0.0

        # Clamp speed to valid range
        speed = max(0.5, min(2.0, speed))

        # Reset PTT release countdown immediately when a new sound is triggered
        self._ptt_release_countdown = 0

        # If speed change with librosa is needed, run in background thread to avoid UI freeze
        if speed != 1.0 and preserve_pitch and LIBROSA_AVAILABLE:
            # Run processing in background thread
            def process_and_play():
                self._play_sound_sync(
                    file_path, volume, speed, preserve_pitch, sound_id, loop, loop_count, loop_delay
                )

            thread = threading.Thread(target=process_and_play, daemon=True)
            thread.start()

            # Return estimated duration (actual may differ slightly after time-stretch)
            if self.sound_cache:
                data = self.sound_cache.get_sound_data(file_path)
                if data is not None:
                    return len(data) / self.sample_rate / speed
            return 1.0  # Fallback estimate

        # For normal speed or simple resample, run synchronously (fast)
        return self._play_sound_sync(
            file_path, volume, speed, preserve_pitch, sound_id, loop, loop_count, loop_delay
        )

    def _play_sound_sync(
        self,
        file_path: str,
        volume: float,
        speed: float,
        preserve_pitch: bool,
        sound_id: Optional[str],
        loop: bool,
        loop_count: int,
        loop_delay: float,
        skip_ptt: bool = False,
    ) -> float:
        """Synchronous sound playback - does all processing on calling thread.

        Internal method called by play_sound. For speed != 1.0, this is called
        from a background thread to prevent UI freeze.
        """
        # Abort if shutting down
        if self._shutting_down:
            return 0.0

        try:
            # Use cached audio data if available (much faster - no disk I/O)
            if self.sound_cache:
                data = self.sound_cache.get_sound_data(file_path)
                if data is not None:
                    # Apply speed adjustment (uses librosa time-stretch if preserve_pitch=True)
                    if speed != 1.0:
                        data = self._apply_speed(data, speed, preserve_pitch)
                    # Apply fade-out to prevent abrupt cutoff (skip for looping sounds)
                    if not loop:
                        data = _apply_fade_out(data, self.sample_rate)
                    # Queue sound FIRST, then press PTT (prevents race condition where
                    # output callback sees empty queue and releases PTT immediately)
                    sound_entry = {
                        "data": data,
                        "position": 0,
                        "volume": volume,
                        "sound_id": sound_id,
                        "loop": loop,
                        "loop_count": loop_count,
                        "loops_remaining": loop_count if loop_count > 0 else -1,
                        "loop_delay": loop_delay,
                        "loop_delay_samples": int(loop_delay * self.sample_rate),
                        "in_delay": False,
                        "delay_position": 0,
                        "file_path": file_path,
                        "speed": speed,
                        "preserve_pitch": preserve_pitch,
                        "name": Path(file_path).stem if file_path else "Unknown",
                        "paused": False,
                    }
                    self.sound_queue.put(sound_entry)
                    if not skip_ptt:
                        self._press_ptt()
                    logger.debug(
                        "Queued from cache: %d samples (speed=%s, preserve_pitch=%s, volume=%s, id=%s, loop=%s)",
                        len(data),
                        speed,
                        preserve_pitch,
                        volume,
                        sound_id,
                        loop,
                    )
                    return len(data) / self.sample_rate

            # Fallback: load from disk (slower) - uses pydub for OGG/M4A/etc
            if not os.path.exists(file_path):
                return 0.0

            data, sr = read_audio_file(file_path)

            # Resample if needed
            if sr != self.sample_rate:
                data = _resample_audio(data, sr, self.sample_rate)

            # Apply speed adjustment (fast - simple resampling)
            if speed != 1.0:
                if self._shutting_down:
                    return 0.0
                data = self._apply_speed(data, speed, preserve_pitch)

            # Apply fade-out to prevent abrupt cutoff (skip for looping sounds)
            if not loop:
                data = _apply_fade_out(data, self.sample_rate)

            logger.debug(
                "Playing from disk: %d samples (speed=%s, preserve_pitch=%s, id=%s, loop=%s)",
                len(data),
                speed,
                preserve_pitch,
                sound_id,
                loop,
            )
            # Queue sound FIRST, then press PTT
            sound_entry = {
                "data": data,
                "position": 0,
                "volume": volume,
                "sound_id": sound_id,
                "loop": loop,
                "loop_count": loop_count,
                "loops_remaining": loop_count if loop_count > 0 else -1,
                "loop_delay": loop_delay,
                "loop_delay_samples": int(loop_delay * self.sample_rate),
                "in_delay": False,
                "delay_position": 0,
                "file_path": file_path,
                "speed": speed,
                "preserve_pitch": preserve_pitch,
                "name": Path(file_path).stem if file_path else "Unknown",
                "paused": False,
            }
            self.sound_queue.put(sound_entry)
            if not skip_ptt:
                self._press_ptt()
            return len(data) / self.sample_rate
        except Exception as e:
            logger.error("Error loading sound: %s", e)
            return 0.0

    def _apply_speed(
        self, data: np.ndarray, speed: float, preserve_pitch: bool = True
    ) -> np.ndarray:
        """Apply playback speed adjustment to audio data.

        Args:
            data: Audio data as numpy array
            speed: Speed factor (>1.0 = faster, <1.0 = slower)
            preserve_pitch: If True, use time-stretch (natural sound); if False, simple resample (chipmunk/deep voice)

        Speed > 1.0 = faster (shorter duration)
        Speed < 1.0 = slower (longer duration)
        """
        logger.debug(
            "Speed: speed=%s, preserve_pitch=%s, LIBROSA=%s",
            speed,
            preserve_pitch,
            LIBROSA_AVAILABLE,
        )

        if speed == 1.0:
            return data

        # Use librosa time_stretch for pitch preservation if available and requested
        if preserve_pitch and LIBROSA_AVAILABLE:
            try:
                # Serialize librosa calls to prevent CPU saturation
                with _librosa_lock:
                    # Convert stereo to mono for librosa, then back
                    if data.ndim == 2:
                        # Process each channel separately
                        left = librosa.effects.time_stretch(data[:, 0], rate=speed)
                        right = librosa.effects.time_stretch(data[:, 1], rate=speed)
                        result = np.column_stack([left, right])
                    else:
                        result = librosa.effects.time_stretch(data, rate=speed)
                    return result.astype(np.float32)
            except Exception as e:
                logger.warning("librosa time_stretch failed, falling back to resample: %s", e)

        # Fallback: simple resampling (changes pitch - chipmunk/deep voice effect)
        # Speed > 1.0 = faster + higher pitch
        # Speed < 1.0 = slower + lower pitch
        new_sr = int(self.sample_rate * speed)
        result = _resample_audio(data, new_sr, self.sample_rate)
        return result.astype(np.float32)

    def _soft_clip(self, x: np.ndarray) -> np.ndarray:
        """Apply soft limiting to prevent harsh clipping while allowing volume boost.

        Volume > 100% makes audio louder. This limiter:
        - Values under 1.0: pass through UNCHANGED
        - Values 1.0-2.0: compressed to 1.0-1.35 range (still noticeably louder)
        - Values > 2.0: approaches 1.4 asymptotically

        At 150% volume on a normalized sound (peaks at 0.7):
        - 0.7 * 1.5 = 1.05 -> output ~1.02 (still louder than 1.0)
        - 0.5 * 1.5 = 0.75 -> output 0.75 (unchanged, full 50% boost)
        """
        # Fast path: no limiting needed for normal audio
        max_abs = np.max(np.abs(x))
        if max_abs <= 1.0:
            return x.astype(np.float32)

        abs_x = np.abs(x)
        sign_x = np.sign(x)

        # Start with original values (preserves values <= 1.0)
        result = np.copy(x)

        # Find samples above 1.0 that need limiting
        hot = abs_x > 1.0

        if np.any(hot):
            # Map values above 1.0 to a compressed range
            # Using curve: 1.0 + 0.4 * tanh((x - 1.0))
            # This gives approximately:
            #   input 1.0 -> output 1.0
            #   input 1.2 -> output ~1.08
            #   input 1.5 -> output ~1.16
            #   input 2.0 -> output ~1.30
            #   input 3.0 -> output ~1.38 (approaches 1.4)
            excess = abs_x[hot] - 1.0
            soft_output = 1.0 + 0.4 * np.tanh(excess)
            result[hot] = sign_x[hot] * soft_output

        return result.astype(np.float32)

    def stop_sound(self, sound_id: str):
        """Stop a specific sound by its ID.

        Args:
            sound_id: The identifier of the sound to stop
        """
        logger.debug(
            "Stop: id=%s, currently_playing=%d, ids=%s",
            sound_id,
            len(self.currently_playing),
            [s.get("sound_id") for s in self.currently_playing],
        )

        with self.lock:
            # Remove sounds with matching ID from currently_playing
            before_count = len(self.currently_playing)
            self.currently_playing = [
                s for s in self.currently_playing if s.get("sound_id") != sound_id
            ]
            after_count = len(self.currently_playing)

        logger.debug("Stop: removed %d sounds from currently_playing", before_count - after_count)

        # Also drain matching sounds from queue
        remaining = []
        while not self.sound_queue.empty():
            try:
                sound = self.sound_queue.get_nowait()
                if sound.get("sound_id") != sound_id:
                    remaining.append(sound)
            except queue.Empty:
                break

        # Put back non-matching sounds
        for sound in remaining:
            self.sound_queue.put(sound)

        # Check if we should release PTT (no more sounds playing)
        with self.lock:
            if len(self.currently_playing) == 0 and self.sound_queue.empty():
                self._release_ptt()

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

    def pause_sound(self, sound_id: str):
        """Pause a specific sound by its ID.

        Args:
            sound_id: The identifier of the sound to pause
        """
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["paused"] = True
                    logger.debug("Paused sound: %s", sound_id)
                    break

    def resume_sound(self, sound_id: str):
        """Resume a paused sound by its ID.

        Args:
            sound_id: The identifier of the sound to resume
        """
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["paused"] = False
                    logger.debug("Resumed sound: %s", sound_id)
                    break

    def toggle_sound_loop(self, sound_id: str, loop: Optional[bool] = None):
        """Toggle or set the loop state of a playing sound.

        Args:
            sound_id: The identifier of the sound
            loop: If provided, sets the loop state; if None, toggles
        """
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    if loop is None:
                        sound["loop"] = not sound.get("loop", False)
                    else:
                        sound["loop"] = loop
                    # If enabling loop and loops_remaining was 0, set to infinite
                    if sound["loop"] and sound.get("loops_remaining", 0) == 0:
                        sound["loops_remaining"] = -1
                    logger.debug("Toggled loop for %s: %s", sound_id, sound["loop"])
                    break

    def set_sound_speed(self, sound_id: str, speed: float, preserve_pitch: bool = True):
        """Change the playback speed of a currently playing sound.

        Re-processes the audio data at the new speed.

        Args:
            sound_id: The identifier of the sound
            speed: New speed (0.5 to 2.0)
            preserve_pitch: If True and librosa available, preserves pitch
        """
        if self._shutting_down:
            return

        speed = max(0.5, min(2.0, speed))

        # Get sound info
        file_path = None
        old_pos = 0
        old_total = 0
        is_looping = False

        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    file_path = sound.get("file_path")
                    old_pos = sound.get("position", 0)
                    old_total = len(sound.get("data", []))
                    is_looping = sound.get("loop", False)
                    break

        if not file_path or not self.sound_cache:
            return

        original_data = self.sound_cache.get_sound_data(file_path)
        if original_data is None:
            return

        # Apply speed (uses librosa time-stretch if preserve_pitch=True and available)
        if speed != 1.0:
            new_data = self._apply_speed(original_data, speed, preserve_pitch)
        else:
            new_data = original_data.copy()

        # Apply fade-out for non-looping sounds
        if not is_looping:
            new_data = _apply_fade_out(new_data, self.sample_rate)

        # Calculate new position based on progress ratio
        progress_ratio = old_pos / old_total if old_total > 0 else 0.0
        new_pos = int(progress_ratio * len(new_data))

        # Update sound data
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["data"] = new_data
                    sound["position"] = new_pos
                    sound["speed"] = speed
                    break

    def get_playing_sounds(self) -> List[Dict]:
        """Get a snapshot of currently playing sounds for UI display.

        Returns a list of dicts with:
            - sound_id: Unique identifier
            - name: Display name
            - progress: Progress ratio (0.0-1.0)
            - volume: Volume level
            - loop: Whether looping
            - loops_remaining: Remaining loops (-1 for infinite)
            - in_delay: Whether in loop delay phase
            - paused: Whether sound is paused
            - speed: Current playback speed
        """
        result = []
        with self.lock:
            for sound in self.currently_playing:
                data_len = len(sound.get("data", []))
                pos = sound.get("position", 0)
                progress = pos / data_len if data_len > 0 else 0.0

                result.append(
                    {
                        "sound_id": sound.get("sound_id"),
                        "name": sound.get("name", "Unknown"),
                        "progress": progress,
                        "volume": sound.get("volume", 1.0),
                        "loop": sound.get("loop", False),
                        "loops_remaining": sound.get("loops_remaining", 0),
                        "in_delay": sound.get("in_delay", False),
                        "paused": sound.get("paused", False),
                        "speed": sound.get("speed", 1.0),
                        "file_path": sound.get("file_path"),
                    }
                )
        return result

    def set_ptt_key(self, key: Optional[str]):
        """Set the Push-to-Talk key for Discord integration.

        Caches the VK code immediately so we never touch keyboard library during playback.
        """
        self.ptt_key = key if key and key.strip() else None

        # Pre-cache the VK code NOW, not during playback
        # This is the only place we touch the keyboard library for VK lookup
        if self.ptt_key and not self.ptt_key.startswith("mouse"):
            self._cached_ptt_vk = _get_vk_code(self.ptt_key)
            if self._cached_ptt_vk is None:
                logger.warning("Could not get VK code for PTT key: %s", self.ptt_key)
        else:
            self._cached_ptt_vk = None

        logger.debug("PTT key set to: %s (VK: %s)", self.ptt_key, self._cached_ptt_vk)

    # mouse button name → Windows API button name (matches Discord's labeling)
    PTT_BUTTON_MAP = {
        "mouse1": "left",
        "mouse2": "right",
        "mouse3": "middle",
        "mouse4": "x2",
        "mouse5": "x",
    }

    def _press_ptt(self):
        """Queue a PTT press command. Executes in background thread.

        NEVER blocks - just puts command in queue. Safe to call from any thread.
        """
        if not self.ptt_key or self.ptt_active:
            return
        try:
            self._ptt_queue.put_nowait("press")
        except queue.Full:
            pass

    def _release_ptt(self):
        """Queue a PTT release command. Executes in background thread.

        NEVER blocks - just puts command in queue. Safe to call from any thread.
        """
        if not self.ptt_key or not self.ptt_active:
            return
        try:
            self._ptt_queue.put_nowait("release")
        except queue.Full:
            pass

    def _force_release_ptt(self):
        """Force release PTT key unconditionally. Used during shutdown.

        This one executes IMMEDIATELY (not queued) since it's for shutdown.
        """
        if not self.ptt_key:
            return
        # Signal the worker thread to stop
        try:
            self._ptt_queue.put_nowait("stop")
        except queue.Full:
            pass
        # Also directly release to ensure key isn't stuck
        try:
            if self.ptt_key.startswith("mouse"):
                button = self.PTT_BUTTON_MAP.get(self.ptt_key, "x2")
                _simulate_mouse_button(button, press=False)
            elif self._cached_ptt_vk is not None:
                _simulate_key_vk(self._cached_ptt_vk, press=False)
            self.ptt_active = False
        except Exception:
            pass  # Ignore errors during shutdown

    def _check_ptt_release(self):
        """Check if all sounds finished and release PTT if so."""
        with self.lock:
            if len(self.currently_playing) == 0 and self.sound_queue.empty():
                self._release_ptt()
