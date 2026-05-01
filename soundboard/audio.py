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
import glob
import hashlib
import io
import logging
import queue
import shutil
import sys
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from typing import Optional, Dict, List, Tuple

from .constants import AUDIO, SOUNDS_DIR

logger = logging.getLogger(__name__)


# --- Real-time pitch-preserving time-stretch (WSOLA-lite) ---------------
# Frame size and synthesis hop for the in-callback OLA time-stretcher used
# when a sound has pitch_preserve_live=True. Hann window with 75% overlap
# (HS = FRAME/4) is COLA so no normalization is needed.
WSOLA_FRAME = 2048  # ~43ms grain at 48kHz
WSOLA_HS = 512  # synthesis hop → 75% overlap (Hann is COLA)


# Optional noise-suppression dependency. Loaded lazily so the app still
# starts if the user hasn't installed noisereduce yet.
try:
    import noisereduce as _nr  # type: ignore

    NOISEREDUCE_AVAILABLE = True
except Exception:  # pragma: no cover - import-time guard
    _nr = None
    NOISEREDUCE_AVAILABLE = False


class NoiseSuppressor:
    """Real-time noise suppressor for mic input.

    Replaces Discord's Krisp noise suppression (which is bypassed when routing
    through a virtual cable). Uses spectral gating via the `noisereduce`
    library. Each incoming mic block is appended to a small ring buffer so
    the spectral analysis has enough context (~85 ms at 48 kHz), then only
    the latest block is returned to keep latency low.

    Designed to be cheap enough to run inside `_input_callback`. Falls back
    to a passthrough (returns input unchanged) if `noisereduce` is missing
    or processing fails.
    """

    def __init__(self, sample_rate: int, block_size: int):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.enabled: bool = False
        # Strength: 0.0 = no reduction, 1.0 = aggressive
        self.strength: float = 0.85
        # Context buffer (~85 ms at 48 kHz). Bigger = better spectral estimate
        # but more CPU per block. 4 blocks is a good compromise.
        self._buffer_size = max(block_size * 4, 4096)
        self._buffer = np.zeros(self._buffer_size, dtype=np.float32)
        # FFT size for spectral gating. 512 samples ~= 10 ms at 48 kHz.
        self._n_fft = 512

    def set_strength(self, strength: float) -> None:
        """Set reduction strength (0.0 - 1.0). Higher = more aggressive."""
        self.strength = max(0.0, min(1.0, float(strength)))

    def reset(self) -> None:
        """Clear the context buffer (e.g. when stream restarts)."""
        self._buffer = np.zeros(self._buffer_size, dtype=np.float32)

    def process(self, mic_block: np.ndarray) -> np.ndarray:
        """Apply noise suppression to a mic block. Returns same shape array.

        Safe to call even when disabled or when noisereduce is unavailable -
        will return the input unchanged in those cases.
        """
        if not self.enabled or not NOISEREDUCE_AVAILABLE or _nr is None:
            return mic_block
        n = len(mic_block)
        if n == 0:
            return mic_block
        try:
            # Slide buffer forward and append the new block at the end so
            # noisereduce has previous context to estimate the noise floor.
            if n >= self._buffer_size:
                self._buffer = mic_block[-self._buffer_size :].astype(np.float32, copy=True)
            else:
                self._buffer[:-n] = self._buffer[n:]
                self._buffer[-n:] = mic_block.astype(np.float32, copy=False)
            denoised = _nr.reduce_noise(
                y=self._buffer,
                sr=self.sample_rate,
                stationary=True,
                prop_decrease=self.strength,
                n_fft=self._n_fft,
            )
            out = np.asarray(denoised[-n:], dtype=np.float32)
            # Defensive: keep shape/length stable
            if len(out) != n:
                return mic_block
            return out
        except Exception as e:
            # Never let noise suppression break the audio callback - fall
            # back to passthrough on any failure.
            logger.debug("NoiseSuppressor.process failed: %s", e)
            return mic_block


