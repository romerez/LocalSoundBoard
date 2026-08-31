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
import collections
import itertools
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from typing import Callable, Optional, Dict, List, Tuple

from .constants import AUDIO, SOUNDS_DIR
from .voice_fx import VoiceChanger

logger = logging.getLogger(__name__)


# --- Real-time pitch-preserving time-stretch (WSOLA-lite) ---------------
# Frame size and synthesis hop for the in-callback OLA time-stretcher used
# when a sound has pitch_preserve_live=True. Hann window with 75% overlap
# (HS = FRAME/4) is COLA so no normalization is needed.
WSOLA_FRAME = 2048  # ~43ms grain at 48kHz
WSOLA_HS = 512  # synthesis hop → 75% overlap (Hann is COLA)


# ===========================================================================
# Mic noise suppression — pluggable engines, all 48 kHz / 480-sample frames.
#
#   deepfilternet  DeepFilterNet3 DNN via onnxruntime — Krisp-class, the default
#   max            DeepFilterNet + a residual gate driven by the DNN's own local
#                  SNR estimate (ducks what little survives between words)
#   classic        statistical spectral denoiser (MCRA noise tracking + decision-
#                  directed Wiener gain) — the WebRTC/Speex family, no AI, no
#                  extra dependencies, never "eats" a voice
#   rnnoise        Xiph RNNoise tiny RNN — lightest CPU, weak on non-stationary
#   gate           adaptive expander only — no denoising, just silence between
#                  words
#
# The engines run on the mixer's dedicated mic-processing thread (see
# AudioMixer._ns_worker), never inside the PortAudio callback, and they are
# built/warmed on a background thread — so enabling or switching mid-call can't
# stall the audio callback, and a missing DLL/model degrades to the next engine
# ONCE with a visible note instead of retrying (and logging) on every block.
# ===========================================================================
_NS_FRAME_SIZE = 480
_NS_SAMPLE_RATE = 48000

try:  # low-cut pre-filter (scipy is already a hard dependency via voice_fx)
    from scipy.signal import butter as _sp_butter, lfilter as _sp_lfilter  # type: ignore

    _SCIPY_SIGNAL_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    _sp_butter = _sp_lfilter = None  # type: ignore
    _SCIPY_SIGNAL_AVAILABLE = False


# --- RNNoise: bound DIRECTLY to rnnoise.dll via ctypes -----------------------
# We deliberately do NOT `import pyrnnoise`. Its package __init__ imports the
# file-conversion wrapper (`pyrnnoise.pyrnnoise`), which drags in `audiolab` ->
# `av` (libav) -> tqdm: ~70 MB of imports that only the CLI needs, and a chain
# the frozen EXE could not satisfy. There, `from pyrnnoise.rnnoise import ...`
# raised at import time, RNNOISE_AVAILABLE went False, "Light (RNNoise)"
# silently passed the RAW mic through, and the lazy loader retried + logged the
# failure on every audio block (36k log lines in one session). Binding the four
# C entry points ourselves needs only the DLL, which soundboard.spec bundles as
# pyrnnoise/rnnoise.dll.
def _rnnoise_dll_candidates() -> List[str]:
    out: List[str] = []
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        out.append(os.path.join(mei, "pyrnnoise", "rnnoise.dll"))
    try:
        import importlib.util as _ilu

        spec = _ilu.find_spec("pyrnnoise")  # locates the package WITHOUT running __init__
        if spec is not None:
            if spec.origin:
                out.append(os.path.join(os.path.dirname(spec.origin), "rnnoise.dll"))
            for loc in list(spec.submodule_search_locations or []):
                out.append(os.path.join(str(loc), "rnnoise.dll"))
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.join(here, "models", "rnnoise.dll"))
    # de-dup, keep order
    seen = set()
    return [p for p in out if p and not (p in seen or seen.add(p))]


class _RnnoiseLib:
    """ctypes binding of the four RNNoise entry points we use."""

    def __init__(self, path: str) -> None:
        lib = ctypes.CDLL(path)
        lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        lib.rnnoise_create.restype = ctypes.c_void_p
        lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        lib.rnnoise_destroy.restype = None
        lib.rnnoise_process_frame.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]
        lib.rnnoise_process_frame.restype = ctypes.c_float
        lib.rnnoise_get_frame_size.argtypes = []
        lib.rnnoise_get_frame_size.restype = ctypes.c_int
        self.lib = lib
        self.path = path
        self.frame_size = int(lib.rnnoise_get_frame_size())
        if self.frame_size <= 0 or self.frame_size > 4800:
            raise RuntimeError(f"implausible RNNoise frame size {self.frame_size}")


def _load_rnnoise() -> Tuple[Optional[_RnnoiseLib], str]:
    errors: List[str] = []
    for cand in _rnnoise_dll_candidates():
        if not os.path.exists(cand):
            continue
        try:
            return _RnnoiseLib(cand), ""
        except Exception as e:  # pragma: no cover - depends on the machine
            errors.append(f"{cand}: {e}")
    return None, ("; ".join(errors) if errors else "rnnoise.dll not found")


_RNNOISE_LIB, RNNOISE_LOAD_ERROR = _load_rnnoise()
RNNOISE_AVAILABLE = _RNNOISE_LIB is not None
_RNN_FRAME_SIZE = _RNNOISE_LIB.frame_size if _RNNOISE_LIB is not None else _NS_FRAME_SIZE
_RNN_SAMPLE_RATE = _NS_SAMPLE_RATE


# ---------------------------------------------------------------------------
# DeepFilterNet3 backend (ONNX via onnxruntime, NO PyTorch at runtime).
#
# A Krisp-class deep denoiser that — unlike RNNoise — strongly suppresses
# NON-stationary noise (keyboard, other voices, clatter). It is 48 kHz native
# (full voice band, no telephone-band muffling) and, conveniently, uses the
# SAME 480-sample frame as RNNoise, so it drops straight into the existing
# ring buffer. The combined raw-in/raw-out model carries its STFT/ERB/deep-
# filtering/ISTFT inside the graph, so per frame we just pass audio + a
# recurrent state vector and get clean audio + the next state back.
# Model: DeepFilterNet3 (Hendrik Schröter, MIT/Apache-2.0), exported to a
# single ONNX (soundboard/models/denoiser_model.onnx).
# ---------------------------------------------------------------------------
_DFN_FRAME_SIZE = 480
_DFN_STATE_SIZE = 45304
_DFN_OUTPUTS = ["enhanced_audio_frame", "new_states", "lsnr"]

try:
    import onnxruntime as _ort  # type: ignore

    ONNXRUNTIME_AVAILABLE = True
except Exception:  # pragma: no cover
    _ort = None  # type: ignore
    ONNXRUNTIME_AVAILABLE = False


def _dfn_model_path() -> str:
    """Locate the DeepFilterNet ONNX in dev (soundboard/models/) or a frozen
    PyInstaller bundle (sys._MEIPASS/soundboard/models/)."""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "models", "denoiser_model.onnx")
    if os.path.exists(cand):
        return cand
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        c2 = os.path.join(mei, "soundboard", "models", "denoiser_model.onnx")
        if os.path.exists(c2):
            return c2
    return cand


DEEPFILTERNET_AVAILABLE = ONNXRUNTIME_AVAILABLE and os.path.exists(_dfn_model_path())


class _DenoiseBackend:
    """A frame-by-frame mono denoiser. ``frame_size`` samples in → same out, at
    48 kHz. Implementations keep their own recurrent state across frames."""

    name = "none"
    frame_size = _NS_FRAME_SIZE
    #: True when the engine has its own strength knob (set_strength); engines
    #: without one get a DELAY-COMPENSATED wet/dry mix in NoiseSuppressor.
    has_native_strength = False
    #: The engine's own output delay in samples (measured on real speech by
    #: cross-correlation; all exact integers). The dry path of the wet/dry mix
    #: is delayed by this much so the two never comb-filter.
    latency_samples = 0
    #: Per-frame speech evidence in dB (DeepFilterNet's local SNR estimate) or
    #: None when the engine has no such estimate. Chained gates read this.
    last_snr_db: Optional[float] = None

    def process_frame(self, frame: np.ndarray) -> np.ndarray:  # float32[fs]->float32[fs]
        return frame

    def set_strength(self, strength: float) -> None:
        """Native strength control, 0.0 (gentle) .. 1.0 (strongest)."""

    def reset(self) -> None:
        pass

    def warmup(self) -> None:
        """Run several dummy frames so the FIRST real frames don't pay the
        graph-load + onnxruntime arena-allocation cost (the very first
        inference can be ~10x steady state). Then reset the recurrent state."""
        try:
            for _ in range(5):
                self.process_frame(np.zeros(self.frame_size, dtype=np.float32))
            self.reset()
        except Exception:
            pass

    def close(self) -> None:
        pass


class _RnnoiseBackend(_DenoiseBackend):
    """Tiny RNN (Xiph RNNoise). ~0.5% CPU, BSD-licensed, lowest latency. Weak on
    non-stationary noise — the light option. Talks to rnnoise.dll directly:
    float frames in the int16 range, in-place, returning the VAD probability."""

    name = "rnnoise"
    frame_size = _RNN_FRAME_SIZE
    latency_samples = 960  # 20 ms (measured)

    def __init__(self) -> None:
        if _RNNOISE_LIB is None:
            raise RuntimeError(f"RNNoise unavailable ({RNNOISE_LOAD_ERROR})")
        self._lib = _RNNOISE_LIB.lib
        self._st = self._lib.rnnoise_create(None)
        if not self._st:
            raise RuntimeError("rnnoise_create() returned NULL")
        self._buf = np.zeros(self.frame_size, dtype=np.float32)
        self._ptr = self._buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        self.last_vad: float = 0.0

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        if not self._st:
            return frame
        np.multiply(frame, 32768.0, out=self._buf)
        self.last_vad = float(self._lib.rnnoise_process_frame(self._st, self._ptr, self._ptr))
        return self._buf * np.float32(1.0 / 32768.0)

    def reset(self) -> None:
        if self._st:
            self._lib.rnnoise_destroy(self._st)
        self._st = self._lib.rnnoise_create(None)

    def close(self) -> None:
        st, self._st = self._st, None
        if st:
            try:
                self._lib.rnnoise_destroy(st)
            except Exception:
                pass


