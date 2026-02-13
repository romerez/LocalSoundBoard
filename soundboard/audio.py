"""
Audio mixing and playback for the Discord Soundboard.
"""

import queue
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf

from .constants import AUDIO


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
        sample_rate: int = None,
        block_size: int = None,
    ):
        self.input_device = input_device
        self.output_device = output_device
        self.sample_rate = sample_rate or AUDIO["sample_rate"]
        self.block_size = block_size or AUDIO["block_size"]
        self.channels = AUDIO["channels"]

        self.running = False
        self.stream = None
        self.sound_queue: queue.Queue = queue.Queue()
        self.currently_playing: list = []
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
        """Queue a sound for playback."""
        try:
            data, sr = sf.read(file_path, dtype=np.float32)

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
