"""
Universal Push-to-Talk — one PTT key for EVERY app.

The Discord auto-PTT in :mod:`soundboard.audio` injects an app-specific key
while sounds play. This module is the opposite model: the soundboard itself
becomes the PTT. Target apps (Zoom / WhatsApp / Slack / Discord / anything)
simply use the virtual cable as their microphone with plain voice-activity,
and *we* gate what reaches the cable:

* Live mic  → only while the user holds the global PTT key (the "gate").
* Sounds    → always pass (they're triggered deliberately).

So one key push-to-talks every app at once, with zero per-app setup beyond
"pick the cable as your mic".

Key detection is a tiny GetAsyncKeyState poll thread — NOT a keyboard/mouse
hook. The `mouse` library's WH_MOUSE_LL hook ran Python for every pointer
movement system-wide and made the OS cursor stutter (see the hover-preview
history in gui.py); polling one VK code at ~100 Hz is a single cheap syscall,
layout-independent (Hebrew!), and self-healing — a missed edge just gets
picked up on the next tick, so the gate can never "stick" open.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# VK codes for the app's mouseN naming (matches _mouse_button_to_key_name in
# gui.py: mouse lib 'x' → mouse5 = VK_XBUTTON1, 'x2' → mouse4 = VK_XBUTTON2).
_MOUSE_VK = {
    "mouse1": 0x01,  # VK_LBUTTON
    "mouse2": 0x02,  # VK_RBUTTON
    "mouse3": 0x04,  # VK_MBUTTON
    "mouse4": 0x06,  # VK_XBUTTON2
    "mouse5": 0x05,  # VK_XBUTTON1
}

_POLL_INTERVAL = 0.010  # 10ms → ≤10ms press latency, negligible CPU


def _resolve_vk(key: str) -> Optional[int]:
    """Resolve a key name ('f13', 'v', 'mouse4', …) to a Windows VK code."""
    key = (key or "").strip().lower()
    if not key:
        return None
    if key in _MOUSE_VK:
        return _MOUSE_VK[key]
    if key.startswith("mouse"):
        return None  # unknown mouse button name
    try:
        from soundboard.audio import _get_vk_code

        return _get_vk_code(key)
    except Exception:
        return None


def _build_cue(freqs_ms, sample_rate: int) -> np.ndarray:
    """Render a sequence of (frequency_hz, duration_ms) sine blips.

    Each segment gets a short raised-cosine fade in/out so the cue is
    click-free at any volume. Mono float32, peak ≈ 0.5.
    """
    parts = []
    for freq, ms in freqs_ms:
        n = int(sample_rate * ms / 1000.0)
        t = np.arange(n, dtype=np.float32) / sample_rate
        seg = np.sin(2.0 * np.pi * freq * t).astype(np.float32)
        fade = min(int(sample_rate * 0.004), n // 2)  # 4ms edges
        if fade > 0:
            ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, fade, dtype=np.float32))
            seg[:fade] *= ramp
            seg[-fade:] *= ramp[::-1]
        parts.append(seg)
    cue = np.concatenate(parts) if parts else np.zeros(1, dtype=np.float32)
    return (cue * 0.5).astype(np.float32)


class UniversalPTT:
    """Global PTT gate controller.

    Owns the poll thread that watches the user's key, flips the
    ``universal_ptt_open`` flag on the attached :class:`AudioMixer`, plays the
    local press/release cue beeps, and notifies the GUI indicator via
    ``on_state`` (called from the poll thread — marshal to Tk with `after`).
    """

    def __init__(self, sample_rate: int = 48000):
        self.enabled: bool = False
        self.key: Optional[str] = None
        self.transmitting: bool = False

        # Cue beeps (local speakers only — never sent to the cable).
        self.cues_enabled: bool = True
        self.cue_volume: float = 0.6  # 0..1
        self._sample_rate = sample_rate
        # Discord-ish blips: press rises, release falls.
        self._press_cue = _build_cue([(660.0, 40), (990.0, 70)], sample_rate)
        self._release_cue = _build_cue([(990.0, 40), (660.0, 70)], sample_rate)

        # GUI callback: on_state(transmitting: bool) — fired from poll thread.
        self.on_state: Optional[Callable[[bool], None]] = None

        self._mixer = None  # AudioMixer or None
        self._vk: Optional[int] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_generation = 0  # bumped on every (re)configure to retire old threads
        self._lock = threading.Lock()

        self._user32 = None
        if sys.platform == "win32":
            try:
                import ctypes

                self._user32 = ctypes.windll.user32
            except Exception:
                self._user32 = None

    # ------------------------------------------------------------------ #
    # Wiring
    # ------------------------------------------------------------------ #
    def attach_mixer(self, mixer) -> None:
        """Point the gate at a (fresh) AudioMixer. Safe to call with None."""
        self._mixer = mixer
        self._push_to_mixer()

    def configure(self, enabled: bool, key: Optional[str]) -> None:
        """Apply settings. Restarts the poll thread as needed.

        The gate always (re)starts CLOSED — enabling PTT must never leave the
        mic hot until the user actually presses the key.
        """
        with self._lock:
            self.key = (key or "").strip().lower() or None
            self._vk = _resolve_vk(self.key) if self.key else None
            self.enabled = bool(enabled) and self._vk is not None
            self._set_transmitting(False, play_cue=False)
            self._poll_generation += 1
            if self.enabled and self._user32 is not None:
                gen = self._poll_generation
                self._poll_thread = threading.Thread(
                    target=self._poll_loop, args=(gen,), daemon=True, name="UniversalPTT"
                )
                self._poll_thread.start()
            else:
                self._poll_thread = None
                if enabled and self._vk is None and self.key:
                    logger.warning("Universal PTT: could not resolve VK for key %r", self.key)
        self._push_to_mixer()

    def shutdown(self) -> None:
        """Stop polling and close the gate (app quit / feature off)."""
        with self._lock:
            self._poll_generation += 1
            self._poll_thread = None
            self.enabled = False
            self._set_transmitting(False, play_cue=False)
        self._push_to_mixer()

    def available(self) -> bool:
        """True when key polling can work on this machine."""
        return self._user32 is not None

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _push_to_mixer(self) -> None:
        m = self._mixer
        if m is not None:
            try:
                m.universal_ptt_open = self.transmitting
                m.universal_ptt_enabled = self.enabled
            except Exception:
                pass

    def _set_transmitting(self, down: bool, play_cue: bool = True) -> None:
        if down == self.transmitting:
            return
        self.transmitting = down
        m = self._mixer
        if m is not None:
            try:
                m.universal_ptt_open = down
            except Exception:
                pass
        if play_cue and self.cues_enabled:
            self.play_cue(press=down)
        cb = self.on_state
        if cb is not None:
            try:
                cb(down)
            except Exception:
                pass

    def _poll_loop(self, generation: int) -> None:
        """Watch the PTT key via GetAsyncKeyState until retired."""
        user32 = self._user32
        vk = self._vk
        if user32 is None or vk is None:
            return
        # Flush the "pressed since last call" latch bit.
        try:
            user32.GetAsyncKeyState(vk)
        except Exception:
            return
        was_down = False
        while generation == self._poll_generation and self.enabled:
            try:
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            except Exception:
                down = False
            if down != was_down:
                was_down = down
                self._set_transmitting(down)
            time.sleep(_POLL_INTERVAL)
        # Retired: make sure this generation never leaves the gate open.
        if self.transmitting and generation == self._poll_generation:
            self._set_transmitting(False, play_cue=False)

    # ------------------------------------------------------------------ #
    # Cue beeps
    # ------------------------------------------------------------------ #
    def play_cue(self, press: bool) -> None:
        """Play the press/release blip on the DEFAULT speakers (non-blocking).

        Deliberately not sd.play(): that would cut off any slot preview using
        the module-level default playback. A tiny dedicated OutputStream per
        cue (~100ms of audio) is cheap and can overlap previews/monitoring.
        """
        buf = self._press_cue if press else self._release_cue
        vol = max(0.0, min(1.0, float(self.cue_volume)))
        if vol <= 0.0:
            return
        threading.Thread(
            target=self._cue_worker, args=(buf * vol,), daemon=True, name="UPTTCue"
        ).start()

    def _cue_worker(self, buf: np.ndarray) -> None:
        try:
            import sounddevice as sd

            with sd.OutputStream(
                samplerate=self._sample_rate, channels=1, dtype="float32", device=None
            ) as stream:
                stream.write(buf.reshape(-1, 1))
        except Exception as e:  # cue is best-effort — never let it crash a hook
            logger.debug("Universal PTT cue failed: %s", e)
