"""Headless engine tests for the Air Deck (WP-A) — drives AudioMixer._output_callback
directly with no audio device. No streams are opened.

Run:  .venv\\Scripts\\python.exe test_deck_engine.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
from soundboard.audio import AudioMixer  # noqa: E402

FRAMES = 1024
SR = 48000


def make_mixer():
    m = AudioMixer(input_device=0, output_device=0)
    m.ptt_key = None
    m._ptt_presses = 0
    m._press_ptt = lambda: setattr(m, "_ptt_presses", m._ptt_presses + 1)  # type: ignore
    m.mic_muted = True                       # isolate sounds (no mic in the mix)
    m._last_mic_data = np.zeros(FRAMES, dtype=np.float32)
    m._u_gate_env = 1.0
    m.voice_changer = None
    m.test_output_enabled = False
    return m


def entry(m, sid="0_0", data=None, gain=1.0, paused=False, instance=None, loop=False, cue=0, level=0.5):
    if data is None:
        data = np.full(4096, level, dtype=np.float32)
    return {
        "data": data, "position": 0, "volume": 1.0, "sound_id": sid,
        "loop": loop, "loop_count": 0, "loops_remaining": (-1 if loop else 0),
        "loop_delay": 0.0, "loop_delay_samples": 0, "in_delay": False, "delay_position": 0,
        "file_path": None, "speed": 1.0, "preserve_pitch": True, "playback_rate": 1.0,
        "name": "Test", "label": "Test", "emoji": None, "base_speed": 1.0, "meta": {},
        "instance": (instance if instance is not None else next(m._instance_counter)),
        "gain": gain, "gain_target": 1.0, "gain_step": 0.0, "after_fade": None,
        "cue": cue, "pending_seek": None, "bed": False, "started": False, "paused": paused,
        "pitch_preserve_live": False,
    }


def drive(m, blocks=1):
    outs = []
    for _ in range(blocks):
        out = np.zeros((FRAMES, m.channels), dtype=np.float32)
        m._output_callback(out, FRAMES, None, None)
        outs.append(out.copy())
    return outs


class Fades(unittest.TestCase):
    def test_fade_out_reaches_zero_removes_and_events(self):
        m = make_mixer()
        m.currently_playing = [entry(m, level=0.5, data=np.full(48000, 0.5, dtype=np.float32))]
        drive(m, 1)                       # let it start (emits 'started')
        m.fade_out_sound("0_0", ms=100, then="stop")
        outs = np.concatenate(drive(m, 8))[:, 0]
        # entry removed once the fade completes
        self.assertEqual(len(m.currently_playing), 0, "faded sound must be removed")
        evs = m.pop_events()
        kinds = [(t, meta.get("reason")) for (t, _id, meta) in evs]
        self.assertIn(("ended", "faded"), kinds)
        # no click across the fade (constant source, so only the gain ramp moves)
        step = np.max(np.abs(np.diff(outs)))
        self.assertLess(step, 0.05, f"fade had a step of {step:.3f}")

    def test_pause_fade_then_resume(self):
        m = make_mixer()
        m.currently_playing = [entry(m, data=np.full(48000, 0.4, dtype=np.float32))]
        drive(m, 1)
        m.pause_sound("0_0", fade_ms=30)
        drive(m, 4)
        s = m.currently_playing[0]
        self.assertTrue(s["paused"], "sound must be paused after the fade")
        self.assertLessEqual(s["gain"], 0.001, "paused at gain 0")
        self.assertIn("paused", [t for (t, _i, _mm) in m.pop_events()])
        # audio is silent while paused
        self.assertLess(np.max(np.abs(np.concatenate(drive(m, 1)))), 1e-6)
        # resume: unpauses, re-presses PTT, audio comes back
        before = m._ptt_presses
        m.resume_sound("0_0", fade_ms=30)
        self.assertFalse(m.currently_playing[0]["paused"])
        self.assertEqual(m._ptt_presses, before + 1, "resume must re-press PTT")
        out = np.concatenate(drive(m, 4))
        self.assertGreater(np.max(np.abs(out)), 0.1, "audio must resume")


class Seek(unittest.TestCase):
    def test_seek_lands_and_works_while_paused(self):
        m = make_mixer()
        data = np.arange(96000, dtype=np.float32) / 96000.0  # ramp so position is identifiable
        m.currently_playing = [entry(m, data=data, paused=True)]
        m.seek_sound("0_0", 1.0)          # 1 s = 48000 samples
        drive(m, 1)                        # applies pending_seek even while paused
        self.assertAlmostEqual(m.currently_playing[0]["position"], 48000, delta=2)
        self.assertTrue(m.currently_playing[0]["paused"], "still paused after seek")

    def test_relative_seek(self):
        m = make_mixer()
        m.currently_playing = [entry(m, data=np.full(192000, 0.3, dtype=np.float32))]
        drive(m, 2)                        # advance ~2048 samples
        pos0 = m.currently_playing[0]["position"]
        m.seek_sound("0_0", 0.5, relative=True)
        drive(m, 1)
        self.assertAlmostEqual(m.currently_playing[0]["position"], pos0 + 24000, delta=1100)


class Retrigger(unittest.TestCase):
    def test_layer_returns_none(self):
        m = make_mixer()
        m.currently_playing = [entry(m)]
        self.assertIsNone(m.handle_retrigger("0_0", "layer"))

    def test_restart_seeks_to_cue_no_new_entry(self):
        m = make_mixer()
        m.currently_playing = [entry(m, data=np.full(192000, 0.3, dtype=np.float32), cue=9600)]
        drive(m, 3)
        before = m._ptt_presses
        self.assertEqual(m.handle_retrigger("0_0", "restart"), "restarted")
        self.assertEqual(len(m.currently_playing), 1, "restart must NOT add a copy")
        drive(m, 1)
        self.assertAlmostEqual(m.currently_playing[0]["position"], 9600, delta=1100)
        self.assertEqual(m._ptt_presses, before + 1)

    def test_toggle_fades(self):
        m = make_mixer()
        m.currently_playing = [entry(m)]
        self.assertEqual(m.handle_retrigger("0_0", "toggle"), "stopped")
        self.assertEqual(m.currently_playing[0]["gain_target"], 0.0)

    def test_instance_cap_recycles_oldest(self):
        m = make_mixer()
        m.currently_playing = [entry(m, instance=i) for i in (1, 2, 3)]
        m._enforce_instance_cap("0_0", 3)  # at cap -> fade the oldest (instance 1)
        self.assertEqual(m.currently_playing[0]["gain_target"], 0.0)
        self.assertEqual(m.currently_playing[1]["gain_target"], 1.0)


class Snapshot(unittest.TestCase):
    def test_get_playing_sounds_deck_fields(self):
        m = make_mixer()
        s = entry(m)
        s["base_speed"] = 1.5
        s["playback_rate"] = 1.0
        m.currently_playing = [s]
        snap = m.get_playing_sounds()[0]
        for k in ("label", "meta", "instance", "base_speed", "live_rate",
                  "effective_rate", "gain", "fading", "bed", "cue_s", "started"):
            self.assertIn(k, snap)
        self.assertAlmostEqual(snap["effective_rate"], 1.5)

    def test_set_effective_rate_honours_baked_speed(self):
        m = make_mixer()
        s = entry(m)
        s["base_speed"] = 1.5              # already baked 1.5x
        m.currently_playing = [s]
        m.set_effective_rate("0_0", 1.5)   # WANT 1.5x total -> live rate 1.0
        self.assertAlmostEqual(m.currently_playing[0]["playback_rate"], 1.0)

    def test_on_air_state(self):
        m = make_mixer()
        m.ptt_active = False
        m.ptt_user_physical = False
        m.manual_ptt_hold = False
        self.assertEqual(m.on_air_state()[0], "off")
        m.ptt_active = True
        self.assertEqual(m.on_air_state()[0], "app")
        m.ptt_user_physical = True
        self.assertEqual(m.on_air_state()[0], "user")

    def test_get_sound_peaks(self):
        m = make_mixer()
        data = np.sin(np.linspace(0, 40, 96000)).astype(np.float32)
        m.currently_playing = [entry(m, data=data)]
        peaks = m.get_sound_peaks("0_0", buckets=64)
        self.assertEqual(len(peaks), 64)
        self.assertLessEqual(float(peaks.max()), 1.0 + 1e-5)
        self.assertGreater(float(peaks.max()), 0.5)
        # memoised: same object back
        self.assertIs(m.get_sound_peaks("0_0", buckets=64), peaks)


class FastPath(unittest.TestCase):
    def test_steady_sound_adds_no_envelope_ops(self):
        m = make_mixer()
        m.currently_playing = [entry(m, data=np.full(192000, 0.3, dtype=np.float32))]
        import soundboard.audio as A
        calls = {"n": 0}
        orig = A.np.linspace

        def counting(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)

        A.np.linspace = counting
        try:
            drive(m, 3)                    # mic muted + gain==target==1.0
        finally:
            A.np.linspace = orig
        self.assertEqual(calls["n"], 0, "steady 1.0 sound must run the byte-identical fast path")


if __name__ == "__main__":
    print("=" * 60)
    print("Air Deck engine tests (WP-A)")
    print("=" * 60)
    unittest.main(verbosity=2)