class _DeepFilterNetBackend(_DenoiseBackend):
    """DeepFilterNet3 via onnxruntime. Krisp-class, 48 kHz, ~4 ms/frame on CPU
    (2.5x real-time). Heavier than RNNoise but a large quality jump."""

    name = "deepfilternet"
    frame_size = _DFN_FRAME_SIZE
    has_native_strength = False   # see set_attenuation_db: the model's own limit comb-filters
    latency_samples = 1440        # 30 ms (measured)

    def __init__(self) -> None:
        if not ONNXRUNTIME_AVAILABLE:
            raise RuntimeError("onnxruntime unavailable")
        path = _dfn_model_path()
        if not os.path.exists(path):
            raise RuntimeError(f"DeepFilterNet model missing: {path}")
        so = _ort.SessionOptions()
        # Single-threaded, sequential — lowest jitter for real-time audio (ORT's
        # own recommendation); the model is small enough not to need more.
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        so.execution_mode = _ort.ExecutionMode.ORT_SEQUENTIAL
        self._sess = _ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        self._state = np.zeros(_DFN_STATE_SIZE, dtype=np.float32)
        self._atten = np.zeros(1, dtype=np.float32)  # 0 dB limit = full denoise
        self.last_snr_db = None

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        f = np.ascontiguousarray(frame, dtype=np.float32)
        enhanced, self._state, lsnr = self._sess.run(
            _DFN_OUTPUTS,
            {"input_frame": f, "states": self._state, "atten_lim_db": self._atten},
        )
        try:
            self.last_snr_db = float(lsnr.reshape(-1)[0])
        except Exception:
            self.last_snr_db = None
        return enhanced

    def set_attenuation_db(self, db: float) -> None:
        """DeepFilterNet's built-in attenuation limit - deliberately NOT used
        for the strength slider any more. Measured on real voice clips with
        this ONNX (clean-speech SI-SDR / white-noise attenuation):

            limit  0 dB (= off) -> 21.8 dB / -43 dB     limit 10 dB ->  6.3 / -10
            limit  3 dB         ->  8.1 dB /  -3 dB     limit 20 dB -> 17.3 / -20
            limit  6 dB         ->  0.3 dB /  -6 dB     limit 40 dB -> 21.8 / -38

        Any finite limit blends the raw input back in MISALIGNED with the
        model's 30 ms lookahead (the output delay jumps 1440 -> 1920 samples),
        comb-filtering the voice. The old mapping strength -> (1-s)*40 dB put
        the GUI default (85) at a 6 dB limit = clean speech at 0.3 dB SI-SDR:
        the "robotic / doubled" voice. Strength is now a delay-compensated
        wet/dry mix in NoiseSuppressor (latency_samples = 1440) and the model
        always runs unlimited.
        """
        self._atten = np.array([max(0.0, float(db))], dtype=np.float32)

    def set_strength(self, strength: float) -> None:
        # Strength lives in NoiseSuppressor's aligned wet/dry mix; the model
        # itself always runs unlimited (see set_attenuation_db).
        self._atten = np.zeros(1, dtype=np.float32)

    def reset(self) -> None:
        self._state = np.zeros(_DFN_STATE_SIZE, dtype=np.float32)
        self.last_snr_db = None


class _ResidualGate:
    """Ducks whatever a denoiser leaves between words, keyed on a speech-
    evidence signal in dB (DeepFilterNet's lsnr). Fast attack, slow release,
    per-sample gain ramps — a duck, never a hard mute, so a wrong decision costs
    a slightly quieter syllable rather than a chopped one."""

    # Measured on real speech through DFN3: pauses sit at lsnr -15..-12.7 dB
    # (p90), voiced speech well above; the slow release rides out brief dips.
    OPEN_DB = -7.0    # evidence above this -> speech, gate opens
    CLOSE_DB = -12.0  # below this -> pause, gate closes (hysteresis)

    def __init__(self) -> None:
        self._floor = 10 ** (-22.0 / 20.0)
        self._gain = 1.0
        self._open = True

    def set_strength(self, strength: float) -> None:
        s = max(0.0, min(1.0, float(strength)))
        self._floor = float(10 ** ((-10.0 - 15.0 * s) / 20.0))  # -10 .. -25 dB

    def reset(self) -> None:
        self._gain = 1.0
        self._open = True

    def apply(self, frame: np.ndarray, snr_db: Optional[float]) -> np.ndarray:
        if snr_db is None:
            return frame
        if snr_db > self.OPEN_DB:
            self._open = True
        elif snr_db < self.CLOSE_DB:
            self._open = False
        target = 1.0 if self._open else self._floor
        start = self._gain
        # ~2 frames to open, ~160 ms to close (release tail keeps word ends).
        coeff = 0.5 if target > start else 0.06
        self._gain = start + coeff * (target - start)
        if abs(self._gain - 1.0) < 1e-4 and abs(start - 1.0) < 1e-4:
            return frame
        return frame * np.linspace(start, self._gain, len(frame), dtype=np.float32)


class _MaxBackend(_DenoiseBackend):
    """DeepFilterNet followed by the residual gate = the strongest setting.
    The DNN removes noise under and around the voice; the gate then silences
    the faint residue in pauses (keyboard burbles, other voices)."""

    name = "max"
    has_native_strength = False  # DNN part: aligned wet/dry in NoiseSuppressor

    def __init__(self) -> None:
        self._dfn = _DeepFilterNetBackend()
        self._gate = _ResidualGate()
        self.frame_size = self._dfn.frame_size
        self.latency_samples = self._dfn.latency_samples
        self.last_snr_db = None

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        y = self._dfn.process_frame(frame)
        self.last_snr_db = self._dfn.last_snr_db
        return self._gate.apply(y, self.last_snr_db)

    def set_strength(self, strength: float) -> None:
        self._dfn.set_strength(strength)
        self._gate.set_strength(strength)

    def reset(self) -> None:
        self._dfn.reset()
        self._gate.reset()
        self.last_snr_db = None

    def close(self) -> None:
        self._dfn.close()


class _SpectralBackend(_DenoiseBackend):
    """'Classic' statistical denoiser — no neural net, NumPy only. The same
    family as WebRTC / Speex noise suppression:

      * 960-sample sqrt-Hann STFT, 50% overlap (10 ms hop, 10 ms latency)
      * noise PSD via MCRA (Cohen 2002): smoothed periodogram, 1 s minimum
        tracking, per-bin speech-presence probability slows the noise update
        while you talk
      * decision-directed a-priori SNR (Ephraim-Malah) -> Wiener gain, floored
        (strength sets the floor: -8 dB gentle .. -32 dB strong) and smoothed
        across frequency so it doesn't twinkle ("musical noise")

    Strong on stationary noise (fans, AC, PC hum, hiss, static), transparent to
    the voice, deterministic and ~0.1 ms/frame. Weak on keyboard/other voices —
    that is what the DNN engines are for.
    """

    name = "classic"
    frame_size = _NS_FRAME_SIZE
    has_native_strength = True
    latency_samples = _NS_FRAME_SIZE  # 10 ms: one hop of the 50%-overlap STFT

    _ALPHA_S = 0.8   # periodogram smoothing
    _ALPHA_D = 0.95  # noise PSD update rate when speech is absent
    _ALPHA_P = 0.2   # speech-presence smoothing
    _DELTA = 5.0     # P / Pmin ratio that counts as speech
    _L_MIN = 96      # minimum-tracking window, in hops (~1 s)
    _A_DD = 0.96     # decision-directed a-priori SNR smoothing

    def __init__(self) -> None:
        n = 2 * self.frame_size
        self._n = n
        self._nb = n // 2 + 1
        # Periodic sqrt-Hann: analysis * synthesis = Hann, which sums to 1 at
        # 50% overlap (COLA), so no renormalisation is needed.
        self._win = np.sqrt(np.hanning(n + 1)[:-1]).astype(np.float32)
        self._floor = 10 ** (-32.0 / 20.0)
        self.reset()

    def reset(self) -> None:
        n = self._n
        self._in = np.zeros(n, dtype=np.float32)
        self._ola = np.zeros(n, dtype=np.float32)
        self._P: Optional[np.ndarray] = None
        self._Pmin: Optional[np.ndarray] = None
        self._Ptmp: Optional[np.ndarray] = None
        self._Npsd: Optional[np.ndarray] = None
        self._p = np.zeros(self._nb, dtype=np.float32)
        self._G = np.ones(self._nb, dtype=np.float32)
        self._prev_X2 = np.zeros(self._nb, dtype=np.float32)
        self._hops = 0
        self._age = 0

    def set_strength(self, strength: float) -> None:
        s = max(0.0, min(1.0, float(strength)))
        self._floor = float(10 ** ((-8.0 - 24.0 * s) / 20.0))

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        H = self.frame_size
        buf = self._in
        buf[:-H] = buf[H:]
        buf[-H:] = frame
        X = np.fft.rfft(buf * self._win)
        X2 = (X.real * X.real + X.imag * X.imag).astype(np.float32) + np.float32(1e-12)
        if self._P is None:
            self._P = X2.copy()
            self._Pmin = X2.copy()
            self._Ptmp = X2.copy()
            self._Npsd = X2.copy()
        P = self._P
        P *= self._ALPHA_S
        P += (1.0 - self._ALPHA_S) * X2
        # Minimum statistics: continuous minimum plus a window-reset buffer so
        # the floor can rise again when the room gets louder.
        np.minimum(self._Pmin, P, out=self._Pmin)
        np.minimum(self._Ptmp, P, out=self._Ptmp)
        self._hops += 1
        self._age += 1
        if self._hops >= self._L_MIN:
            self._hops = 0
            np.minimum(self._Ptmp, P, out=self._Pmin)
            self._Ptmp[:] = P
        # Speech presence -> how fast the noise estimate may follow the input.
        speech = (P > self._DELTA * self._Pmin).astype(np.float32)
        p = self._p
        p *= self._ALPHA_P
        p += (1.0 - self._ALPHA_P) * speech
        # Warm start: for the first second after reset let the noise estimate
        # converge fast (the minimum tracker has no history yet), then settle
        # to the slow, speech-protected rate.
        a_d = self._ALPHA_D if self._age >= self._L_MIN else 0.85
        alpha_d = a_d + (1.0 - a_d) * p
        Npsd = self._Npsd
        Npsd *= alpha_d
        Npsd += (1.0 - alpha_d) * X2
        # Decision-directed a-priori SNR -> Wiener gain.
        gamma = X2 / Npsd
        xi = self._A_DD * (self._G * self._G * self._prev_X2 / Npsd) + (1.0 - self._A_DD) * np.maximum(
            gamma - 1.0, 0.0
        )
        G = xi / (1.0 + xi)
        Gs = G.copy()
        Gs[1:-1] = 0.25 * G[:-2] + 0.5 * G[1:-1] + 0.25 * G[2:]
        np.maximum(Gs, self._floor, out=Gs)
        self._G = Gs.astype(np.float32, copy=False)
        self._prev_X2 = X2
        y = (np.fft.irfft(X * Gs, n=self._n).astype(np.float32)) * self._win
        ola = self._ola
        ola += y
        out = ola[:H].copy()
        ola[:-H] = ola[H:]
        ola[-H:] = 0.0
        return out


class _NoiseGateBackend(_DenoiseBackend):
    """Adaptive noise gate / expander — no model, no deps, works everywhere.

    Not a denoiser: it estimates the room's noise floor from quiet frames and
    smoothly DUCKS the mic when you are not talking, so background chatter,
    fans and hum vanish between words. During speech it is transparent
    (background bleeds through, but under your voice it is far less audible).
    Uses a 2-band split so a low-frequency rumble alone can't hold the gate
    open.
    """

    name = "gate"
    frame_size = _NS_FRAME_SIZE  # 480 @ 48k, ~10 ms
    has_native_strength = True

    def __init__(self) -> None:
        self.reset()
        # Reduction depth: how far the gate ducks the background (linear gain).
        # 0.06 ≈ -24 dB. Driven by strength (1.0 -> full depth).
        self._floor_gain = 0.06

    def reset(self) -> None:
        self._noise = -1.0          # noise-floor estimate (RMS); <0 = seed from 1st frame
        self._gain = 0.0            # smoothed gate gain, ramps 0..1
        self._open = False          # hysteresis latch

    def set_depth(self, floor_gain: float) -> None:
        self._floor_gain = float(max(0.0, min(1.0, floor_gain)))

    def set_strength(self, strength: float) -> None:
        # dB-linear depth: strength 0 -> -6 dB duck, 1.0 -> -36 dB (near mute).
        s = max(0.0, min(1.0, float(strength)))
        self.set_depth(10.0 ** (-(6.0 + 30.0 * s) / 20.0))

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        f = np.ascontiguousarray(frame, dtype=np.float32)
        # Level detector on the voice band only: subtracting a 128-sample
        # moving average is a ~165 Hz low-cut (first null 375 Hz), so a 50/60/
        # 120 Hz hum or desk rumble can't hold the gate open. (The old 8-sample
        # average cut everything below ~3 kHz, so under broadband noise the
        # detector saw the hiss, not the voice, and the gate closed ON speech.)
        hp = f - _moving_avg(f, 128)
        rms = float(np.sqrt(np.mean(hp * hp)) + 1e-9)
        # Track the noise floor: seed from the first frame, fall fast toward
        # quiet, rise at a moderate pace while the gate is closed (the room got
        # louder) and only very slowly while it is open (that's the voice).
        if self._noise < 0.0:
            self._noise = rms
        elif rms < self._noise:
            self._noise += 0.1 * (rms - self._noise)
        elif not self._open:
            self._noise += 0.05 * (rms - self._noise)
        else:
            self._noise += 0.002 * (rms - self._noise)
        open_thr = self._noise * 3.0      # +9.5 dB over floor -> open
        close_thr = self._noise * 1.8     # +5 dB -> close (hysteresis)
        if rms > open_thr:
            self._open = True
        elif rms < close_thr:
            self._open = False
        target = 1.0 if self._open else self._floor_gain
        # Fast attack (open in ~1 frame), slow release (tail ~60 ms).
        coeff = 0.6 if target >= self._gain else 0.12
        self._gain += coeff * (target - self._gain)
        return f * self._gain


