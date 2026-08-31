"""Headless engine tests for the DJ Looper bug-fixes (no audio devices opened).

Run:  .venv\\Scripts\\python.exe test_dj_engine.py

Guards the 2026-08-31 fixes surfaced by the DJ-Looper redesign diagnosis:
  * resume/restart re-press the Discord auto-PTT key (a pause releases it, so a
    resumed sound used to reach the virtual cable with PTT UP → inaudible);
  * play_sound threads a display_name + emoji through to get_playing_sounds so
    the panel shows the slot's real title, not the audio file's hash.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from soundboard.audio import AudioMixer  # noqa: E402


def _mixer():
    # No streams are opened until start(); construction touches no devices.
    m = AudioMixer(input_device=0, output_device=0)
    m.ptt_key = "f9"
    m.ptt_active = False
    m._ptt_presses = 0
    m._press_ptt = lambda: setattr(m, "_ptt_presses", m._ptt_presses + 1)  # type: ignore
    return m


def _fake_sound(sound_id="0_0", paused=False, name="filehash_123", emoji=None):
    return {
        "data": [0.0] * 480,
        "position": 0,
        "volume": 1.0,
        "sound_id": sound_id,
        "loop": False,
        "loops_remaining": -1,
        "in_delay": False,
        "delay_position": 0,
        "paused": paused,
        "name": name,
        "emoji": emoji,
    }


class PttResume(unittest.TestCase):
    def test_resume_repps_ptt(self):
        m = _mixer()
        m.currently_playing = [_fake_sound(paused=True)]
        m._ptt_release_countdown = 99
        m.resume_sound("0_0")
        self.assertFalse(m.currently_playing[0]["paused"])
        self.assertEqual(m._ptt_presses, 1, "resume must re-press the PTT key")
        self.assertEqual(m._ptt_release_countdown, 0)

    def test_resume_unknown_id_is_safe(self):
        m = _mixer()
        m.currently_playing = [_fake_sound(paused=True)]
        m.resume_sound("nope")
        # Nothing resumed → no spurious PTT press.
        self.assertEqual(m._ptt_presses, 0)
        self.assertTrue(m.currently_playing[0]["paused"])

    def test_restart_repps_ptt(self):
        m = _mixer()
        s = _fake_sound(paused=True)
        s["position"] = 12345
        m.currently_playing = [s]
        m.restart_sound("0_0")
        self.assertEqual(m.currently_playing[0]["position"], 0)
        self.assertFalse(m.currently_playing[0]["paused"])
        self.assertEqual(m._ptt_presses, 1, "restart must re-press the PTT key")


class DisplayName(unittest.TestCase):
    def test_get_playing_sounds_exposes_name_and_emoji(self):
        m = _mixer()
        m.currently_playing = [_fake_sound(name="שלום עולם", emoji="👋")]
        snap = m.get_playing_sounds()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["name"], "שלום עולם")
        self.assertEqual(snap[0]["emoji"], "👋")

    def test_play_sound_accepts_display_name_kwargs(self):
        # Signature guard: play_sound / _play_sound_sync must accept the new
        # keyword arguments (they are threaded through to the sound entry).
        import inspect
        for fn in (AudioMixer.play_sound, AudioMixer._play_sound_sync):
            params = inspect.signature(fn).parameters
            self.assertIn("display_name", params, f"{fn.__name__} missing display_name")
            self.assertIn("emoji", params, f"{fn.__name__} missing emoji")


if __name__ == "__main__":
    print("=" * 60)
    print("DJ Looper engine tests")
    print("=" * 60)
    unittest.main(verbosity=2)