# Mouse button simulation using direct Windows SendInput API
# Avoids the mouse library which can have internal state tracking issues
# that cause buttons to get "stuck" when physical and simulated inputs mix.
def _simulate_mouse_button(button: str, press: bool = True):
    """
    Simulate mouse button press/release using Windows SendInput API directly.
    This is more reliable than the mouse library because it doesn't maintain
    internal state that can get confused by concurrent physical button presses.
    """
    # Mouse event flags for SendInput
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_XDOWN = 0x0080
    MOUSEEVENTF_XUP = 0x0100
    XBUTTON1 = 0x0001
    XBUTTON2 = 0x0002
    INPUT_MOUSE = 0

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_ulong),
            ("mi", MOUSEINPUT),
            ("padding", ctypes.c_ubyte * 8),
        ]

    # Map button names to (down_flag, up_flag, mouseData)
    button_map = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
        "x": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
        "x1": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
        "x2": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
    }

    entry = button_map.get(button)
    if entry is None:
        return

    down_flag, up_flag, mouse_data = entry
    flags = down_flag if press else up_flag

    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dx = 0
    inp.mi.dy = 0
    inp.mi.mouseData = mouse_data
    inp.mi.dwFlags = flags
    inp.mi.time = 0
    inp.mi.dwExtraInfo = None

    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


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
except (ImportError, FileNotFoundError):
    FFMPEG_PATH = None