def _moving_avg(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return x
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = (c[k:] - c[:-k]) / k
    pad = k // 2
    return np.concatenate((np.full(pad, out[0]), out, np.full(len(x) - len(out) - pad, out[-1])))


class _LowCut:
    """2nd-order Butterworth high-pass with carried state — strips desk rumble,
    handling thumps and plosive booms before the denoiser sees them."""

    def __init__(self, sample_rate: int, cutoff_hz: float = 80.0) -> None:
        if not _SCIPY_SIGNAL_AVAILABLE:
            raise RuntimeError("scipy.signal unavailable")
        self._b, self._a = _sp_butter(2, cutoff_hz, btype="highpass", fs=sample_rate)
        self._zi = np.zeros(max(len(self._a), len(self._b)) - 1, dtype=np.float64)

    def reset(self) -> None:
        self._zi[:] = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self._zi = _sp_lfilter(self._b, self._a, x, zi=self._zi)
        return y.astype(np.float32, copy=False)


# --- Engine catalogue (shared with the GUI) --------------------------------
NS_BACKEND_ORDER: Tuple[str, ...] = ("deepfilternet", "max", "classic", "rnnoise", "gate")
NS_BACKEND_LABELS: Dict[str, str] = {
    "deepfilternet": "Best (DeepFilterNet)",
    "max": "Max (DeepFilterNet + gate)",
    "classic": "Classic (spectral, no AI)",
    "rnnoise": "Light (RNNoise)",
    "gate": "Gate only",
}
NS_BACKEND_BLURBS: Dict[str, str] = {
    "deepfilternet": "AI denoiser — keyboard, other voices, fans. ~4 ms/frame.",
    "max": "AI denoiser + silences the residue between words. Strongest.",
    "classic": "Statistical filter — fans, hum, hiss. Most natural voice, no AI.",
    "rnnoise": "Tiny RNN — lowest CPU/latency, weak on keyboard & voices.",
    "gate": "No denoising: only mutes the mic between words.",
}
# When the chosen engine can't load, degrade along these routes (once, with a
# visible note) rather than to raw passthrough.
_NS_FALLBACKS: Dict[str, Tuple[str, ...]] = {
    "deepfilternet": ("classic", "rnnoise", "gate"),
    "max": ("deepfilternet", "classic", "rnnoise", "gate"),
    "classic": ("gate",),
    "rnnoise": ("classic", "gate"),
    "gate": (),
}


def ns_backend_available(name: str) -> bool:
    if name in ("deepfilternet", "max"):
        return DEEPFILTERNET_AVAILABLE
    if name == "rnnoise":
        return RNNOISE_AVAILABLE
    return name in ("classic", "gate")


def ns_available_backends() -> List[str]:
    """Engine names that can load on this machine, best first. Never empty:
    'classic' and 'gate' are pure NumPy."""
    return [n for n in NS_BACKEND_ORDER if ns_backend_available(n)]


def ns_unavailable_reason(name: str) -> str:
    if name in ("deepfilternet", "max"):
        if not ONNXRUNTIME_AVAILABLE:
            return "onnxruntime not installed"
        if not os.path.exists(_dfn_model_path()):
            return "denoiser_model.onnx missing"
        return ""
    if name == "rnnoise":
        return "" if RNNOISE_AVAILABLE else f"rnnoise.dll not loadable ({RNNOISE_LOAD_ERROR})"
    return ""


def _default_backend_name() -> str:
    """Best available backend: DeepFilterNet if its model + onnxruntime are
    present, else classic (always loadable)."""
    avail = ns_available_backends()
    return avail[0] if avail else "classic"


def _build_ns_backend(name: str) -> _DenoiseBackend:
    if name == "deepfilternet":
        return _DeepFilterNetBackend()
    if name == "max":
        return _MaxBackend()
    if name == "classic":
        return _SpectralBackend()
    if name == "rnnoise":
        return _RnnoiseBackend()
    if name == "gate":
        return _NoiseGateBackend()
    raise ValueError(f"unknown denoiser backend {name!r}")


class NoiseSuppressor:
    """Real-time mic noise suppressor with a PLUGGABLE engine.

    Replaces Discord's Krisp (which would also eat the soundboard sounds, since
    everything shares one cable) by denoising the MIC ONLY, before sounds are
    mixed in. Engines: see NS_BACKEND_LABELS. All run on 480-sample 48 kHz
    frames, so one input/output ring bridges arbitrary audio blocks (1024) to
    fixed frames; the one-frame output priming avoids the underrun buzz that
    block≠frame caused.

    Threading contract:
      * ``process()`` is called from the mixer's mic-processing thread (or, in
        tests, inline). It NEVER builds an engine — a missing engine means
        passthrough for that block.
      * Engines are built + warmed on a background thread (``ensure_backend_
        async``); ``prepare()`` builds synchronously for tests.
      * A failed engine is remembered (``status()["note"]``) and the next one
        on its fallback route is tried ONCE — no per-block retries or logging.
      * ``set_backend / set_strength / reset / lowcut`` may be called from the
        GUI thread at any time; a small lock serialises them with ``process``.
    """

    def __init__(self, sample_rate: int, block_size: int):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self._enabled: bool = False
        # Engine strength 0..1 (native knob when the engine has one, wet/dry
        # mix otherwise). 1.0 = strongest.
        self.strength: float = 1.0
        # All engines are 48 kHz native; only run when the mixer matches.
        self._matched_sr = sample_rate == _NS_SAMPLE_RATE
        self._requested: str = _default_backend_name()
        self._backend: Optional[_DenoiseBackend] = None
        self._lock = threading.Lock()
        self._gen = 0                 # bumps on every switch; stale builds are discarded
        self._building = False
        self._failed: Dict[str, str] = {}   # engine -> why it failed to load
        self._note: str = ""                # fallback / failure note for the GUI
        # Float32 rings bridging arbitrary blocks <-> fixed frames.
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(0, dtype=np.float32)
        self._primed = False
        # Raw-input history for the delay-compensated wet/dry mix.
        self._dry_hist = np.zeros(0, dtype=np.float32)
        # Optional 80 Hz low-cut ahead of the engine.
        self._lowcut_on = False
        self._lowcut: Optional[_LowCut] = None
        # Perf stats (written by the processing thread, read by the GUI).
        self.ms_last: float = 0.0
        self.ms_avg: float = 0.0
        self.ms_peak: float = 0.0
        self.blocks_processed: int = 0

    # ------------------------------------------------------------ properties
    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        if self._enabled:
            self.ensure_backend_async()

    @property
    def lowcut(self) -> bool:
        return self._lowcut_on

    @lowcut.setter
    def lowcut(self, value: bool) -> None:
        value = bool(value)
        with self._lock:
            if value and self._lowcut is None:
                try:
                    self._lowcut = _LowCut(self.sample_rate)
                except Exception as e:
                    logger.warning("Low-cut filter unavailable: %s", e)
                    self._lowcut = None
                    value = False
            if self._lowcut is not None:
                self._lowcut.reset()
            self._lowcut_on = value

    @property
    def backend_name(self) -> str:
        """The REQUESTED engine (what the user picked)."""
        return self._requested

    @property
    def active_backend(self) -> Optional[str]:
        """The engine actually processing right now (None while loading /
        when nothing could load)."""
        b = self._backend
        return b.name if b is not None else None

    @property
    def is_loading(self) -> bool:
        return self._building

    @property
    def note(self) -> str:
        return self._note

    def available_backends(self) -> list:
        """Engine names that can actually load on this machine (for the GUI)."""
        return ns_available_backends()

    # ------------------------------------------------------------- switching
    def set_backend(self, name: str, retry: bool = False) -> None:
        """Switch engine. The new engine is built on a background thread when
        NS is enabled (or on the next enable); until it lands the mic passes
        through untouched. Re-applying the current choice is a no-op, except
        with ``retry=True`` (an explicit user pick) while a fallback engine is
        standing in for a failed one - that retries the failed engine."""
        name = (name or "").strip().lower()
        if name == "none":
            name = _default_backend_name()
        if name not in NS_BACKEND_LABELS:
            return
        if name == self._requested and (self._backend is not None or self._building):
            b = self._backend
            if not (retry and b is not None and b.name != name):
                return
        with self._lock:
            old, self._backend = self._backend, None
            self._gen += 1
            self._requested = name
            self._failed.pop(name, None)
            self._note = ""
            self._reset_rings_locked()
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        if self._enabled:
            self.ensure_backend_async()

    def ensure_backend_async(self) -> None:
        """Kick off an engine build on a background thread if none is live."""
        with self._lock:
            if self._backend is not None or self._building:
                return
            self._building = True
            gen = self._gen
        t = threading.Thread(target=self._builder, args=(gen,), name="lsb-ns-build", daemon=True)
        t.start()

    def prepare(self) -> Optional[str]:
        """Build the engine synchronously (tests / eager warm-up). Returns the
        active engine name, or None if nothing could load."""
        with self._lock:
            if self._backend is not None:
                return self._backend.name
            if self._building:
                gen = None
            else:
                self._building = True
                gen = self._gen
        if gen is None:
            # A background build is in flight — wait for it (bounded).
            deadline = time.monotonic() + 10.0
            while self._building and time.monotonic() < deadline:
                time.sleep(0.01)
            return self.active_backend
        self._builder(gen)
        return self.active_backend

    def _builder(self, gen: int) -> None:
        try:
            self._build_chain(gen)
        finally:
            self._building = False

    def _build_chain(self, gen: int) -> None:
        requested = self._requested
        route = (requested,) + _NS_FALLBACKS.get(requested, ())
        for name in route:
            if self._gen != gen:
                return  # superseded by a newer set_backend()
            if name in self._failed:
                continue
            if not ns_backend_available(name):
                self._failed[name] = ns_unavailable_reason(name) or "unavailable"
                continue
            try:
                b = _build_ns_backend(name)
                b.warmup()
                b.set_strength(self.strength)
            except Exception as e:
                self._failed[name] = str(e)
                logger.warning("Denoiser engine '%s' failed to load: %s", name, e)
                continue
            with self._lock:
                if self._gen != gen or self._backend is not None:
                    stale = True
                else:
                    stale = False
                    self._backend = b
                    self._reset_rings_locked()
                    if name != requested:
                        why = self._failed.get(requested, "")
                        self._note = (
                            f"{NS_BACKEND_LABELS[requested]} unavailable"
                            + (f" ({why})" if why else "")
                            + f" → using {NS_BACKEND_LABELS[name]}"
                        )
                        logger.warning("NoiseSuppressor: %s", self._note)
                    else:
                        self._note = ""
            if stale:
                try:
                    b.close()
                except Exception:
                    pass
            return
        with self._lock:
            if self._gen == gen and self._backend is None:
                why = self._failed.get(requested, "")
                self._note = "no noise-suppression engine could load" + (f" ({why})" if why else "") + " — mic passes through unfiltered"
                logger.warning("NoiseSuppressor: %s", self._note)

    # -------------------------------------------------------------- controls
    def set_strength(self, strength: float) -> None:
        """Set suppression strength (0.0 = gentlest, 1.0 = strongest).

        Engines with a NATIVE strength control (the spectral gain floor, the
        gate depth) get it directly. The neural engines (DeepFilterNet, Max,
        RNNoise) get a DELAY-COMPENSATED wet/dry mix in process(): the dry copy
        is delayed by the engine's measured latency so nothing comb-filters,
        and the residual is dB-linear (0.5 -> noise -20 dB, 1.0 -> engine
        output only). DeepFilterNet's own attenuation limit is NOT used - see
        _DeepFilterNetBackend.set_attenuation_db for why.
        """
        self.strength = max(0.0, min(1.0, float(strength)))
        with self._lock:
            b = self._backend
            if b is not None:
                try:
                    b.set_strength(self.strength)
                except Exception:
                    pass

    def _reset_rings_locked(self) -> None:
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(0, dtype=np.float32)
        self._dry_hist = np.zeros(0, dtype=np.float32)
        self._primed = False

    def reset(self) -> None:
        """Reset internal state (e.g. when stream restarts)."""
        with self._lock:
            self._reset_rings_locked()
            if self._backend is not None:
                try:
                    self._backend.reset()
                except Exception:
                    pass
            if self._lowcut is not None:
                self._lowcut.reset()

    def close(self) -> None:
        with self._lock:
            b, self._backend = self._backend, None
            self._gen += 1
        if b is not None:
            try:
                b.close()
            except Exception:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- status
    def status(self) -> Dict[str, object]:
        b = self._backend
        return {
            "enabled": self._enabled,
            "requested": self._requested,
            "active": b.name if b is not None else None,
            "loading": self._building,
            "note": self._note,
            "lowcut": self._lowcut_on,
            "ms_avg": self.ms_avg,
            "ms_peak": self.ms_peak,
            "block_ms": 1000.0 * self.block_size / float(self.sample_rate or 48000),
            "blocks": self.blocks_processed,
        }

    def status_text(self) -> str:
        """One-line human status for the GUI."""
        if not self._enabled:
            return "Off — mic passes through untouched"
        if not self._matched_sr:
            return f"⚠ engines need 48 kHz (stream is {self.sample_rate} Hz) — passthrough"
        b = self._backend
        if b is None:
            if self._building:
                return f"Loading {NS_BACKEND_LABELS.get(self._requested, self._requested)}…"
            return "⚠ " + (self._note or "no engine loaded — mic passes through")
        label = NS_BACKEND_LABELS.get(b.name, b.name)
        block_ms = 1000.0 * self.block_size / float(self.sample_rate or 48000)
        sr = float(self.sample_rate or 48000)
        lat_ms = (int(getattr(b, "latency_samples", 0)) + b.frame_size) / sr * 1000.0
        parts = [f"✓ {label} active", f"{self.ms_avg:.1f} ms/block", f"{lat_ms:.0f} ms delay"]
        if self.ms_avg > 0.6 * block_ms:
            parts.append("⚠ heavy CPU — try Classic or Light")
        if self._lowcut_on:
            parts.append("low-cut 80 Hz")
        if self._note:
            parts.append(self._note)
        return " · ".join(parts)

    # --------------------------------------------------------------- process
    def process(self, mic_block: np.ndarray) -> np.ndarray:
        """Apply noise suppression to a mic block. Returns a same-length array.

        Safe to call when disabled or before an engine has loaded — returns the
        input unchanged in those cases (low-cut still applies when on).
        """
        if not self._enabled or not self._matched_sr:
            return mic_block
        n = len(mic_block)
        if n == 0:
            return mic_block
        t0 = time.perf_counter()
        try:
            with self._lock:
                mic_f32 = np.ascontiguousarray(mic_block, dtype=np.float32).reshape(-1)
                if self._lowcut_on and self._lowcut is not None:
                    mic_f32 = self._lowcut.process(mic_f32)
                backend = self._backend
                if backend is None:
                    return mic_f32
                fs = backend.frame_size
                # Prime the output ring with ONE frame of latency the first
                # time we run after enable/reset/switch. The block size isn't
                # a multiple of the 480-sample frame, so without this slack
                # the ring underflows every block and we'd splice raw samples
                # in at a discontinuity (the old "constant buzz / partly-
                # undenoised" bug). ~10 ms of leading silence is imperceptible
                # and is the only ring-added latency.
                if not self._primed:
                    self._out_buf = np.zeros(fs, dtype=np.float32)
                    self._primed = True
                self._in_buf = (
                    np.concatenate((self._in_buf, mic_f32)) if self._in_buf.size else mic_f32.copy()
                )
                # Drain as many full frames as we can through the engine.
                n_frames = self._in_buf.size // fs
                if n_frames > 0:
                    consumed = n_frames * fs
                    frames = self._in_buf[:consumed].reshape(n_frames, fs)
                    den_parts = [backend.process_frame(frames[i]) for i in range(n_frames)]
                    denoised_f32 = np.concatenate(den_parts).astype(np.float32, copy=False)
                    # Delay-compensated wet/dry mix for engines without a
                    # native strength control (the neural ones). The dry copy
                    # is read `latency_samples` behind so it lines up with the
                    # engine output sample-for-sample; the residual is
                    # dB-linear in strength (-40 dB * strength).
                    if not backend.has_native_strength:
                        lat = max(0, int(getattr(backend, "latency_samples", 0)))
                        hist = self._dry_hist if self._dry_hist.size else np.zeros(lat, dtype=np.float32)
                        hist = np.concatenate((hist, self._in_buf[:consumed]))
                        if self.strength < 1.0:
                            end = hist.size - lat
                            start = end - consumed
                            if start >= 0:
                                dry = hist[start:end]
                            else:  # history shorter than the delay: left-pad
                                dry = np.concatenate((np.zeros(-start, dtype=np.float32), hist[:end]))
                            r = np.float32(10.0 ** (-2.0 * self.strength))
                            denoised_f32 = denoised_f32 + r * (dry - denoised_f32)
                        keep = lat + fs
                        self._dry_hist = hist[-keep:] if hist.size > keep else hist
                    self._in_buf = self._in_buf[consumed:].copy()
                    self._out_buf = np.concatenate((self._out_buf, denoised_f32))
                # Steady state: the primed backlog guarantees >= n samples here.
                if self._out_buf.size >= n:
                    out = self._out_buf[:n].copy()
                    self._out_buf = self._out_buf[n:]
                    return out
                # Safety net (shouldn't fire after priming): pad the TAIL with
                # the freshest input so the timeline stays monotonic.
                deficit = n - self._out_buf.size
                out = np.concatenate((self._out_buf, mic_f32[-deficit:]))
                self._out_buf = np.zeros(0, dtype=np.float32)
                return out
        except Exception as e:
            # Never let noise suppression break the mic path.
            logger.debug("NoiseSuppressor.process failed: %s", e)
            return mic_block
        finally:
            dt = (time.perf_counter() - t0) * 1000.0
            self.ms_last = dt
            self.ms_avg = dt if self.blocks_processed == 0 else 0.9 * self.ms_avg + 0.1 * dt
            self.ms_peak = max(dt, self.ms_peak * 0.98)
            self.blocks_processed += 1


def _raise_thread_priority() -> None:
    """Best effort: run the mic-processing thread in the Pro Audio MMCSS class
    (what PortAudio does for its own callbacks), else just HIGHEST priority.
    Denoising must win the CPU over Tk repaints, not queue behind them."""
    if sys.platform != "win32":
        return
    try:
        task_index = ctypes.c_ulong(0)
        h = ctypes.windll.avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(task_index))
        if h:
            return
    except Exception:
        pass
    try:
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), 2)  # THREAD_PRIORITY_HIGHEST
    except Exception:
        pass


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
                f"Failed to load '{os.path.basename(file_path)}': "
                f"{clean_ffmpeg_error(str(e))}"
            )
    else:
        raise RuntimeError(
            f"Cannot load '{ext}' files. Install pydub and ffmpeg for extended format support: "
            f"pip install pydub"
        )


