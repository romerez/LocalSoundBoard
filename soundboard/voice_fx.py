"""
Real-time voice changer / microphone effects for the Discord Soundboard.

Pure-NumPy DSP applied to the LIVE microphone signal inside the audio output
callback (after RNNoise noise suppression, before the mic is mixed with sounds
and sent to the virtual cable). People on the Discord call hear the modulated
voice.

Hard requirements (this code runs in the sounddevice audio thread):
  * cheap        — total cost must stay well under one 1024-sample block
                   @ 48 kHz (~21 ms). Everything here is vectorised NumPy.
  * lock-free    — parameters are plain attributes set from the GUI thread and
                   read in the audio thread. A torn read across a parameter
                   change costs at most one glitchy block — acceptable for a
                   voice toy, and we never block the audio callback.
  * crash-proof  — ``process()`` NEVER raises. On any error it returns the
                   input unchanged so the mic keeps working.
  * length-safe  — ``process()`` always returns exactly len(input) samples so
                   the caller's stereo ``column_stack`` stays aligned.

Signal chain (each stage is independently bypassable, fixed order):

    input
      → pitch shift     deep ↔ chipmunk (time-domain crossfade shifter)
      → drive           tanh overdrive / distortion
      → bitcrush        sample-rate + bit-depth reduction (lo-fi / 8-bit)
      → ring mod        robot / metallic (carrier multiply)
      → bandpass        radio / megaphone / telephone character (+ grit)
      → chorus/vibrato  modulated delay (thickening / wobble)
      → echo            feedback delay
      → reverb          Schroeder comb bank (room / hall)
      → tremolo         amplitude LFO
      → output gain + safety clamp

The pitch shifter is a classic two-tap crossfading delay-line shifter (the
same technique used by lo-fi hardware pitch pedals): two grains read from a
delay line at a ramping offset, windowed with complementary Hann windows whose
gains always sum to 1. It is O(n), needs no FFT, and has bounded latency.
"""

from __future__ import annotations

import logging
from typing import Dict, Any

import numpy as np

logger = logging.getLogger(__name__)

# SciPy is a transitive dependency of librosa (already installed). We only use
# it for the radio/megaphone biquad bandpass. If it is somehow unavailable the
# radio effect degrades to a no-op instead of crashing.
try:
    from scipy.signal import butter, lfilter, lfilter_zi  # type: ignore

    _SCIPY_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    butter = lfilter = lfilter_zi = None  # type: ignore
    _SCIPY_AVAILABLE = False


# Freeverb-style comb delays (samples). All are > 1024 (one callback block) so
# the comb feedback never reads samples produced within the current block,
# which is what lets us run each comb as a single vectorised gather instead of
# a per-sample Python recursion.
_REVERB_COMB_DELAYS = (1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617)


