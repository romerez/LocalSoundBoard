"""
Audio mixing and playback for the Discord Soundboard.
"""

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

# Try to import pydub for extended format support (M4A, AAC, WMA, etc.)
try:
    from pydub import AudioSegment

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

        # Formats that soundfile handles well
        soundfile_formats = {".wav", ".flac", ".ogg", ".aiff", ".aif"}

        # Try soundfile first for known supported formats
        if ext in soundfile_formats:
            data, sr = sf.read(file_path, dtype="float32")
            return data, sr

        # Try soundfile for other formats (MP3 support varies)
        try:
            data, sr = sf.read(file_path, dtype="float32")
            return data, sr
        except Exception:
            pass

        # Fallback to pydub for M4A, AAC, WMA, WebM, etc.
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
        with self._lock:
            if file_path in self._cache:
                return self._cache[file_path].copy()

        # Not in cache, try to load
        return self._load_into_cache(file_path).copy()

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


class AudioMixer:
    """
    Handles real-time audio processing.

    Captures microphone input, mixes it with sound effects,
    and outputs to a virtual audio device.
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
        self.stream = None
        self.sound_queue: queue.Queue = queue.Queue()
        self.currently_playing: List[Dict] = []
        self.lock = threading.Lock()

        # Mic settings
        self.mic_volume = 1.0
        self.mic_muted = False

    def start(self):
        """Begin audio stream."""
        if self.running:
            return

        self.running = True
        self.stream = sd.Stream(
            device=(self.input_device, self.output_device),
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            channels=(1, self.channels),
            callback=self._audio_callback,
            dtype=np.float32,
        )
        self.stream.start()

    def stop(self):
        """End audio stream."""
        self.running = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def _audio_callback(self, indata, outdata, frames, time, status):
        """
        Real-time audio mixing callback.

        Called by sounddevice for each audio block.
        Keep this minimal - no blocking operations!
        """
        # Process microphone input
        if self.mic_muted:
            mixed = np.zeros((frames, self.channels), dtype=np.float32)
        else:
            mic_mono = indata[:, 0] * self.mic_volume
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

                mixed += chunk
                sound["position"] += chunk_size

            # Remove finished sounds
            for i in reversed(finished):
                self.currently_playing.pop(i)

        # Clip to prevent distortion and write to output
        np.clip(mixed, -1.0, 1.0, out=outdata)

    def play_sound(self, file_path: str, volume: float = 1.0):
        """Queue a sound for playback. Uses cache if available for better performance."""
        try:
            # Use cached audio data if available (much faster - no disk I/O)
            if self.sound_cache:
                data = self.sound_cache.get_sound_data(file_path)
                if data is not None:
                    self.sound_queue.put({"data": data, "position": 0, "volume": volume})
                    return

            # Fallback: load from disk (slower)
            data, sr = sf.read(file_path, dtype="float32")

            # Resample if needed
            if sr != self.sample_rate:
                ratio = self.sample_rate / sr
                new_length = int(len(data) * ratio)
                indices = np.linspace(0, len(data) - 1, new_length).astype(int)
                data = data[indices]

            self.sound_queue.put({"data": data, "position": 0, "volume": volume})
        except Exception as e:
            print(f"Error loading sound: {e}")

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