# ---------------------------------------------------------------------------
# Large / long-file decoding (ffmpeg, bounded memory)
#
# A soundboard clip is normally a few seconds. Fully decoding a long recording
# (e.g. a 2h50m call ≈ 2 GB as float32) into RAM is what crashed the loader.
# These helpers let the GUI work with huge files WITHOUT ever holding the whole
# thing: probe the duration instantly, decode a tiny low-rate overview just for
# the waveform, and decode only the user-selected time range at full quality.
# All of them invoke ffmpeg on the seekable file path (never a stdin pipe), so
# the "mp3 detected only with low score / two consecutive frames" misdetection
# that pydub's pipe path can hit cannot happen here.
# ---------------------------------------------------------------------------

# Files longer than this many seconds are routed through the overview / range
# decoders instead of read_audio_file (which would materialise the whole thing).
HUGE_AUDIO_SECONDS = 600  # 10 minutes (~115 MB stereo float32) — above this, stream it


def _ffmpeg_exe() -> str:
    """Path to the ffmpeg binary (bundled imageio build if present)."""
    return FFMPEG_PATH or "ffmpeg"


def _ffmpeg_run_kwargs() -> dict:
    """subprocess kwargs that keep ffmpeg from flashing a console window."""
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return kwargs


def clean_ffmpeg_error(stderr) -> str:
    """Turn ffmpeg's verbose stderr (or a pydub CouldntDecodeError message) into
    a short, human-readable reason — never the multi-screen version banner."""
    if isinstance(stderr, (bytes, bytearray)):
        text = stderr.decode("utf-8", "replace")
    else:
        text = str(stderr)
    skip = ("ffmpeg version", "built with", "configuration:", "lib", "Output from")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    meaningful = [ln for ln in lines if not ln.startswith(skip)]
    tail = (meaningful or lines)[-3:]
    return " / ".join(tail) if tail else "ffmpeg failed"