class VoiceChanger:
    """Stateful real-time mic effect chain. One instance per AudioMixer.

    All parameters are public attributes; set them directly from the GUI
    thread. ``enabled`` is the master on/off; each effect also has its own
    ``*_enabled`` flag so presets can mix and match.
    """

    # Presets: name -> attribute overrides. Applying a preset first resets the
    # whole chain to a clean (all-off) baseline, then sets these. The GUI shows
    # one button per preset for one-click voices.
    PRESETS: Dict[str, Dict[str, Any]] = {
        "Clean": {},
        "Deep": {"pitch_enabled": True, "pitch_semitones": -5.0},
        "Demon": {
            "pitch_enabled": True, "pitch_semitones": -8.0,
            "drive_enabled": True, "drive_amount": 0.35,
            "reverb_enabled": True, "reverb_amount": 0.45, "reverb_mix": 0.3,
        },
        "Chipmunk": {"pitch_enabled": True, "pitch_semitones": 7.0},
        "Helium": {"pitch_enabled": True, "pitch_semitones": 11.0},
        "Robot": {
            "robot_enabled": True, "robot_freq": 60.0,
            "drive_enabled": True, "drive_amount": 0.2,
        },
        "Cylon": {
            "robot_enabled": True, "robot_freq": 110.0,
            "tremolo_enabled": True, "tremolo_rate": 18.0, "tremolo_depth": 0.5,
        },
        "Radio": {
            "radio_enabled": True, "radio_low": 350.0, "radio_high": 3000.0,
            "radio_grit": 0.35,
        },
        "Megaphone": {
            "radio_enabled": True, "radio_low": 500.0, "radio_high": 4000.0,
            "radio_grit": 0.6, "drive_enabled": True, "drive_amount": 0.3,
        },
        "Telephone": {
            "radio_enabled": True, "radio_low": 300.0, "radio_high": 3400.0,
            "radio_grit": 0.2,
        },
        "Alien": {
            "pitch_enabled": True, "pitch_semitones": 3.0,
            "robot_enabled": True, "robot_freq": 175.0,
            "chorus_enabled": True, "chorus_depth": 8.0, "chorus_rate": 1.5,
            "chorus_mix": 0.5,
        },
        "Underwater": {
            "radio_enabled": True, "radio_low": 120.0, "radio_high": 900.0,
            "chorus_enabled": True, "chorus_depth": 10.0, "chorus_rate": 0.6,
            "chorus_mix": 0.6, "chorus_vibrato": True,
        },
        "Cave": {
            "echo_enabled": True, "echo_time": 0.28, "echo_feedback": 0.45,
            "echo_mix": 0.45,
            "reverb_enabled": True, "reverb_amount": 0.7, "reverb_mix": 0.45,
        },
        "Ghost": {
            "pitch_enabled": True, "pitch_semitones": -3.0,
            "reverb_enabled": True, "reverb_amount": 0.8, "reverb_mix": 0.55,
            "tremolo_enabled": True, "tremolo_rate": 4.0, "tremolo_depth": 0.4,
        },
        "Drunk": {
            "chorus_enabled": True, "chorus_depth": 14.0, "chorus_rate": 0.8,
            "chorus_mix": 0.7, "chorus_vibrato": True,
        },
        "8-Bit": {
            "crush_enabled": True, "crush_bits": 6, "crush_downsample": 8,
        },
        "Stadium": {
            "echo_enabled": True, "echo_time": 0.35, "echo_feedback": 0.5,
            "echo_mix": 0.4,
            "reverb_enabled": True, "reverb_amount": 0.6, "reverb_mix": 0.4,
        },
    }

    def __init__(self, sample_rate: int = 48000, block_size: int = 1024):
        self.sr = int(sample_rate)
        self.block_size = int(block_size)

        # ---- Master + per-effect parameters (read lock-free in audio thread).
        self.enabled: bool = False
        self.preset: str = "Clean"

        # Pitch shift
        self.pitch_enabled: bool = False
        self.pitch_semitones: float = 0.0  # -12 .. +12

        # Drive / distortion
        self.drive_enabled: bool = False
        self.drive_amount: float = 0.3  # 0 .. 1

        # Bitcrush (lo-fi)
        self.crush_enabled: bool = False
        self.crush_bits: int = 6        # 1 .. 16
        self.crush_downsample: int = 6  # 1 .. 50 (sample & hold factor)

        # Ring modulation (robot)
        self.robot_enabled: bool = False
        self.robot_freq: float = 60.0   # Hz

        # Radio / megaphone bandpass
        self.radio_enabled: bool = False
        self.radio_low: float = 350.0   # Hz
        self.radio_high: float = 3000.0  # Hz
        self.radio_grit: float = 0.3    # 0 .. 1 post-band tanh drive

        # Chorus / vibrato (modulated delay)
        self.chorus_enabled: bool = False
        self.chorus_depth: float = 8.0  # ms peak modulation
        self.chorus_rate: float = 1.2   # Hz
        self.chorus_mix: float = 0.5    # 0 .. 1 wet
        self.chorus_vibrato: bool = False  # True = wet only (pure pitch wobble)

        # Echo / delay
        self.echo_enabled: bool = False
        self.echo_time: float = 0.3     # seconds
        self.echo_feedback: float = 0.4  # 0 .. <0.95
        self.echo_mix: float = 0.4      # 0 .. 1 wet

        # Reverb (Schroeder combs)
        self.reverb_enabled: bool = False
        self.reverb_amount: float = 0.5  # 0 .. 1 -> room size / decay
        self.reverb_mix: float = 0.35   # 0 .. 1 wet

        # Tremolo
        self.tremolo_enabled: bool = False
        self.tremolo_rate: float = 5.0  # Hz
        self.tremolo_depth: float = 0.5  # 0 .. 1

        # Output makeup gain
        self.output_gain: float = 1.0

        self._init_state()

    # ------------------------------------------------------------------ state
    def _init_state(self) -> None:
        sr = self.sr

        # Pitch shifter delay line.
        self._ps_grain = 1024  # grain length (samples) ~ 21 ms
        self._ps_tape = np.zeros(8192, dtype=np.float32)
        self._ps_wpos = 0      # absolute write counter
        self._ps_phase = 0.0   # fractional grain phase

        # Bitcrush sample & hold.
        self._crush_last = 0.0
        self._crush_counter = 0

        # Ring mod carrier phase (radians).
        self._rm_phase = 0.0

        # Radio biquad state.
        self._radio_b = None
        self._radio_a = None
        self._radio_zi = None
        self._radio_key = None  # (low, high) the current coeffs were built for

        # Chorus modulated delay line (100 ms) + LFO phase.
        self._ch_buf = np.zeros(int(sr * 0.1) + 4, dtype=np.float32)
        self._ch_wpos = 0
        self._ch_phase = 0.0

        # Echo delay line (up to 2 s) + write pos.
        self._echo_buf = np.zeros(int(sr * 2.0) + 4, dtype=np.float32)
        self._echo_wpos = 0

        # Reverb comb buffers + write positions.
        self._rev_bufs = [np.zeros(d + 1, dtype=np.float32) for d in _REVERB_COMB_DELAYS]
        self._rev_wpos = [0 for _ in _REVERB_COMB_DELAYS]

        # Tremolo LFO phase (radians).
        self._trem_phase = 0.0

    def reset(self) -> None:
        """Clear all internal buffers (e.g. when the audio stream restarts)."""
        try:
            self._init_state()
        except Exception:
            pass

    # ----------------------------------------------------------------- presets
    def apply_preset(self, name: str) -> None:
        """Reset the chain to a clean baseline, then apply the named preset.

        Does NOT touch ``enabled`` — the GUI's master toggle owns that.
        Unknown names are treated as "Clean".
        """
        self._reset_effect_params()
        self.preset = name
        for attr, value in self.PRESETS.get(name, {}).items():
            setattr(self, attr, value)

    def _reset_effect_params(self) -> None:
        """Disable every effect and restore neutral parameters."""
        self.pitch_enabled = False
        self.pitch_semitones = 0.0
        self.drive_enabled = False
        self.crush_enabled = False
        self.robot_enabled = False
        self.radio_enabled = False
        self.chorus_enabled = False
        self.chorus_vibrato = False
        self.echo_enabled = False
        self.reverb_enabled = False
        self.tremolo_enabled = False
        self.output_gain = 1.0

    # ------------------------------------------------------------- main entry
    def process(self, mono: np.ndarray) -> np.ndarray:
        """Apply the enabled effect chain to a mono float32 block.

        Returns a float32 array the SAME length as the input. Never raises.
        """
        if not self.enabled:
            return mono
        try:
            x = np.ascontiguousarray(mono, dtype=np.float32)
            if x.ndim != 1:
                x = x.reshape(-1)
            n = x.shape[0]
            if n == 0:
                return mono

            if self.pitch_enabled and abs(self.pitch_semitones) > 1e-3:
                x = self._pitch(x, 2.0 ** (float(self.pitch_semitones) / 12.0))
            if self.drive_enabled:
                x = self._drive(x, float(self.drive_amount))
            if self.crush_enabled:
                x = self._bitcrush(x, int(self.crush_bits), int(self.crush_downsample))
            if self.robot_enabled:
                x = self._ringmod(x, float(self.robot_freq))
            if self.radio_enabled and _SCIPY_AVAILABLE:
                x = self._radio(x, float(self.radio_low), float(self.radio_high),
                                float(self.radio_grit))
            if self.chorus_enabled:
                x = self._chorus(x, float(self.chorus_depth), float(self.chorus_rate),
                                 float(self.chorus_mix), bool(self.chorus_vibrato))
            if self.echo_enabled:
                x = self._echo(x, float(self.echo_time), float(self.echo_feedback),
                               float(self.echo_mix))
            if self.reverb_enabled:
                x = self._reverb(x, float(self.reverb_amount), float(self.reverb_mix))
            if self.tremolo_enabled:
                x = self._tremolo(x, float(self.tremolo_rate), float(self.tremolo_depth))

            g = float(self.output_gain)
            if g != 1.0:
                x = x * g

            # Length safety: the caller stereo-stacks this with itself and adds
            # it to a `frames`-long mix, so the length MUST match the input.
            if x.shape[0] != n:
                fixed = np.zeros(n, dtype=np.float32)
                m = min(n, x.shape[0])
                fixed[:m] = x[:m]
                x = fixed

            # Internal safety clamp (the mixer's own _soft_clip still runs
            # downstream, but we keep runaway feedback in check here too).
            np.clip(x, -2.0, 2.0, out=x)
            return x.astype(np.float32, copy=False)
        except Exception as e:  # never break the audio callback
            logger.debug("VoiceChanger.process failed: %s", e)
            return mono

    # ------------------------------------------------------------ DSP stages
    def _read_interp(self, tape: np.ndarray, pos: np.ndarray) -> np.ndarray:
        """Linear-interpolated read from a ring buffer at fractional absolute
        positions ``pos``. Out-of-history (negative) reads return 0."""
        L = tape.shape[0]
        pos = np.maximum(pos, 0.0)
        f = np.floor(pos).astype(np.int64)
        frac = (pos - f).astype(np.float32)
        i0 = np.mod(f, L)
        i1 = np.mod(f + 1, L)
        return tape[i0] * (1.0 - frac) + tape[i1] * frac

    def _pitch(self, x: np.ndarray, ratio: float) -> np.ndarray:
        n = x.shape[0]
        tape = self._ps_tape
        L = tape.shape[0]
        G = float(self._ps_grain)
        half = G * 0.5
        w = self._ps_wpos

        # Write this block into the ring buffer first.
        widx = np.mod(w + np.arange(n), L)
        tape[widx] = x
        self._ps_wpos = w + n

        # Phase ramps by (1 - ratio) per output sample. ratio>1 → phase falls →
        # read delay shrinks → playback speeds up → pitch rises.
        step = 1.0 - ratio
        k = np.arange(1, n + 1, dtype=np.float64)
        ph = self._ps_phase + step * k
        self._ps_phase = float((self._ps_phase + step * n) % G)

        ph0 = np.mod(ph, G)
        ph1 = np.mod(ph + half, G)
        live = (w + np.arange(n)).astype(np.float64)
        # Base delay of one grain keeps reads strictly in the past.
        rp0 = live - G - ph0
        rp1 = live - G - ph1
        two_pi = 2.0 * np.pi
        w0 = (0.5 - 0.5 * np.cos(two_pi * ph0 / G)).astype(np.float32)
        w1 = (0.5 - 0.5 * np.cos(two_pi * ph1 / G)).astype(np.float32)
        y = self._read_interp(tape, rp0) * w0 + self._read_interp(tape, rp1) * w1
        return y.astype(np.float32)

    def _drive(self, x: np.ndarray, amount: float) -> np.ndarray:
        amount = max(0.0, min(1.0, amount))
        g = 1.0 + amount * 30.0
        # tanh soft clip; gentle makeup so heavy drive doesn't get too loud.
        return (np.tanh(x * g) * (0.85 - 0.35 * amount)).astype(np.float32)

    def _bitcrush(self, x: np.ndarray, bits: int, downsample: int) -> np.ndarray:
        bits = max(1, min(16, int(bits)))
        levels = float(2 ** bits)
        half = levels / 2.0
        y = np.round(x * half) / half

        ds = max(1, int(downsample))
        if ds > 1:
            n = y.shape[0]
            gi = self._crush_counter + np.arange(n)
            take = (gi % ds) == 0
            # Forward-fill held samples: src[k] = index of the most recent
            # "take" position at or before k, or -1 if none yet this block.
            src = np.where(take, np.arange(n), -1)
            np.maximum.accumulate(src, out=src)
            held = np.where(src >= 0, y[np.clip(src, 0, None)], np.float32(self._crush_last))
            self._crush_last = float(held[-1])
            self._crush_counter = (self._crush_counter + n) % max(ds, 1)
            y = held.astype(np.float32)
        return y.astype(np.float32)

    def _ringmod(self, x: np.ndarray, freq: float) -> np.ndarray:
        n = x.shape[0]
        sr = self.sr
        inc = 2.0 * np.pi * max(0.0, freq) / sr
        ph = self._rm_phase + inc * np.arange(n)
        carrier = np.cos(ph).astype(np.float32)
        self._rm_phase = float((self._rm_phase + inc * n) % (2.0 * np.pi))
        return (x * carrier).astype(np.float32)

    def _radio(self, x: np.ndarray, low: float, high: float, grit: float) -> np.ndarray:
        if not _SCIPY_AVAILABLE:
            return x
        nyq = self.sr * 0.5
        low = max(20.0, min(nyq - 200.0, low))
        high = max(low + 100.0, min(nyq - 50.0, high))
        key = (round(low, 1), round(high, 1))
        if key != self._radio_key or self._radio_b is None:
            try:
                b, a = butter(2, [low / nyq, high / nyq], btype="band")
                self._radio_b = b
                self._radio_a = a
                self._radio_zi = lfilter_zi(b, a).astype(np.float64) * 0.0
                self._radio_key = key
            except Exception:
                return x
        try:
            y, self._radio_zi = lfilter(self._radio_b, self._radio_a, x, zi=self._radio_zi)
        except Exception:
            return x
        y = y.astype(np.float32)
        grit = max(0.0, min(1.0, grit))
        if grit > 0.0:
            y = np.tanh(y * (1.0 + grit * 4.0)).astype(np.float32)
        return y

    def _chorus(self, x: np.ndarray, depth_ms: float, rate: float, mix: float,
                vibrato: bool) -> np.ndarray:
        n = x.shape[0]
        sr = self.sr
        buf = self._ch_buf
        L = buf.shape[0]
        w = self._ch_wpos

        # Write dry input into the modulated delay line.
        widx = np.mod(w + np.arange(n), L)
        buf[widx] = x
        self._ch_wpos = w + n

        depth = max(0.0, depth_ms) * 0.001 * sr  # peak modulation in samples
        center = depth + 2.0                      # keep delay strictly positive
        inc = 2.0 * np.pi * max(0.0, rate) / sr
        ph = self._ch_phase + inc * np.arange(n)
        self._ch_phase = float((self._ch_phase + inc * n) % (2.0 * np.pi))
        delay = center + depth * np.sin(ph)
        live = (w + np.arange(n)).astype(np.float64)
        wet = self._read_interp(buf, live - delay)

        mix = max(0.0, min(1.0, mix))
        if vibrato:
            return wet.astype(np.float32)
        return (x * (1.0 - mix) + wet * mix).astype(np.float32)

    def _echo(self, x: np.ndarray, time_s: float, feedback: float, mix: float) -> np.ndarray:
        n = x.shape[0]
        buf = self._echo_buf
        L = buf.shape[0]
        # Delay must be >= block so the read region is entirely in the past
        # (lets us vectorise instead of recursing sample by sample).
        d = int(max(self.block_size, min(L - 2, time_s * self.sr)))
        fb = max(0.0, min(0.9, feedback))
        mix = max(0.0, min(1.0, mix))
        w = self._echo_wpos

        ridx = np.mod(w + np.arange(n) - d, L)
        delayed = buf[ridx].astype(np.float32)
        out = x + mix * delayed
        widx = np.mod(w + np.arange(n), L)
        buf[widx] = x + fb * delayed
        self._echo_wpos = w + n
        return out.astype(np.float32)

    def _reverb(self, x: np.ndarray, amount: float, mix: float) -> np.ndarray:
        n = x.shape[0]
        amount = max(0.0, min(1.0, amount))
        mix = max(0.0, min(1.0, mix))
        g = 0.7 + amount * 0.27  # comb feedback (<= 0.97 for stability)
        acc = np.zeros(n, dtype=np.float32)
        for ci, d in enumerate(_REVERB_COMB_DELAYS):
            buf = self._rev_bufs[ci]
            L = buf.shape[0]
            w = self._rev_wpos[ci]
            ridx = np.mod(w + np.arange(n) - d, L)
            delayed = buf[ridx].astype(np.float32)
            widx = np.mod(w + np.arange(n), L)
            buf[widx] = x + g * delayed
            self._rev_wpos[ci] = w + n
            acc += delayed
        acc *= np.float32(1.0 / len(_REVERB_COMB_DELAYS))
        return (x * (1.0 - mix) + acc * mix).astype(np.float32)

    def _tremolo(self, x: np.ndarray, rate: float, depth: float) -> np.ndarray:
        n = x.shape[0]
        sr = self.sr
        depth = max(0.0, min(1.0, depth))
        inc = 2.0 * np.pi * max(0.0, rate) / sr
        ph = self._trem_phase + inc * np.arange(n)
        self._trem_phase = float((self._trem_phase + inc * n) % (2.0 * np.pi))
        # LFO swings between 1.0 and (1 - depth).
        lfo = (1.0 - depth * 0.5 * (1.0 - np.cos(ph))).astype(np.float32)
        return (x * lfo).astype(np.float32)

    # ----------------------------------------------------------- (de)serialize
    def to_dict(self) -> Dict[str, Any]:
        """Serialise all parameters for config persistence."""
        return {
            "enabled": self.enabled,
            "preset": self.preset,
            "pitch_enabled": self.pitch_enabled,
            "pitch_semitones": self.pitch_semitones,
            "drive_enabled": self.drive_enabled,
            "drive_amount": self.drive_amount,
            "crush_enabled": self.crush_enabled,
            "crush_bits": self.crush_bits,
            "crush_downsample": self.crush_downsample,
            "robot_enabled": self.robot_enabled,
            "robot_freq": self.robot_freq,
            "radio_enabled": self.radio_enabled,
            "radio_low": self.radio_low,
            "radio_high": self.radio_high,
            "radio_grit": self.radio_grit,
            "chorus_enabled": self.chorus_enabled,
            "chorus_depth": self.chorus_depth,
            "chorus_rate": self.chorus_rate,
            "chorus_mix": self.chorus_mix,
            "chorus_vibrato": self.chorus_vibrato,
            "echo_enabled": self.echo_enabled,
            "echo_time": self.echo_time,
            "echo_feedback": self.echo_feedback,
            "echo_mix": self.echo_mix,
            "reverb_enabled": self.reverb_enabled,
            "reverb_amount": self.reverb_amount,
            "reverb_mix": self.reverb_mix,
            "tremolo_enabled": self.tremolo_enabled,
            "tremolo_rate": self.tremolo_rate,
            "tremolo_depth": self.tremolo_depth,
            "output_gain": self.output_gain,
        }

    def load_dict(self, data: Dict[str, Any]) -> None:
        """Restore parameters from a config dict (backward-compatible)."""
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if hasattr(self, key) and not key.startswith("_"):
                try:
                    setattr(self, key, value)
                except Exception:
                    pass