# When running as frozen exe, ffmpeg may be in the bundle directory
if not FFMPEG_PATH and getattr(sys, "frozen", False):
    import glob

    base = sys._MEIPASS  # type: ignore[attr-defined]
    candidates = glob.glob(os.path.join(base, "imageio_ffmpeg", "binaries", "ffmpeg*"))
    if candidates:
        FFMPEG_PATH = candidates[0]

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

        # Master volume — multiplier applied to ALL playing sounds before
        # they're added to the mix (separate from mic_volume which only
        # affects the microphone passthrough).
        self.master_volume = 1.0

        # Precomputed Hann window for real-time WSOLA pitch-preserving
        # time-stretch (see _wsola_render). Built once; reused per grain.
        self._wsola_window = np.hanning(WSOLA_FRAME).astype(np.float32)

        # Noise suppression (replaces Discord's Krisp NS which is bypassed
        # when routing through the virtual cable). Disabled by default;
        # toggled via the GUI checkbox.
        self.noise_suppressor = NoiseSuppressor(self.sample_rate, self.block_size)

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
        # Safety timeout: track how long PTT has been held (in callback cycles)
        # If PTT is active for too long without sounds, force release as safety net
        self._ptt_active_cycles = 0  # Counts callback cycles while ptt_active is True
        self._ptt_max_hold_cycles = 500  # ~10 seconds (500 * ~21ms)

        # Local monitoring (play sounds to speakers too)
        self.monitor_enabled = False
        self.monitor_stream = None
        self._monitor_queue: queue.Queue = queue.Queue()  # Queue for monitor audio blocks

        # Optional mic tap for the call recorder. When set, every mic block
        # captured in `_input_callback` is also pushed to this queue so the
        # Recorder can mix the user's own voice into the recording. The
        # Recorder owns the queue lifecycle (set/clear via attach/detach).
        self._recording_tap: Optional[queue.Queue] = None

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

                try:
                    if cmd == "press":
                        self._do_ptt_press()
                    elif cmd == "release":
                        self._do_ptt_release()
                    elif cmd == "force_release":
                        self._do_ptt_release()
                    elif cmd == "stop":
                        break
                except BaseException as e:
                    # Catch ALL exceptions (including SystemExit, KeyboardInterrupt)
                    # to prevent worker thread from dying silently
                    logger.error("PTT worker error processing '%s': %s", cmd, e)
                    # If we crashed during a release attempt, force ptt_active = False
                    # so the next release attempt can try again
                    if cmd in ("release", "force_release"):
                        self.ptt_active = False

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
                # No valid key config - clear active flag to prevent stuck state
                self.ptt_active = False
                return
        except Exception as e:
            logger.error("Failed to release PTT key: %s", e)
        finally:
            # ALWAYS clear ptt_active on release attempt, even if API call failed.
            # If the OS-level key is stuck, retrying the same failed call won't help.
            # Better to reset state so the next press/release cycle starts clean.
            self.ptt_active = False
            self._ptt_active_cycles = 0

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
        self._force_release_ptt(shutdown=True)

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

        # Apply noise suppression to the mic channel only (sounds are mixed
        # in later, untouched). This replaces Discord's Krisp NS which is
        # bypassed when routing through the virtual cable.
        if self.noise_suppressor.enabled:
            mic_data = self.noise_suppressor.process(mic_data)

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

        # Also tap the (post-noise-suppression) mic into the recorder queue
        # if recording is active. We push the volume-applied signal so the
        # recording matches what the user actually sends to Discord.
        tap = self._recording_tap
        if tap is not None and not self.mic_muted:
            try:
                tap.put_nowait((mic_data * self.mic_volume).astype(np.float32, copy=False))
            except queue.Full:
                # Drop oldest to keep recorder caught up
                try:
                    tap.get_nowait()
                    tap.put_nowait((mic_data * self.mic_volume).astype(np.float32, copy=False))
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
                        # Reset WSOLA state so the new loop iteration
                        # starts with a clean overlap buffer.
                        sound.pop("wsola_buf", None)
                        sound["wsola_read"] = 0
                        sound["wsola_write"] = 0
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
                # Live playback rate (instant speed change via on-the-fly
                # linear-interp resampling — no buffer rebuild). 1.0 = no
                # change. Set by set_playback_rate(); the librosa-based
                # set_sound_speed() leaves this at 1.0 and rebuilds the
                # buffer instead.
                rate = float(sound.get("playback_rate", 1.0))
                data_len = len(data)
                remaining = data_len - pos

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
                                # Clear WSOLA buffer for clean restart
                                sound.pop("wsola_buf", None)
                                sound["wsola_read"] = 0
                                sound["wsola_write"] = 0
                                if loops_remaining > 0:
                                    sound["loops_remaining"] -= 1
                            continue
                    # Not looping or no more loops - mark as finished
                    finished.append(i)
                    continue

                if rate == 1.0:
                    # Fast path: integer indexing, no interpolation.
                    int_pos = int(pos)
                    chunk_size = min(frames, data_len - int_pos)
                    chunk = data[int_pos : int_pos + chunk_size] * volume * self.master_volume
                    new_pos = int_pos + chunk_size
                elif sound.get("pitch_preserve_live", False):
                    # WSOLA pitch-preserving path: time-stretches the
                    # audio in real time WITHOUT changing pitch. CPU cost
                    # is moderate (~one windowed OLA grain per ~10ms).
                    chunk, wsola_finished = self._wsola_render(sound, frames)
                    chunk = chunk * volume * self.master_volume
                    chunk_size = frames
                    # _wsola_render advances sound["position"] internally
                    new_pos = sound["position"]
                    if wsola_finished:
                        # Force end-of-input so loop logic catches it next
                        # callback (and the WSOLA buffer is cleared on
                        # reset so warmup is clean for next iteration).
                        new_pos = data_len
                else:
                    # Live resample path: produce `frames` output samples
                    # by reading `frames * rate` input samples with linear
                    # interpolation. Position advances fractionally.
                    max_out = int((data_len - pos) / rate) if rate > 0 else 0
                    out_frames = min(frames, max(0, max_out))
                    if out_frames <= 0:
                        # Out of input — treat as finished for this iteration
                        # (loop handling above will catch it next callback).
                        sound["position"] = data_len
                        continue
                    indices = pos + np.arange(out_frames, dtype=np.float64) * rate
                    i0 = indices.astype(np.int64)
                    np.clip(i0, 0, data_len - 1, out=i0)
                    i1 = np.minimum(i0 + 1, data_len - 1)
                    frac = (indices - i0).astype(np.float32)
                    if data.ndim == 2:
                        chunk = (
                            (data[i0] * (1.0 - frac)[:, None] + data[i1] * frac[:, None])
                            * volume
                            * self.master_volume
                        )
                    else:
                        chunk = (
                            (data[i0] * (1.0 - frac) + data[i1] * frac)
                            * volume
                            * self.master_volume
                        )
                    chunk_size = out_frames
                    new_pos = pos + out_frames * rate

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
                sound["position"] = new_pos

            # Remove finished sounds
            for i in reversed(finished):
                self.currently_playing.pop(i)

            # Check if we should start PTT release countdown
            # Only count sounds that are actively producing audio (not paused)
            active_sounds = sum(1 for s in self.currently_playing if not s.get("paused", False))
            all_sounds_finished = active_sounds == 0

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

        # Safety timeout: force release if PTT has been held too long with NO
        # sounds playing (catches zombie state from worker crash / race conditions).
        # Only counts cycles while PTT is active AND nothing is playing — otherwise
        # long sounds, looping sounds, or rapid back-to-back plays would trip the
        # timeout mid-playback and cut PTT while audio is still being mixed.
        if self.ptt_active and all_sounds_finished and self.sound_queue.empty():
            self._ptt_active_cycles += 1
            if self._ptt_active_cycles >= self._ptt_max_hold_cycles:
                logger.warning(
                    "PTT safety timeout reached (%d cycles, no sounds playing), force releasing",
                    self._ptt_active_cycles,
                )
                # Use direct release (bypass queue) for maximum reliability
                self._force_release_ptt()
        else:
            self._ptt_active_cycles = 0

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
                # Force direct release for reliability (bypass queue)
                self._force_release_ptt()

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

        # Force release PTT key directly (bypass queue for reliability)
        self._force_release_ptt()

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

    def set_sound_volume(self, sound_id: str, volume: float):
        """Set the volume of a currently playing sound.

        Args:
            sound_id: The identifier of the sound
            volume: New volume (0.0 to 1.5)
        """
        volume = max(0.0, min(1.5, volume))
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["volume"] = volume
                    logger.debug("Set volume for %s: %.2f", sound_id, volume)
                    break

    def set_playback_rate(self, sound_id: str, rate: float):
        """Set the LIVE playback rate of a currently playing sound.

        This is INSTANT — no buffer rebuild, no librosa. The output callback
        applies linear-interpolation resampling on the fly. Side effect:
        pitch changes with rate (chipmunk/deep-voice). For pitch-preserved
        speed change, use set_sound_speed() instead (slow, librosa).

        Args:
            sound_id: The identifier of the sound
            rate: Playback rate (0.5 to 2.0). 1.0 = normal.
        """
        rate = max(0.5, min(2.0, rate))
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["playback_rate"] = rate
                    # Mirror into "speed" so the UI's get_playing_sounds()
                    # snapshot reflects the change.
                    sound["speed"] = rate
                    break

    def set_pitch_preserve_live(self, sound_id: str, enabled: bool):
        """Enable real-time pitch-preserving time-stretch for a playing sound.

        When enabled, set_playback_rate() changes preserve pitch (no
        chipmunk/deep voice) via in-callback WSOLA-lite OLA. CPU cost is
        moderate. When disabled, set_playback_rate() uses cheap linear
        interpolation (chipmunk/deep voice).

        Args:
            sound_id: The identifier of the sound
            enabled: True for pitch-preserved stretch, False for chipmunk
        """
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["pitch_preserve_live"] = bool(enabled)
                    # Reset WSOLA state so it warms up cleanly at the
                    # current playhead position (and we don't replay stale
                    # buffered output from the old mode).
                    sound.pop("wsola_buf", None)
                    sound["wsola_read"] = 0
                    sound["wsola_write"] = 0
                    break

    def _wsola_render(self, sound, frames):
        """Render `frames` samples using WSOLA-lite (windowed OLA).

        Pitch-preserving time-stretch in real time, suitable for the audio
        callback. Uses Hann windows with 75% overlap (which is COLA, so no
        normalization step is needed). Skips the cross-correlation
        similarity search of full WSOLA — purely overlap-add — to keep CPU
        cost low. Quality is acceptable for typical 0.5x–2.0x ranges.

        Advances ``sound["position"]`` by WSOLA_HS * rate per emitted grain.

        Returns:
            (chunk, finished) where ``chunk`` is shape (frames,) or
            (frames, channels) and ``finished`` is True when the input
            data has been exhausted.
        """
        data = sound["data"]
        data_len = len(data)
        rate = float(sound.get("playback_rate", 1.0))
        win = self._wsola_window

        # Lazy-init the OLA accumulator buffer (per sound).
        buf = sound.get("wsola_buf")
        if buf is None:
            if data.ndim == 2:
                buf = np.zeros((WSOLA_FRAME * 4, data.shape[1]), dtype=np.float32)
            else:
                buf = np.zeros((WSOLA_FRAME * 4,), dtype=np.float32)
            sound["wsola_buf"] = buf
            sound["wsola_read"] = 0
            sound["wsola_write"] = 0

        write = sound["wsola_write"]
        read = sound["wsola_read"]
        finished = False

        # Produce grains until we have at least `frames` samples ready or
        # input is exhausted.
        while (write - read) < frames:
            in_pos = float(sound["position"])
            i0 = int(in_pos)
            if i0 + WSOLA_FRAME >= data_len:
                finished = True
                break

            # Compact the buffer if next grain would overflow it.
            if write + WSOLA_FRAME > len(buf):
                n_unread = write - read
                if n_unread > 0:
                    buf[:n_unread] = buf[read:write]
                # Zero the rest so OLA accumulates correctly next grains.
                buf[n_unread:] = 0
                write = n_unread
                read = 0

            # Read grain, apply Hann window, overlap-add into buffer.
            grain = data[i0 : i0 + WSOLA_FRAME].astype(np.float32, copy=True)
            if grain.ndim == 2:
                grain *= win[:, None]
            else:
                grain *= win
            buf[write : write + WSOLA_FRAME] += grain

            # Advance: synthesis hop in OUTPUT, analysis hop in INPUT.
            # analysis_hop = synthesis_hop * rate → time-stretch by 1/rate
            # which means rate>1 plays faster, rate<1 plays slower (same
            # convention as the linear-interp resample path).
            write += WSOLA_HS
            sound["position"] = in_pos + WSOLA_HS * rate

        sound["wsola_write"] = write
        sound["wsola_read"] = read

        # Pull `frames` from buffer (may be short if input ran out).
        available = write - read
        n_out = min(frames, available)
        if data.ndim == 2:
            chunk = np.zeros((frames, data.shape[1]), dtype=np.float32)
        else:
            chunk = np.zeros((frames,), dtype=np.float32)
        if n_out > 0:
            chunk[:n_out] = buf[read : read + n_out]
            sound["wsola_read"] = read + n_out

        return chunk, finished

    def restart_sound(self, sound_id: str):
        """Restart a sound from the beginning.

        Args:
            sound_id: The identifier of the sound to restart
        """
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["position"] = 0
                    sound["in_delay"] = False
                    sound["delay_position"] = 0
                    sound["paused"] = False
                    logger.debug("Restarted sound: %s", sound_id)
                    break

    def set_sound_loop_count(self, sound_id: str, count: int):
        """Set the loop count for a currently playing sound.

        Args:
            sound_id: The identifier of the sound
            count: Number of remaining loops (0 = stop after current, -1 = infinite)
        """
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["loops_remaining"] = count
                    if count != 0:
                        sound["loop"] = True
                    logger.debug("Set loop count for %s: %d", sound_id, count)
                    break

    def set_sound_loop_delay(self, sound_id: str, delay: float):
        """Set the delay between loops for a currently playing sound.

        Args:
            sound_id: The identifier of the sound
            delay: Delay in seconds between loops
        """
        delay = max(0.0, min(10.0, delay))
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["loop_delay"] = delay
                    sound["loop_delay_samples"] = int(delay * self.sample_rate)
                    logger.debug("Set loop delay for %s: %.2f", sound_id, delay)
                    break

    def set_sound_speed(self, sound_id: str, speed: float, preserve_pitch: bool = True):
        """Change the playback speed of a currently playing sound.

        Re-processes the audio data at the new speed in a background-safe way.

        Concurrency model:
        - Optimistically writes the requested speed/preserve_pitch into the
          playing sound under the lock so the UI sees the new value immediately.
        - Runs the slow librosa work OUTSIDE any lock.
        - Before committing the new audio buffer, re-checks the sound's current
          requested speed; if it changed (user moved slider again), drops the
          stale result so the newest pending change wins.
        - Reads `position` INSIDE the commit lock so the new position reflects
          where playback actually is now (not where it was when this call
          started, several hundred ms ago).
        """
        if self._shutting_down:
            return

        speed = max(0.5, min(2.0, speed))

        # Phase 1: read what we need + optimistically publish requested speed
        file_path = None
        is_looping = False
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    file_path = sound.get("file_path")
                    is_looping = sound.get("loop", False)
                    # Optimistic write so get_playing_sounds() reports the
                    # value the user just selected, even before librosa
                    # finishes re-stretching the buffer.
                    sound["speed"] = speed
                    sound["preserve_pitch"] = preserve_pitch
                    break

        if not file_path or not self.sound_cache:
            return

        original_data = self.sound_cache.get_sound_data(file_path)
        if original_data is None:
            return

        # Phase 2: slow processing OUTSIDE the lock (librosa can take
        # hundreds of ms; we must not block the audio callback).
        if speed != 1.0:
            new_data = self._apply_speed(original_data, speed, preserve_pitch)
        else:
            new_data = original_data.copy()

        if not is_looping:
            new_data = _apply_fade_out(new_data, self.sample_rate)

        if self._shutting_down:
            return

        new_len = len(new_data)
        if new_len == 0:
            return

        # Phase 3: commit. Drop result if the user changed speed again while
        # we were processing (the newer call will commit its own result).
        # Also re-read `position` from the live sound so we don't snap back.
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") != sound_id:
                    continue
                # Stale-update guard: someone else won.
                if (
                    abs(sound.get("speed", 1.0) - speed) > 1e-6
                    or sound.get("preserve_pitch", True) != preserve_pitch
                ):
                    return
                old_data_len = len(sound.get("data", []))
                old_pos = sound.get("position", 0)
                progress_ratio = old_pos / old_data_len if old_data_len > 0 else 0.0
                new_pos = int(progress_ratio * new_len)
                if new_pos >= new_len:
                    new_pos = new_len - 1
                sound["data"] = new_data
                sound["position"] = max(0, new_pos)
                # Buffer is now baked at the new speed — clear any live
                # playback_rate so the callback uses the fast int path.
                sound["playback_rate"] = 1.0
                # speed/preserve_pitch were already set in phase 1
                break

    def get_playing_sounds(self) -> List[Dict]:
        """Get a snapshot of currently playing sounds for UI display.

        Returns a list of dicts with:
            - sound_id: Unique identifier
            - name: Display name
            - progress: Progress ratio (0.0-1.0)
            - volume: Volume level
            - loop: Whether looping
            - loop_count: Original loop count setting
            - loops_remaining: Remaining loops (-1 for infinite)
            - loop_delay: Delay between loops in seconds
            - in_delay: Whether in loop delay phase
            - paused: Whether sound is paused
            - speed: Current playback speed
            - elapsed_seconds: Elapsed time in seconds
            - total_seconds: Total duration in seconds
        """
        result = []
        with self.lock:
            for sound in self.currently_playing:
                data_len = len(sound.get("data", []))
                pos = sound.get("position", 0)
                progress = pos / data_len if data_len > 0 else 0.0

                total_seconds = data_len / self.sample_rate if data_len > 0 else 0.0
                elapsed_seconds = pos / self.sample_rate if data_len > 0 else 0.0

                result.append(
                    {
                        "sound_id": sound.get("sound_id"),
                        "name": sound.get("name", "Unknown"),
                        "progress": progress,
                        "volume": sound.get("volume", 1.0),
                        "loop": sound.get("loop", False),
                        "loop_count": sound.get("loop_count", 0),
                        "loops_remaining": sound.get("loops_remaining", 0),
                        "loop_delay": sound.get("loop_delay", 0.0),
                        "in_delay": sound.get("in_delay", False),
                        "paused": sound.get("paused", False),
                        "speed": sound.get("speed", 1.0),
                        "file_path": sound.get("file_path"),
                        "elapsed_seconds": elapsed_seconds,
                        "total_seconds": total_seconds,
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

    def _force_release_ptt(self, shutdown: bool = False):
        """Force release PTT key unconditionally. Bypasses the queue for reliability.

        Used when stopping all sounds and during shutdown. Executes immediately
        on the calling thread to prevent PTT from getting stuck.

        Args:
            shutdown: If True, also sends "stop" to the worker thread.
        """
        if not self.ptt_key:
            return
        if shutdown:
            # Signal the worker thread to stop
            try:
                self._ptt_queue.put_nowait("stop")
            except queue.Full:
                pass
        else:
            # Drain any pending press commands that could re-activate PTT after release
            while not self._ptt_queue.empty():
                try:
                    self._ptt_queue.get_nowait()
                except queue.Empty:
                    break
        # Directly release to ensure key isn't stuck (bypass queue)
        try:
            if self.ptt_key.startswith("mouse"):
                button = self.PTT_BUTTON_MAP.get(self.ptt_key, "x2")
                _simulate_mouse_button(button, press=False)
            elif self._cached_ptt_vk is not None:
                _simulate_key_vk(self._cached_ptt_vk, press=False)
        except Exception:
            pass  # Best effort
        finally:
            self.ptt_active = False
            self._ptt_active_cycles = 0
            self._ptt_release_countdown = 0

    def _check_ptt_release(self):
        """Check if all sounds finished and release PTT if so."""
        with self.lock:
            if len(self.currently_playing) == 0 and self.sound_queue.empty():
                self._release_ptt()


# =============================================================================
# Call Recorder (Discord output capture via WASAPI loopback)
# =============================================================================

# `soundcard` provides clean WASAPI loopback recording on Windows. Stock
# `sounddevice` does NOT — its `WasapiSettings` has no `loopback` flag — so
# we use soundcard for capturing the speaker output.
try:
    import soundcard as _sc  # type: ignore

    SOUNDCARD_AVAILABLE = True
except Exception:  # pragma: no cover
    _sc = None
    SOUNDCARD_AVAILABLE = False


class Recorder:
    """
    Records a Discord call to a compressed audio file.

    Captures the system playback device using WASAPI loopback (so it grabs
    everything Discord plays through the user's speakers/headphones — i.e.
    the other people on the call). Optionally mixes in the local mic via
    `AudioMixer._recording_tap` so both sides of the conversation are saved.

    On stop, the buffered float32 PCM is mixed down to mono and encoded
    to MP3 at a low bitrate via pydub (which uses the bundled ffmpeg from
    `imageio-ffmpeg`). Mono + 64 kbps is plenty for voice and keeps files
    tiny (~30 MB/hour).
    """

    DEFAULT_BITRATE = "64k"
    # Per-pull frame count for the soundcard recorder loop. ~21ms at 48kHz.
    _CAPTURE_FRAMES = 1024

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate
        self.recording: bool = False
        self.output_path: Optional[str] = None

        # Buffers: list of np.ndarray blocks, concatenated on stop.
        self._loopback_blocks: List[np.ndarray] = []
        self._mic_blocks: List[np.ndarray] = []

        self._capture_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._mic_tap: Optional[queue.Queue] = None
        self._mixer_ref: Optional["AudioMixer"] = None
        self._include_mic: bool = True
        self._lock = threading.Lock()
        self._start_time: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        output_dir: str,
        include_mic: bool = True,
        mixer: Optional["AudioMixer"] = None,
        filename: Optional[str] = None,
    ) -> str:
        """
        Begin recording. Returns the planned output file path.

        - `output_dir`: folder where the MP3 will be saved (created if missing).
        - `include_mic`: if True and `mixer` is provided, the user's mic is
          mixed into the recording.
        - `mixer`: optional AudioMixer to tap mic from.
        - `filename`: optional explicit filename. Defaults to a timestamped
          name like `discord_20260501_143022.mp3`.
        """
        if self.recording:
            raise RuntimeError("Recorder is already running.")
        if not SOUNDCARD_AVAILABLE:
            raise RuntimeError(
                "The 'soundcard' library is not installed. " "Run: pip install soundcard"
            )

        import time as _time

        # Locate the default speaker as a loopback microphone.
        try:
            speaker = _sc.default_speaker()  # type: ignore[union-attr]
            loop_mic = _sc.get_microphone(  # type: ignore[union-attr]
                speaker.name, include_loopback=True
            )
        except Exception as e:
            raise RuntimeError(f"Could not find default playback device: {e}")

        # 48 kHz works on essentially all modern devices and matches the rest
        # of the app's audio pipeline.
        self.sample_rate = 48000

        os.makedirs(output_dir, exist_ok=True)
        if filename is None:
            timestamp = _time.strftime("%Y%m%d_%H%M%S")
            filename = f"discord_{timestamp}.mp3"
        if not filename.lower().endswith(".mp3"):
            filename += ".mp3"
        self.output_path = os.path.join(output_dir, filename)

        self._loopback_blocks = []
        self._mic_blocks = []
        self._include_mic = include_mic and mixer is not None
        self._mixer_ref = mixer
        self._stop_event.clear()

        # Attach mic tap BEFORE starting capture so we don't miss frames.
        if self._include_mic and mixer is not None:
            self._mic_tap = queue.Queue(maxsize=512)
            mixer._recording_tap = self._mic_tap

        # Start the capture thread (soundcard's API is blocking, not callback)
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            args=(loop_mic,),
            name="RecorderLoopback",
            daemon=True,
        )
        self.recording = True
        self._start_time = _time.time()
        self._capture_thread.start()

        logger.info("Recording started → %s", self.output_path)
        return self.output_path

    def _capture_loop(self, loop_mic):
        """Background thread: pull blocks from the loopback mic until stopped."""
        # soundcard uses COM under the hood and requires CoInitialize on
        # whatever thread opens a stream. Without this we get HRESULT
        # 0x800401f0 (CO_E_NOTINITIALIZED).
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED=0
        except Exception:
            pass
        try:
            with loop_mic.recorder(
                samplerate=self.sample_rate, channels=2, blocksize=self._CAPTURE_FRAMES
            ) as rec:
                while not self._stop_event.is_set():
                    try:
                        data = rec.record(numframes=self._CAPTURE_FRAMES)
                    except Exception as e:
                        logger.error("Loopback record error: %s", e)
                        break
                    # data shape: (frames, channels) float32 in [-1, 1]
                    self._loopback_blocks.append(np.asarray(data, dtype=np.float32))

                    # Drain mic tap so mic stays roughly synced with loopback.
                    if self._mic_tap is not None:
                        drained = 0
                        while drained < 8:
                            try:
                                m = self._mic_tap.get_nowait()
                            except queue.Empty:
                                break
                            self._mic_blocks.append(m)
                            drained += 1
        except Exception as e:
            logger.error("Recorder capture loop crashed: %s", e)
            self.recording = False
        finally:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass

    def _detach_mic_tap(self):
        if self._mixer_ref is not None and self._mic_tap is not None:
            try:
                self._mixer_ref._recording_tap = None
            except Exception:
                pass
        self._mic_tap = None

    def stop(self) -> Optional[str]:
        """
        Stop recording. Captured audio is written to disk in a background
        thread so the UI never freezes (MP3 encoding can take a few seconds
        for long recordings). Returns the planned output path immediately
        (the file may not exist yet — encoding is async).
        """
        if not self.recording:
            return None

        self.recording = False
        self._stop_event.set()

        # Wait briefly for capture thread to exit cleanly
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=2.0)
            self._capture_thread = None

        # Detach mic tap IMMEDIATELY so the mixer's input callback stops
        # touching our queue, regardless of how long encoding takes.
        self._detach_mic_tap()

        # Snapshot blocks and clear the buffers on this (UI) thread so the
        # recorder is fully reset before encoding even starts.
        loopback_blocks = self._loopback_blocks
        mic_blocks = self._mic_blocks
        include_mic = self._include_mic
        out_path = self.output_path
        sample_rate = self.sample_rate
        self._loopback_blocks = []
        self._mic_blocks = []

        if not loopback_blocks:
            logger.warning("Recorder stopped with no captured audio.")
            return None

        # Encode in a background thread so the UI thread (and the audio
        # callback's chance at the GIL) is freed immediately.
        threading.Thread(
            target=self._encode_async,
            args=(loopback_blocks, mic_blocks, include_mic, out_path, sample_rate),
            name="RecorderEncoder",
            daemon=True,
        ).start()

        return out_path

    def _encode_async(
        self,
        loopback_blocks: List[np.ndarray],
        mic_blocks: List[np.ndarray],
        include_mic: bool,
        out_path: Optional[str],
        sample_rate: int,
    ) -> Optional[str]:
        """Background-thread encoding job. Writes the MP3 (or WAV fallback)."""
        try:
            loopback = np.concatenate(loopback_blocks, axis=0)
        except Exception as e:
            logger.error("Failed to concatenate loopback audio: %s", e)
            return None

        mic_audio: Optional[np.ndarray] = None
        if include_mic and mic_blocks:
            try:
                mic_audio = np.concatenate(mic_blocks, axis=0)
            except Exception as e:
                logger.warning("Failed to concatenate mic audio: %s", e)
                mic_audio = None

        if loopback.ndim == 2:
            mono = loopback.mean(axis=1).astype(np.float32)
        else:
            mono = loopback.astype(np.float32)

        if mic_audio is not None and mic_audio.size > 0:
            n = mono.shape[0]
            if mic_audio.shape[0] < n:
                mic_audio = np.pad(mic_audio, (0, n - mic_audio.shape[0]))
            else:
                mic_audio = mic_audio[:n]
            mono = mono + mic_audio.astype(np.float32)

        np.tanh(mono, out=mono)
        mono = np.clip(mono, -1.0, 1.0)

        if not PYDUB_AVAILABLE:
            wav_path = (out_path or "recording.mp3").rsplit(".", 1)[0] + ".wav"
            try:
                sf.write(wav_path, mono, sample_rate, subtype="PCM_16")
                logger.warning("pydub not available; wrote WAV instead: %s", wav_path)
                return wav_path
            except Exception as e:
                logger.error("Failed to write fallback WAV: %s", e)
                return None

        try:
            int16 = (mono * 32767.0).astype(np.int16)
            seg = AudioSegment(
                int16.tobytes(),
                frame_rate=int(sample_rate),
                sample_width=2,
                channels=1,
            )
            assert out_path is not None
            seg.export(out_path, format="mp3", bitrate=self.DEFAULT_BITRATE)
            logger.info("Recording saved: %s", out_path)
            return out_path
        except Exception as e:
            logger.error("Failed to encode MP3: %s", e)
            try:
                wav_path = (out_path or "recording.mp3").rsplit(".", 1)[0] + ".wav"
                sf.write(wav_path, mono, sample_rate, subtype="PCM_16")
                logger.warning("MP3 encode failed; wrote WAV instead: %s", wav_path)
                return wav_path
            except Exception as e2:
                logger.error("Fallback WAV also failed: %s", e2)
                return None

    def get_elapsed(self) -> float:
        """Seconds since recording started (0 if not recording)."""
        if not self.recording:
            return 0.0
        import time as _time

        return _time.time() - self._start_time