def probe_duration(file_path: str) -> float:
    """Audio duration in seconds (0.0 if unknown). Cheap — no full decode."""
    # soundfile is instant and exact for the formats it understands.
    try:
        info = sf.info(file_path)
        if info.frames and info.samplerate:
            return info.frames / float(info.samplerate)
    except Exception:
        pass
    # Otherwise parse ffmpeg's "Duration: HH:MM:SS.xx" header line.
    try:
        proc = subprocess.run([_ffmpeg_exe(), "-i", file_path], **_ffmpeg_run_kwargs())
        text = proc.stderr.decode("utf-8", "replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if m:
            h, mn, s = m.groups()
            return int(h) * 3600 + int(mn) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def decode_audio_range(
    file_path: str,
    start_s: float,
    dur_s: float,
    target_sr: int,
    channels: int = 2,
) -> Tuple[np.ndarray, int]:
    """Decode only ``[start_s, start_s+dur_s)`` from a (possibly huge) file at
    full quality. Bounded memory — ffmpeg seeks on the file, we read just that
    window. Returns (samples, sample_rate); shape (n,) for mono else (n, ch)."""
    cmd = [_ffmpeg_exe(), "-nostdin", "-v", "error"]
    if start_s and start_s > 0:
        cmd += ["-ss", f"{start_s:.6f}"]
    cmd += ["-i", file_path]
    if dur_s and dur_s > 0:
        cmd += ["-t", f"{dur_s:.6f}"]
    cmd += [
        "-vn", "-map", "a:0",
        "-ac", str(channels), "-ar", str(target_sr),
        "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ]
    proc = subprocess.run(cmd, **_ffmpeg_run_kwargs())
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(clean_ffmpeg_error(proc.stderr))
    arr = np.frombuffer(proc.stdout, dtype=np.float32).copy()  # copy → writable
    if channels > 1:
        usable = (len(arr) // channels) * channels
        arr = arr[:usable].reshape(-1, channels)
    return arr, target_sr


def decode_overview(
    file_path: str,
    max_points: int = 8000,
    duration: Optional[float] = None,
) -> Tuple[np.ndarray, int, float]:
    """Decode the whole file at a very low sample rate, purely to draw a
    coarse waveform overview for navigation. Memory ≈ a few hundred KB even for
    a multi-hour file. Returns (mono samples, low_sr, duration_seconds)."""
    if duration is None:
        duration = probe_duration(file_path)
    if duration <= 0:
        duration = 1.0
    low_sr = int(max(20, min(1000, round(max_points / duration))))
    cmd = [
        _ffmpeg_exe(), "-nostdin", "-v", "error", "-i", file_path,
        "-vn", "-map", "a:0", "-ac", "1", "-ar", str(low_sr),
        "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ]
    proc = subprocess.run(cmd, **_ffmpeg_run_kwargs())
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(clean_ffmpeg_error(proc.stderr))
    peaks = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    return peaks, low_sr, duration


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


def apply_speed(
    data: np.ndarray, speed: float, preserve_pitch: bool = True, sample_rate: int = 48000
) -> np.ndarray:
    """Render *data* at a playback *speed* — the ONE speed/pitch transform used
    by both the Discord path (AudioMixer._play_sound_sync) and the local
    preview, so what you audition is what the call hears.

    preserve_pitch=True  → librosa time-stretch (same pitch, shorter/longer);
    preserve_pitch=False → plain resample (chipmunk / deep voice).
    Speed > 1.0 = faster (shorter), < 1.0 = slower (longer). Module-level so
    it works before the stream (mixer) exists.
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
    new_sr = int(sample_rate * speed)
    result = _resample_audio(data, new_sr, sample_rate)
    return result.astype(np.float32)


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
        # On-disk cache of fully-decoded + resampled PCM. Decoding (esp. the
        # ffmpeg subprocess for OGG/MP3) and the 48 kHz resample are the slow
        # part of warming; persisting the finished float32 array lets later
        # launches skip ALL of it and just np.load the result.
        self._audio_cache_dir = Path("audio_cache")

        # Ensure sounds directory exists
        self.sounds_dir.mkdir(exist_ok=True)

    def add_sound(self, source_path: str, preload: bool = True) -> str:
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

        if preload:
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
        raw_stem = Path(original_name).stem
        stem = "".join(
            "_" if (ch in '<>:"/\\|?*' or ord(ch) < 32) else ch
            for ch in raw_stem
        ).strip(" ._")
        if not stem:
            stem = "sound"
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

    def _disk_cache_path(self, file_path: str):
        """Path of the on-disk decoded-PCM cache for ``file_path`` (or None).

        Keyed by absolute path + mtime + target sample rate, so editing or
        replacing a sound automatically invalidates its cache (new key).
        """
        try:
            p = Path(file_path)
            mtime = int(p.stat().st_mtime)
            key = hashlib.md5(
                f"{p.resolve()}|{mtime}|{self.sample_rate}".encode("utf-8")
            ).hexdigest()
            return self._audio_cache_dir / f"{key}.npy"
        except Exception:
            return None

    def _load_into_cache(self, file_path: str) -> np.ndarray:
        """Load + resample an audio file, caching the result in RAM and on disk.

        Fast path: if a decoded-PCM cache file exists on disk, np.load it (no
        decode, no ffmpeg, no resample). Slow path: decode + resample, then
        persist the result so the NEXT launch is fast. Always falls back to
        decoding if the cache is missing/corrupt — so it can never break audio.
        """
        with self._lock:
            if file_path in self._cache:
                return self._cache[file_path]

        # ---- fast path: pre-decoded PCM on disk -------------------------
        dc = self._disk_cache_path(file_path)
        if dc is not None and dc.exists():
            try:
                data = np.load(str(dc))
                with self._lock:
                    self._cache[file_path] = data
                return data
            except Exception:
                try:
                    dc.unlink()  # corrupt entry — drop and re-decode
                except OSError:
                    pass

        # ---- slow path: decode + resample, then persist ----------------
        try:
            data, sr = self._read_audio_file(file_path)

            # Resample if needed (do this once, not on every play)
            if sr != self.sample_rate:
                data = _resample_audio(data, sr, self.sample_rate)
            data = np.ascontiguousarray(data, dtype=np.float32)

            with self._lock:
                self._cache[file_path] = data

            if dc is not None:
                try:
                    self._audio_cache_dir.mkdir(exist_ok=True)
                    tmp = dc.parent / (dc.name + ".tmp")
                    with open(tmp, "wb") as fh:
                        np.save(fh, data)          # write to handle => exact name
                    os.replace(str(tmp), str(dc))  # atomic publish
                except Exception:
                    pass

            return data
        except Exception as e:
            print(f"Error loading sound into cache: {e}")
            raise

    def warm_disk_cache(self, file_path: str) -> bool:
        """Ensure the on-disk decoded-PCM cache exists WITHOUT keeping the PCM
        in RAM.

        The startup warmer keeps short sounds resident, but holding every big
        sound in memory would balloon RAM — so long files used to stay fully
        cold, and their FIRST play decoded + resampled synchronously (a
        multi-second UI freeze for multi-minute person sounds). Warming just
        the disk cache makes first play one np.load instead of a full decode.
        Files at/over HUGE_AUDIO_SECONDS are skipped — playback streams those
        and never materialises the full PCM. Returns True when the disk cache
        exists by the time we're done.
        """
        try:
            with self._lock:
                if file_path in self._cache:
                    return True  # resident → disk copy was made on load
            dc = self._disk_cache_path(file_path)
            if dc is None:
                return False
            if dc.exists():
                return True
            if probe_duration(file_path) >= HUGE_AUDIO_SECONDS:
                return False  # streamed at play time; nothing to warm
            data, sr = self._read_audio_file(file_path)
            if sr != self.sample_rate:
                data = _resample_audio(data, sr, self.sample_rate)
            data = np.ascontiguousarray(data, dtype=np.float32)
            self._audio_cache_dir.mkdir(exist_ok=True)
            tmp = dc.parent / (dc.name + ".tmp")
            with open(tmp, "wb") as fh:
                np.save(fh, data)          # write to handle => exact name
            os.replace(str(tmp), str(dc))  # atomic publish
            return True
        except Exception:
            return False

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
        # Mic-processing worker: while noise suppression is on, the input
        # callback hands raw blocks to this thread and returns immediately, so a
        # heavy engine (DeepFilterNet ~10 ms per 1024 block) can never overrun
        # the PortAudio callback. See _ns_worker / _start_ns_worker.
        self._ns_in_q: queue.Queue = queue.Queue(maxsize=16)
        self._ns_thread: Optional[threading.Thread] = None
        self._ns_stop: Optional[threading.Event] = None

        # Mic settings
        self.mic_volume = 1.0
        self.mic_muted = False

        # Master volume — multiplier applied to ALL playing sounds before
        # they're added to the mix (separate from mic_volume which only
        # affects the microphone passthrough).
        self.master_volume = 1.0

        # --- Air Deck engine state ---
        self.bed_level = 0.35                       # bed sounds duck to this under voice
        self._events = collections.deque(maxlen=256)  # ('started'|'ended'|'loop'|'paused', id, meta)
        self._instance_counter = itertools.count(1)   # unique per play, for instance-cap recycling
        self._peaks_cache = {}                        # waveform peak buckets, keyed by (id(data), n)

        # Precomputed Hann window for real-time WSOLA pitch-preserving
        # time-stretch (see _wsola_render). Built once; reused per grain.
        self._wsola_window = np.hanning(WSOLA_FRAME).astype(np.float32)

        # Noise suppression (replaces Discord's Krisp NS which is bypassed
        # when routing through the virtual cable). Disabled by default;
        # toggled via the GUI checkbox.
        self.noise_suppressor = NoiseSuppressor(self.sample_rate, self.block_size)

        # Real-time voice changer applied to the mic before it's mixed with
        # sounds and sent to the virtual cable (so people on the Discord call
        # hear the modulated voice). Disabled by default; params + presets are
        # driven from the GUI's Voice Changer card. Reads are lock-free in the
        # audio callback — see _output_callback.
        self.voice_changer = VoiceChanger(self.sample_rate, self.block_size)

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

        # --- Physical-PTT awareness -------------------------------------
        # We inject the USER'S OWN Discord PTT key for auto-PTT, so the OS
        # key state can't tell "we hold it" from "the user holds it". A
        # keyboard hook (see _register_ptt_physical_watch) tracks the
        # user's real finger: events inside our tiny injection windows are
        # ours; everything else is physical. Two bugs this kills:
        # * "PTT locked": a sound ended while the user was PHYSICALLY
        #   holding the key to talk — our injected key-UP cut Discord off,
        #   and since a held key sends no new key-downs, transmission
        #   stayed dead until they re-pressed.
        # * "people hear my mic with sounds": during auto-PTT Discord
        #   transmits the whole cable; the live mic is now ducked unless
        #   the user is actually holding the key to talk over the sound.
        self.ptt_user_physical: bool = False     # user's real finger on the key
        self._inject_window_until: float = 0.0   # our SendInput moments
        self._ptt_watch_handles: list = []       # keyboard-lib hook handles
        self.duck_mic_during_sounds: bool = True  # GUI checkbox; persisted

        # --- Universal PTT (mic gate) -------------------------------------
        # The opposite model from the Discord auto-PTT above: no key is ever
        # injected. Instead, apps use the virtual cable as their mic on plain
        # voice-activity, and the LIVE MIC only reaches the cable while the
        # user holds a global key — soundboard/universal_ptt.py flips
        # `universal_ptt_open` from its poll thread. Sounds always pass. The
        # envelope ramps open/close across one block (~21ms) so the gate
        # never clicks.
        self.universal_ptt_enabled: bool = False
        self.universal_ptt_open: bool = False
        self._u_gate_env: float = 1.0  # current gate gain (1 = mic passes)

        # Local monitoring (play sounds to speakers too)
        self.monitor_enabled = False
        self.monitor_stream = None
        self._monitor_queue: queue.Queue = queue.Queue()  # Queue for monitor audio blocks

        # Test output - plays the EXACT signal sent to the virtual cable
        # (mic + sounds, post-clip) on the default speakers so the user can
        # hear precisely what Discord receives. Different from `monitor`,
        # which only plays sounds (no mic).
        self.test_output_enabled = False
        self.test_output_stream = None
        self._test_output_queue: queue.Queue = queue.Queue(maxsize=64)

        # Test recording - captures the same final mix into an in-memory
        # buffer for a fixed duration, then the GUI plays it back so the
        # user can A/B against what Discord hears (delay, clipping, etc.).
        self._test_record_lock = threading.Lock()
        self._test_record_blocks: List[np.ndarray] = []
        self._test_record_frames_remaining: int = 0
        self.test_recording_active: bool = False
        self._test_record_done_callback: Optional[callable] = None  # type: ignore[assignment]

        # Live output levels (0..~1+) for the GUI "Discord level" meter. Updated
        # every output callback. output_peak = full signal to the cable;
        # sounds_peak = just the sound effects (excludes mic).
        self.output_peak: float = 0.0
        self.sounds_peak: float = 0.0

        # Manual PTT hold - when True, the auto-release-on-silence countdown
        # AND the safety timeout are bypassed so an external caller (e.g. the
        # Test Output panel) can keep PTT active for as long as it wants
        # without sounds actually playing.
        self.manual_ptt_hold: bool = False

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
            if self.ptt_user_physical:
                # The user already physically holds the key (they're talking)
                # — Discord is transmitting; nothing to inject. Still mark
                # active so the release countdown runs at sound end.
                self.ptt_active = True
                return
            self._inject_window_until = time.time() + 0.05
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
            if self.ptt_user_physical:
                # The user is PHYSICALLY holding the key (talking). Injecting
                # a key-up now cuts Discord off mid-sentence — and because a
                # held key sends no further key-downs, transmission stays
                # dead until they re-press: the recurring "PTT locked" bug.
                # Skip the injection; their own release ends the transmit.
                logger.debug("PTT release skipped — user physically holds %s", self.ptt_key)
            elif self.ptt_key.startswith("mouse"):
                self._inject_window_until = time.time() + 0.05
                button = self.PTT_BUTTON_MAP.get(self.ptt_key, "x2")
                _simulate_mouse_button(button, press=False)
            elif self._cached_ptt_vk is not None:
                self._inject_window_until = time.time() + 0.05
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

        # Clear stale voice-changer buffers (echo/reverb tails, pitch delay
        # line) so a restarted stream doesn't replay old audio.
        try:
            self.voice_changer.reset()
        except Exception:
            pass

        # Mic-processing worker must be up before the first input callback.
        self._start_ns_worker()

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
        self._stop_ns_worker()
        if self.monitor_stream:
            try:
                self.monitor_stream.abort()
                self.monitor_stream.close()
            except Exception:
                pass
            self.monitor_stream = None
        if self.test_output_stream:
            try:
                self.test_output_stream.abort()
                self.test_output_stream.close()
            except Exception:
                pass
            self.test_output_stream = None
            self.test_output_enabled = False
        # Cancel any in-flight test recording
        with self._test_record_lock:
            self.test_recording_active = False
            self._test_record_blocks = []
            self._test_record_frames_remaining = 0
            self._test_record_done_callback = None
        # Drop any manual PTT hold so PTT can't get stuck across restarts
        self.manual_ptt_hold = False

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

    # ------------------------------------------------------------------
    # Test output / Test recording
    # ------------------------------------------------------------------

    def set_test_output_enabled(self, enabled: bool):
        """Enable/disable live test output.

        When enabled, the EXACT signal sent to the virtual cable (mic + sounds,
        post soft-clip) is also played on the default speakers so the user can
        hear precisely what Discord receives.
        """
        if enabled and not self.test_output_stream and self.running:
            # Drain any stale data
            while not self._test_output_queue.empty():
                try:
                    self._test_output_queue.get_nowait()
                except queue.Empty:
                    break
            self.test_output_stream = sd.OutputStream(
                device=None,  # Default speakers
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                channels=self.channels,
                callback=self._test_output_callback,
                dtype=np.float32,
            )
            self.test_output_stream.start()
            self.test_output_enabled = True
        elif not enabled and self.test_output_stream:
            self.test_output_enabled = False
            try:
                self.test_output_stream.abort()
                self.test_output_stream.close()
            except Exception:
                pass
            self.test_output_stream = None

    def _test_output_callback(self, outdata, frames, time, status):
        """Output callback for test-output stream (default speakers)."""
        try:
            audio_block = self._test_output_queue.get_nowait()
            if len(audio_block) >= frames:
                outdata[:] = audio_block[:frames]
            else:
                outdata[: len(audio_block)] = audio_block
                outdata[len(audio_block) :] = 0
        except queue.Empty:
            outdata.fill(0)

    def start_test_recording(self, duration_seconds: float, on_done=None) -> bool:
        """Begin capturing the final Discord-bound mix into a buffer.

        Returns False if the mixer isn't running or a recording is already in
        progress. `on_done` (callable) is invoked from the audio thread when
        the requested duration has been captured.
        """
        if not self.running:
            return False
        with self._test_record_lock:
            if self.test_recording_active:
                return False
            self._test_record_blocks = []
            self._test_record_frames_remaining = int(duration_seconds * self.sample_rate)
            self._test_record_done_callback = on_done
            self.test_recording_active = True
        return True

    def stop_test_recording(self) -> Optional[np.ndarray]:
        """Stop test recording (if active) and return captured audio.

        Returns a `(frames, channels)` float32 array, or None if nothing was
        captured. Safe to call whether or not a recording is in progress.
        """
        with self._test_record_lock:
            self.test_recording_active = False
            self._test_record_done_callback = None
            blocks = self._test_record_blocks
            self._test_record_blocks = []
            self._test_record_frames_remaining = 0
        if not blocks:
            return None
        try:
            return np.concatenate(blocks, axis=0)
        except Exception as e:
            logger.error("Failed to concatenate test recording: %s", e)
            return None

    def hold_ptt(self) -> bool:
        """Manually press and hold the PTT key (bypasses auto-release).

        Used by Test Output to simulate a real Discord transmission so the
        user can verify the full pipeline (mic + sounds + PTT keypress) end
        to end. Returns False if no PTT key is configured.
        """
        if not self.ptt_key:
            return False
        self.manual_ptt_hold = True
        # Reset countdowns so they don't immediately fire when we release later
        self._ptt_release_countdown = 0
        self._ptt_active_cycles = 0
        if not self.ptt_active:
            self._press_ptt()
        return True

    def release_ptt_hold(self):
        """Release a manual PTT hold previously started with `hold_ptt()`.

        Always force-releases (bypasses queue) for reliability — matches the
        pattern used by `stop_all_sounds` so PTT can never get stuck.
        """
        self.manual_ptt_hold = False
        if self.ptt_active:
            self._force_release_ptt()

    def _input_callback(self, indata, frames, time, status):
        """Capture microphone input.

        With noise suppression on, the raw block is handed to the mic-processing
        worker (which denoises it and pushes it on); otherwise it goes straight
        to the mix queue. Blocks keep flowing through the worker until it has
        drained after NS is switched off, so no block can overtake another.
        """
        mic_data = indata[:, 0].copy()
        ns = self.noise_suppressor
        stop = self._ns_stop
        q = self._ns_in_q
        if stop is not None and not stop.is_set() and (ns.enabled or q.unfinished_tasks > 0):
            try:
                q.put_nowait(mic_data)
            except queue.Full:
                # Worker is badly behind - drop the oldest so we never stall.
                try:
                    q.get_nowait()
                    q.task_done()
                    q.put_nowait(mic_data)
                except (queue.Empty, queue.Full, ValueError):
                    pass
            return
        if ns.enabled:
            mic_data = ns.process(mic_data)
        self._push_mic_block(mic_data)

    def _push_mic_block(self, mic_data: np.ndarray) -> None:
        """Hand a (processed) mic block to the output mixer and, when the call
        recorder is running, to its mic tap."""
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
        # Skip the tap while the Universal PTT gate is closed - the call
        # didn't hear the user, so the recording shouldn't either.
        _gated = self.universal_ptt_enabled and not self.universal_ptt_open
        if tap is not None and not self.mic_muted and not _gated:
            try:
                tap.put_nowait((mic_data * self.mic_volume).astype(np.float32, copy=False))
            except queue.Full:
                # Drop oldest to keep recorder caught up
                try:
                    tap.get_nowait()
                    tap.put_nowait((mic_data * self.mic_volume).astype(np.float32, copy=False))
                except queue.Empty:
                    pass

    # ----------------------------------------------------- mic-processing thread
    def _start_ns_worker(self) -> None:
        """Start the dedicated mic-processing thread (idempotent)."""
        t = self._ns_thread
        if t is not None and t.is_alive():
            return
        # Drain leftovers from a previous run so unfinished_tasks starts at 0.
        while True:
            try:
                self._ns_in_q.get_nowait()
                self._ns_in_q.task_done()
            except (queue.Empty, ValueError):
                break
        stop = threading.Event()
        self._ns_stop = stop
        t = threading.Thread(
            target=self._ns_worker, args=(stop,), name="lsb-mic-ns", daemon=True
        )
        self._ns_thread = t
        t.start()

    def _stop_ns_worker(self) -> None:
        stop, self._ns_stop = self._ns_stop, None
        t, self._ns_thread = self._ns_thread, None
        if stop is not None:
            stop.set()
        if t is not None and t.is_alive() and t is not threading.current_thread():
            try:
                self._ns_in_q.put_nowait(None)  # wake it so it sees the stop flag
            except queue.Full:
                pass
            t.join(timeout=1.0)

    def _ns_worker(self, stop: threading.Event) -> None:
        """Denoise mic blocks off the audio callback. Runs at Pro-Audio / highest
        thread priority so GUI repaints can't starve it; never raises."""
        _raise_thread_priority()
        q = self._ns_in_q
        while not stop.is_set():
            try:
                blk = q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if blk is None:
                    continue  # wake-up sentinel
                ns = self.noise_suppressor
                if ns.enabled:
                    try:
                        blk = ns.process(blk)
                    except Exception as e:
                        logger.debug("mic NS worker: %s", e)
                self._push_mic_block(blk)
            finally:
                try:
                    q.task_done()
                except ValueError:
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

        # Process microphone input.
        # Duck the live mic during AUTO-PTT: when a sound auto-presses the
        # user's Discord PTT key, Discord transmits the whole cable — so
        # everyone heard the user's room/mic alongside every sound. The mic
        # stays in the mix when the user PHYSICALLY holds the PTT key
        # (deliberately talking over the sound) and during Test Output's
        # manual hold (it verifies the full pipeline).
        _duck = (
            self.duck_mic_during_sounds
            and self.ptt_active
            and not self.ptt_user_physical
            and not self.manual_ptt_hold
        )
        # Universal PTT mic gate: 1.0 when the key is held (or the feature is
        # off), 0.0 when released. The envelope ramps between targets across
        # one block so opening/closing never clicks. Env values are only ever
        # exactly 0.0 or 1.0, so the equality checks below are safe.
        if self.universal_ptt_enabled:
            _gate_target = 1.0 if self.universal_ptt_open else 0.0
        else:
            _gate_target = 1.0
        _gate_env = self._u_gate_env
        if self.mic_muted or _duck or (_gate_target == 0.0 and _gate_env == 0.0):
            mixed = np.zeros((frames, self.channels), dtype=np.float32)
            self._u_gate_env = _gate_target
        else:
            mic_mono = mic_data * self.mic_volume
            # Real-time voice changer (pitch/robot/echo/reverb/radio/etc.).
            # Applied to the mic BEFORE mixing with sounds and BEFORE the
            # virtual-cable output, so the Discord call hears the effect.
            # process() is a no-op that returns its input when disabled and
            # never raises, so this can't break the audio callback.
            vc = self.voice_changer
            if vc is not None and vc.enabled:
                mic_mono = vc.process(mic_mono)
            if _gate_env != _gate_target:
                mic_mono = mic_mono * np.linspace(
                    _gate_env, _gate_target, frames, dtype=np.float32
                )
                self._u_gate_env = _gate_target
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
            _ev = self._events
            for i, sound in enumerate(self.currently_playing):
                # Apply a pending seek (also while paused: position moves, stays paused).
                _seek = sound.get("pending_seek")
                if _seek is not None:
                    _dl = len(sound["data"])
                    sound["position"] = (max(0, min(int(_seek), _dl - 1)) if _dl > 0 else 0)
                    sound["pending_seek"] = None
                    sound.pop("wsola_buf", None)
                    sound["wsola_read"] = 0
                    sound["wsola_write"] = 0
                    sound["in_delay"] = False
                    sound["delay_position"] = 0
                    # 10 ms fade-in hides the splice (only if not already fading).
                    if sound.get("gain", 1.0) >= 1.0 and sound.get("gain_target", 1.0) >= 1.0:
                        sound["gain"] = 0.0
                        sound["gain_target"] = 1.0
                        sound["gain_step"] = 1.0 / max(1, int(0.010 * self.sample_rate))
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
                    _ev.append(("ended", sound.get("sound_id"), {"reason": "finished", "instance": sound.get("instance")}))
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

                # --- fade envelope: fast path (gain==target==1.0) skips this entirely ---
                _g = sound.get("gain", 1.0)
                _gt = sound.get("gain_target", 1.0)
                _fade_done = False
                if _g != 1.0 or _gt != 1.0:
                    _step = sound.get("gain_step", 0.0) * chunk_size
                    _g2 = min(_gt, _g + _step) if _gt >= _g else max(_gt, _g - _step)
                    _ramp = np.linspace(_g, _g2, chunk_size, dtype=np.float32)
                    chunk[:chunk_size] *= _ramp[:, None]
                    sound["gain"] = _g2
                    _fade_done = (_g2 <= 0.0 and _gt <= 0.0)

                # Add to both main mix and sounds-only mix. The block that ramps
                # to zero is mixed TOO, so the fade reaches silence with no click.
                mixed += chunk
                sounds_mix += chunk
                sound["position"] = new_pos
                if not sound.get("started", False):
                    sound["started"] = True
                    _ev.append(("started", sound.get("sound_id"), {"instance": sound.get("instance")}))
                if _fade_done:
                    if sound.get("after_fade") == "pause":
                        sound["paused"] = True
                        sound["gain"] = 0.0
                        sound["gain_target"] = 1.0
                        sound["after_fade"] = None
                        _ev.append(("paused", sound.get("sound_id"), {}))
                    else:
                        finished.append(i)
                        _ev.append(("ended", sound.get("sound_id"), {"reason": "faded", "instance": sound.get("instance")}))

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
            if self.ptt_active and not self.manual_ptt_hold:
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
        # Skipped while `manual_ptt_hold` is set (Test Output explicitly wants
        # PTT held with no sounds playing).
        if (
            self.ptt_active
            and all_sounds_finished
            and self.sound_queue.empty()
            and not self.manual_ptt_hold
        ):
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

        # Track the TRUE level of the signal sent to Discord (post-clip) so the
        # GUI can show a level meter. This is what others actually hear — it is
        # independent of the user's local speaker volume (which is why a sound
        # can sound quiet to them but loud to everyone else). Also track the
        # sounds-only level so a played sound's loudness reads clearly.
        try:
            self.output_peak = float(np.max(np.abs(outdata))) if outdata.size else 0.0
            self.sounds_peak = float(np.max(np.abs(sounds_mix))) if sounds_mix.size else 0.0
        except Exception:
            pass

        # Test output: pipe the EXACT signal sent to the virtual cable to the
        # default speakers so the user can hear what Discord hears. Capture
        # AFTER soft-clip so quality, clipping and dynamics match 1:1.
        if self.test_output_enabled:
            try:
                self._test_output_queue.put_nowait(outdata.copy())
            except queue.Full:
                pass

        # Test recording: append the same final mix to a fixed-length buffer
        # for later playback. Auto-stops when the requested duration is met.
        if self.test_recording_active:
            done_cb = None
            with self._test_record_lock:
                if self.test_recording_active and self._test_record_frames_remaining > 0:
                    take = min(frames, self._test_record_frames_remaining)
                    self._test_record_blocks.append(outdata[:take].copy())
                    self._test_record_frames_remaining -= take
                    if self._test_record_frames_remaining <= 0:
                        self.test_recording_active = False
                        done_cb = self._test_record_done_callback
                        self._test_record_done_callback = None
            # Fire the done-callback OUTSIDE the lock so it can call back into
            # the mixer (e.g. to drain the buffer) without deadlocking.
            if done_cb is not None:
                try:
                    done_cb()
                except Exception as e:
                    logger.error("Test-record done callback failed: %s", e)

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
        display_name: Optional[str] = None,
        emoji: Optional[str] = None,
        meta: Optional[dict] = None,
        fade_in_ms: int = 0,
        cue_s: float = 0.0,
        max_instances: int = 0,
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
                    file_path, volume, speed, preserve_pitch, sound_id, loop, loop_count, loop_delay,
                    display_name=display_name, emoji=emoji, meta=meta,
                    fade_in_ms=fade_in_ms, cue_s=cue_s, max_instances=max_instances,
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
            file_path, volume, speed, preserve_pitch, sound_id, loop, loop_count, loop_delay,
            display_name=display_name, emoji=emoji, meta=meta,
            fade_in_ms=fade_in_ms, cue_s=cue_s, max_instances=max_instances,
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
        display_name: Optional[str] = None,
        emoji: Optional[str] = None,
        meta: Optional[dict] = None,
        fade_in_ms: int = 0,
        cue_s: float = 0.0,
        max_instances: int = 0,
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
                        "name": display_name or (Path(file_path).stem if file_path else "Unknown"),
                        "emoji": emoji,
                        "base_speed": speed,
                        "meta": meta or {},
                        "instance": next(self._instance_counter),
                        "gain": 0.0 if fade_in_ms > 0 else 1.0,
                        "gain_target": 1.0,
                        "gain_step": (1.0 / max(1, int((fade_in_ms / 1000.0) * self.sample_rate))) if fade_in_ms > 0 else 0.0,
                        "after_fade": None,
                        "cue": max(0, int(cue_s * self.sample_rate)),
                        "pending_seek": None,
                        "bed": False,
                        "started": False,
                        "paused": False,
                    }
                    if max_instances > 0:
                        self._enforce_instance_cap(sound_id, max_instances)
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
                "name": display_name or (Path(file_path).stem if file_path else "Unknown"),
                "emoji": emoji,
                "base_speed": speed,
                "meta": meta or {},
                "instance": next(self._instance_counter),
                "gain": 0.0 if fade_in_ms > 0 else 1.0,
                "gain_target": 1.0,
                "gain_step": (1.0 / max(1, int((fade_in_ms / 1000.0) * self.sample_rate))) if fade_in_ms > 0 else 0.0,
                "after_fade": None,
                "cue": max(0, int(cue_s * self.sample_rate)),
                "pending_seek": None,
                "bed": False,
                "started": False,
                "paused": False,
            }
            if max_instances > 0:
                self._enforce_instance_cap(sound_id, max_instances)
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
        """Apply playback speed adjustment to audio data (see :func:`apply_speed`)."""
        return apply_speed(data, speed, preserve_pitch, self.sample_rate)

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

    def stop_sound(self, sound_id: str, fade_ms: int = 0):
        """Stop a specific sound by its ID.

        fade_ms == 0 keeps today's exact semantics (hard removal + queue drain +
        PTT release when empty) so the Queue scheduler, AFK, shutdown and the
        existing tests are untouched. fade_ms > 0 ramps to silence and lets the
        normal PTT countdown release ~300 ms after the tail (Deck opts in).
        """
        if fade_ms > 0:
            self.fade_out_sound(sound_id, fade_ms, then="stop")
            return
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

    def stop_all_sounds(self, fade_ms: int = 0):
        """Clear playback queue and stop all sounds.

        fade_ms == 0 is today's behaviour exactly; fade_ms > 0 fades every
        playing sound and lets the PTT countdown release after the tails.
        """
        if fade_ms > 0:
            with self.lock:
                ids = {s.get("sound_id") for s in self.currently_playing}
            for sid in ids:
                self.fade_out_sound(sid, fade_ms, then="stop")
            return
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

    def pause_sound(self, sound_id: str, fade_ms: int = 0):
        """Pause a specific sound by its ID.

        fade_ms == 0 pauses instantly (today's behaviour, kept for the Queue
        scheduler etc.); fade_ms > 0 ramps to silence first, then pauses at
        gain 0 (the Deck uses ~30 ms so pausing to talk is clickless).
        """
        if fade_ms > 0:
            self.fade_out_sound(sound_id, fade_ms, then="pause")
            return
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["paused"] = True
                    logger.debug("Paused sound: %s", sound_id)
                    break

    def resume_sound(self, sound_id: str, fade_ms: int = 0):
        """Resume a paused sound by its ID (and re-press the Discord PTT key)."""
        resumed = False
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["paused"] = False
                    if fade_ms > 0:
                        sound["gain"] = 0.0
                        sound["gain_target"] = 1.0
                        sound["gain_step"] = 1.0 / max(1, int((fade_ms / 1000.0) * self.sample_rate))
                    else:
                        sound["gain"] = 1.0
                        sound["gain_target"] = 1.0
                    sound["after_fade"] = None
                    resumed = True
                    logger.debug("Resumed sound: %s", sound_id)
                    break
        # A pause released the auto-PTT (the countdown ignores paused sounds);
        # re-press so the resumed sound actually transmits to Discord.
        if resumed:
            self._ptt_release_countdown = 0
            self._press_ptt()

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
                    sound["gain"] = 1.0
                    sound["gain_target"] = 1.0
                    sound["after_fade"] = None
                    logger.debug("Restarted sound: %s", sound_id)
                    break
        # Re-assert auto-PTT in case a prior pause released it.
        self._ptt_release_countdown = 0
        self._press_ptt()

    # ------------------------------------------------------------------
    # Air Deck engine (WP-A): fades, seek, retrigger, events, deck helpers.
    # ------------------------------------------------------------------
    def pop_events(self):
        """Drain and return the (type, sound_id, meta) events the audio callback
        appended since the last drain. Called once per GUI tick; never calls
        back on the audio thread. deque append/popleft are atomic, so no lock."""
        out = []
        ev = self._events
        while True:
            try:
                out.append(ev.popleft())
            except IndexError:
                break
        return out

    def _enforce_instance_cap(self, sound_id, max_instances):
        """Fade the OLDEST live instance of sound_id when at the cap, so a
        spammed short sound keeps the joke but not a growing wall of copies."""
        with self.lock:
            live = [s for s in self.currently_playing
                    if s.get("sound_id") == sound_id and not s.get("paused", False)]
        if len(live) < max_instances:
            return
        live.sort(key=lambda s: s.get("instance", 0))
        for s in live[:len(live) - max_instances + 1]:
            self.fade_out_sound_entry(s, 20, then="stop")

    def fade_out_sound_entry(self, sound, ms=800, then="stop"):
        """Arm a fade on ONE entry dict (already located)."""
        n = max(1, int((ms / 1000.0) * self.sample_rate))
        with self.lock:
            sound["gain_target"] = 0.0
            sound["gain"] = sound.get("gain", 1.0)
            sound["gain_step"] = 1.0 / n
            sound["after_fade"] = then

    def fade_out_sound(self, sound_id: str, ms: int = 800, then: str = "stop"):
        """Ramp every live instance of sound_id to silence over *ms*, then stop
        or pause. Fading entries stay ACTIVE so the PTT key is held through the
        tail (they finish, THEN the normal countdown releases)."""
        n = max(1, int((ms / 1000.0) * self.sample_rate))
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id and not sound.get("paused", False):
                    sound["gain_target"] = 0.0
                    sound["gain"] = sound.get("gain", 1.0)
                    sound["gain_step"] = 1.0 / n
                    sound["after_fade"] = then

    def seek_sound(self, sound_id: str, seconds: float, relative: bool = False) -> bool:
        """Queue a seek applied at the next block boundary (works while paused)."""
        hit = False
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    dl = len(sound["data"])
                    base = sound.get("position", 0) if relative else 0
                    tgt = int(base + seconds * self.sample_rate)
                    sound["pending_seek"] = (max(0, min(tgt, dl - 1)) if dl > 0 else 0)
                    hit = True
        return hit

    def set_cue(self, sound_id: str, seconds: float):
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["cue"] = max(0, int(seconds * self.sample_rate))

    def set_sound_bed(self, sound_id: str, enabled: bool):
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    sound["bed"] = bool(enabled)

    def set_effective_rate(self, sound_id: str, rate: float, preserve_pitch=None):
        """Set the EFFECTIVE playback rate, honest for speed-baked slots:
        playback_rate = clamp(rate / base_speed, 0.5, 2.0)."""
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    base = float(sound.get("base_speed", 1.0)) or 1.0
                    sound["playback_rate"] = max(0.5, min(2.0, rate / base))
                    if preserve_pitch is not None:
                        sound["pitch_preserve_live"] = bool(preserve_pitch)
                        sound.pop("wsola_buf", None)
                        sound["wsola_read"] = 0
                        sound["wsola_write"] = 0

    def handle_retrigger(self, sound_id: str, policy: str):
        """Pre-check for a re-press of a playing sound_id. Returns None when the
        caller should proceed to play_sound (nothing live, or 'layer'). 'restart'
        seeks the newest instance to its cue (no new copy); 'toggle' fades it."""
        with self.lock:
            live = [s for s in self.currently_playing if s.get("sound_id") == sound_id]
        if not live or policy == "layer":
            return None
        if policy == "restart":
            newest = max(live, key=lambda s: s.get("instance", 0))
            with self.lock:
                newest["pending_seek"] = newest.get("cue", 0)
                newest["paused"] = False
            self._ptt_release_countdown = 0
            self._press_ptt()
            return "restarted"
        if policy == "toggle":
            self.fade_out_sound(sound_id, 250, then="stop")
            return "stopped"
        return None

    def on_air_state(self):
        """('user'|'app'|'off', mic_ducked) for the ON-AIR LED. 'app' = the
        mixer is auto-holding your PTT key (your own mic is ducked); 'user' =
        your own voice is live."""
        talking = bool(self.ptt_user_physical
                       or (self.universal_ptt_enabled and self.universal_ptt_open)
                       or self.manual_ptt_hold)
        app_holding = bool(self.ptt_active and not self.ptt_user_physical)
        state = "user" if talking else ("app" if app_holding else "off")
        ducked = bool(app_holding and self.duck_mic_during_sounds)
        return state, ducked

    def get_sound_peaks(self, sound_id: str, buckets: int = 160):
        """Normalised abs-max peak envelope for the waveform, memoised by
        (id(data), buckets). Cheap; the Deck calls it off the Tk thread."""
        data = None
        with self.lock:
            for sound in self.currently_playing:
                if sound.get("sound_id") == sound_id:
                    data = sound.get("data")
                    break
        if data is None or len(data) == 0:
            return None
        key = (id(data), buckets)
        cache = self._peaks_cache
        if key in cache:
            return cache[key]
        mono = data if getattr(data, "ndim", 1) == 1 else data.mean(axis=1)
        n = len(mono)
        edges = np.linspace(0, n, buckets + 1).astype(np.int64)
        out = np.zeros(buckets, dtype=np.float32)
        for b in range(buckets):
            a, c = int(edges[b]), int(edges[b + 1])
            if c > a:
                out[b] = float(np.max(np.abs(mono[a:c])))
        m = float(out.max())
        if m > 0:
            out = out / m
        cache[key] = out
        if len(cache) > 32:
            cache.pop(next(iter(cache)))
        return out

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
                        "emoji": sound.get("emoji"),
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
                        "label": sound.get("label", sound.get("name", "Unknown")),
                        "meta": sound.get("meta", {}),
                        "instance": sound.get("instance"),
                        "base_speed": sound.get("base_speed", 1.0),
                        "live_rate": sound.get("playback_rate", 1.0),
                        "effective_rate": float(sound.get("base_speed", 1.0)) * float(sound.get("playback_rate", 1.0)),
                        "pitch_preserve_live": sound.get("pitch_preserve_live", False),
                        "gain": sound.get("gain", 1.0),
                        "fading": sound.get("gain_target", 1.0) < sound.get("gain", 1.0),
                        "bed": sound.get("bed", False),
                        "cue_s": sound.get("cue", 0) / self.sample_rate,
                        "started": sound.get("started", False),
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

        self._register_ptt_physical_watch()
        logger.debug("PTT key set to: %s (VK: %s)", self.ptt_key, self._cached_ptt_vk)

    def _register_ptt_physical_watch(self):
        """Watch the user's REAL finger on the (keyboard) PTT key.

        We inject the same key for auto-PTT, so the OS key state alone can't
        distinguish our injection from the user's hold. The keyboard hook
        sees both too — but we know exactly when we inject
        (``_inject_window_until`` is set right before every SendInput), so
        any event OUTSIDE those ~50ms windows is the user's own press or
        release. Mouse PTT buttons aren't watched (no reliable physical
        signal); ``ptt_user_physical`` simply stays False for them.
        """
        # Drop the previous watch (key changed or cleared).
        for h in self._ptt_watch_handles:
            try:
                import keyboard as _kb

                _kb.unhook(h)
            except Exception:
                pass
        self._ptt_watch_handles = []
        self.ptt_user_physical = False
        if not self.ptt_key or self.ptt_key.startswith("mouse"):
            return
        try:
            import keyboard as _kb

            def _phys(event, down):
                # Our own injected events land inside the window — ignore.
                if time.time() < self._inject_window_until:
                    return
                self.ptt_user_physical = down

            self._ptt_watch_handles = [
                _kb.on_press_key(self.ptt_key, lambda e: _phys(e, True), suppress=False),
                _kb.on_release_key(self.ptt_key, lambda e: _phys(e, False), suppress=False),
            ]
        except Exception as e:
            logger.warning("PTT physical watch unavailable: %s", e)
            self._ptt_watch_handles = []

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
        # Directly release to ensure key isn't stuck (bypass queue). Skipped
        # while the user PHYSICALLY holds the key — injecting a key-up then
        # would cut their Discord transmission dead until they re-pressed
        # (their own physical release ends it instead).
        try:
            if self.ptt_user_physical and not shutdown:
                logger.debug("PTT force-release skipped — user physically holds %s", self.ptt_key)
            elif self.ptt_key.startswith("mouse"):
                self._inject_window_until = time.time() + 0.05
                button = self.PTT_BUTTON_MAP.get(self.ptt_key, "x2")
                _simulate_mouse_button(button, press=False)
            elif self._cached_ptt_vk is not None:
                self._inject_window_until = time.time() + 0.05
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
        self._output_dir: Optional[str] = None
        self._explicit_filename: Optional[str] = None
        self._start_timestamp: Optional[str] = None

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

    @staticmethod
    def _format_duration_for_filename(seconds: float) -> str:
        """Compact recording duration token safe for filenames."""
        total = max(0, int(round(seconds)))
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}h{minutes:02d}m{secs:02d}s"
        if minutes:
            return f"{minutes:02d}m{secs:02d}s"
        return f"{secs:02d}s"

    @staticmethod
    def _unique_output_path(path: str) -> str:
        """Return a non-conflicting path by adding (2), (3), ... if needed."""
        base = Path(path)
        if not base.exists():
            return str(base)
        for idx in range(2, 1000):
            candidate = base.with_name(f"{base.stem} ({idx}){base.suffix}")
            if not candidate.exists():
                return str(candidate)
        return str(base.with_name(f"{base.stem}_{int(time.time())}{base.suffix}"))

    def _build_final_output_path(self, duration_seconds: float, ext: str = ".mp3") -> str:
        """Build the final path once duration is known."""
        if self._explicit_filename and self.output_path:
            return self._unique_output_path(self.output_path)

        output_dir = self._output_dir or os.getcwd()
        timestamp = self._start_timestamp or time.strftime("%Y-%m-%d_%H-%M-%S")
        duration = self._format_duration_for_filename(duration_seconds)
        filename = f"recording_{timestamp}_{duration}{ext}"
        return self._unique_output_path(os.path.join(output_dir, filename))

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
        self._output_dir = output_dir
        self._explicit_filename = filename
        self._start_timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        if filename is None:
            filename = f"recording_{self._start_timestamp}_pending.mp3"
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
        self._start_time = time.time()
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

    def stop(self, on_saved: Optional[Callable[[Optional[str]], None]] = None) -> Optional[str]:
        """
        Stop recording. Captured audio is written to disk in a background
        thread so the UI never freezes (MP3 encoding can take a few seconds
        for long recordings). Returns the planned output path immediately
        (the file may not exist yet — encoding is async).
        """
        if not self.recording:
            return None

        duration_seconds = max(0.0, time.time() - self._start_time)
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
        out_path = self._build_final_output_path(duration_seconds)
        self.output_path = out_path
        sample_rate = self.sample_rate
        self._loopback_blocks = []
        self._mic_blocks = []

        if not loopback_blocks:
            logger.warning("Recorder stopped with no captured audio.")
            if on_saved is not None:
                try:
                    on_saved(None)
                except Exception:
                    logger.exception("Recorder on_saved callback failed")
            return None

        # Encode in a background thread so the UI thread (and the audio
        # callback's chance at the GIL) is freed immediately.
        threading.Thread(
            target=self._encode_async,
            args=(loopback_blocks, mic_blocks, include_mic, out_path, sample_rate, on_saved),
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
        on_saved: Optional[Callable[[Optional[str]], None]] = None,
    ) -> Optional[str]:
        """Background-thread encoding job. Writes the MP3 (or WAV fallback)."""
        saved_path: Optional[str] = None
        try:
            loopback = np.concatenate(loopback_blocks, axis=0)
        except Exception as e:
            logger.error("Failed to concatenate loopback audio: %s", e)
            if on_saved is not None:
                try:
                    on_saved(None)
                except Exception:
                    logger.exception("Recorder on_saved callback failed")
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
                saved_path = wav_path
                if on_saved is not None:
                    try:
                        on_saved(saved_path)
                    except Exception:
                        logger.exception("Recorder on_saved callback failed")
                return saved_path
            except Exception as e:
                logger.error("Failed to write fallback WAV: %s", e)
                if on_saved is not None:
                    try:
                        on_saved(None)
                    except Exception:
                        logger.exception("Recorder on_saved callback failed")
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
            saved_path = out_path
            return saved_path
        except Exception as e:
            logger.error("Failed to encode MP3: %s", e)
            try:
                wav_path = (out_path or "recording.mp3").rsplit(".", 1)[0] + ".wav"
                sf.write(wav_path, mono, sample_rate, subtype="PCM_16")
                logger.warning("MP3 encode failed; wrote WAV instead: %s", wav_path)
                saved_path = wav_path
                return saved_path
            except Exception as e2:
                logger.error("Fallback WAV also failed: %s", e2)
                return None
        finally:
            if on_saved is not None:
                try:
                    on_saved(saved_path)
                except Exception:
                    logger.exception("Recorder on_saved callback failed")

    def get_elapsed(self) -> float:
        """Seconds since recording started (0 if not recording)."""
        if not self.recording:
            return 0.0
        return time.time() - self._start_time
