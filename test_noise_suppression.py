"""Headless tests for the mic noise-suppression engines (soundboard/audio.py).

Run with:  .venv\\Scripts\\python.exe test_noise_suppression.py

No audio devices are opened. Covers:
  * RNNoise loads WITHOUT importing the pyrnnoise package (the frozen-EXE bug:
    pyrnnoise's __init__ needs audiolab/av, which the EXE never had, so the
    "Light" engine silently passed the raw mic through for months)
  * every available engine builds, keeps frame length, and attenuates
    steady noise in steady state
  * the block<->frame ring is a clean, exactly-one-frame delay
  * a missing engine degrades ONCE along its fallback route with a note, with
    no per-block retries / log spam
  * engines are built off-thread; process() passes through until one lands
  * the mixer's mic-processing worker keeps block order and drains cleanly
  * the 80 Hz low-cut removes rumble and leaves the voice band alone
"""
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soundboard.audio as A  # noqa: E402

SR, BLOCK, FS = 48000, 1024, 480


def _blocks(x):
    for i in range(0, len(x) - BLOCK + 1, BLOCK):
        yield x[i : i + BLOCK]


def _rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)) + 1e-12)


def _db(a, b):
    return 20.0 * np.log10(_rms(a) / _rms(b))


class _Identity(A._DenoiseBackend):
    name = "identity"
    frame_size = FS


class _Boom(A._DenoiseBackend):
    name = "boom"

    def __init__(self):
        raise RuntimeError("boom")


class RnnoiseLoaderTests(unittest.TestCase):
    def test_loader_does_not_import_pyrnnoise_package(self):
        code = (
            "import sys, soundboard.audio as a;"
            "print(a.RNNOISE_AVAILABLE, 'pyrnnoise' in sys.modules, 'audiolab' in sys.modules, 'av' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))
        )
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        flags = out.stdout.split()[-4:]
        self.assertEqual(flags[0], "True", "RNNoise DLL should load in the dev venv")
        self.assertEqual(flags[1:], ["False", "False", "False"], "pyrnnoise/audiolab/av must never be imported")

    def test_loader_works_when_package_is_gone_but_dll_is_in_meipass(self):
        """Frozen-EXE simulation: the package is excluded from the bundle; only
        _MEIPASS/pyrnnoise/rnnoise.dll exists."""
        code = r"""
import sys, os, shutil, tempfile, importlib.abc, importlib.util
spec = importlib.util.find_spec("pyrnnoise")
dll = os.path.join(os.path.dirname(spec.origin), "rnnoise.dll")
mei = tempfile.mkdtemp(prefix="lsb_mei_")
os.makedirs(os.path.join(mei, "pyrnnoise"))
shutil.copy(dll, os.path.join(mei, "pyrnnoise", "rnnoise.dll"))
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "pyrnnoise" or name.startswith("pyrnnoise.") or name in ("audiolab", "av"):
            raise ModuleNotFoundError(name)
        return None
sys.meta_path.insert(0, Block())
sys._MEIPASS = mei
import numpy as np, soundboard.audio as a
b = a._RnnoiseBackend(); y = b.process_frame(np.zeros(480, np.float32)); b.close()
print(a.RNNOISE_AVAILABLE, a._RNNOISE_LIB.path.startswith(mei), y.shape[0])
"""
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))
        )
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(out.stdout.split()[-3:], ["True", "True", "480"])


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(0)
        cls.noise = (0.05 * rng.standard_normal(SR * 5)).astype(np.float32)
        t = np.arange(SR * 5) / SR
        # "voice": a few harmonics with a slow amplitude envelope, 200 Hz f0
        env = (0.5 + 0.5 * np.sin(2 * np.pi * 2.0 * t)) ** 2
        voice = sum(np.sin(2 * np.pi * 200 * k * t) / k for k in range(1, 8))
        cls.voice = (0.2 * env * voice / np.abs(voice).max()).astype(np.float32)

    def _run(self, name, x, strength=1.0, lowcut=False):
        ns = A.NoiseSuppressor(SR, BLOCK)
        ns.set_backend(name)
        ns.set_strength(strength)
        ns.lowcut = lowcut
        ns.enabled = True
        self.assertEqual(ns.prepare(), name, ns.note)
        out = np.concatenate([ns.process(b) for b in _blocks(x)])
        ns.close()
        return out

    def test_catalogue_is_consistent(self):
        avail = A.ns_available_backends()
        self.assertIn("classic", avail)
        self.assertIn("gate", avail)
        for n in avail:
            self.assertIn(n, A.NS_BACKEND_LABELS)
            self.assertIn(n, A.NS_BACKEND_BLURBS)
            self.assertIn(n, A._NS_FALLBACKS)
        self.assertEqual(avail[0], A._default_backend_name())

    def test_every_engine_keeps_length_and_attenuates_steady_noise(self):
        floor_db = {"deepfilternet": -25, "max": -25, "classic": -15, "rnnoise": -15, "gate": -10}
        for name in A.ns_available_backends():
            with self.subTest(engine=name):
                out = self._run(name, self.noise)
                self.assertEqual(len(out), len(self.noise) // BLOCK * BLOCK)
                self.assertTrue(np.all(np.isfinite(out)))
                # steady state: last second
                att = _db(out[-SR:], self.noise[-SR:])
                self.assertLess(att, floor_db[name], f"{name}: only {att:.1f} dB of steady-noise attenuation")

    def test_classic_keeps_a_clean_voice(self):
        out = self._run("classic", self.voice)
        # 20 ms latency (10 ms STFT + 10 ms ring); compare steady-state RMS
        self.assertGreater(_db(out[SR:], self.voice[SR:]), -1.5)

    def test_strength_changes_classic_floor(self):
        strong = self._run("classic", self.noise, strength=1.0)
        gentle = self._run("classic", self.noise, strength=0.0)
        self.assertLess(_db(strong[-SR:], self.noise[-SR:]) + 6, _db(gentle[-SR:], self.noise[-SR:]))

    def test_lowcut_removes_rumble_only(self):
        t = np.arange(SR * 2) / SR
        for f, lo, hi in ((50.0, -30.0, -6.0), (1000.0, -0.3, 0.3)):
            tone = (0.2 * np.sin(2 * np.pi * f * t)).astype(np.float32)
            ns = A.NoiseSuppressor(SR, BLOCK)
            ns.lowcut = True
            ns.enabled = True  # no engine loaded -> passthrough + low-cut only
            ns._building = False
            ns._backend = None
            out = np.concatenate([ns.process(b) for b in _blocks(tone)])
            d = _db(out[SR:], tone[SR:])
            self.assertTrue(lo <= d <= hi, f"{f:.0f} Hz -> {d:.1f} dB")


class RingAndFallbackTests(unittest.TestCase):
    def test_ring_is_an_exact_one_frame_delay(self):
        ns = A.NoiseSuppressor(SR, BLOCK)
        ns._backend = _Identity()
        ns._enabled = True
        x = np.random.default_rng(1).standard_normal(BLOCK * 40).astype(np.float32)
        out = np.concatenate([ns.process(b) for b in _blocks(x)])
        np.testing.assert_allclose(out[FS:], x[: len(x) - FS], atol=1e-6)
        np.testing.assert_array_equal(out[:FS], 0.0)

    def test_wet_dry_mix_only_for_engines_without_native_strength(self):
        ns = A.NoiseSuppressor(SR, BLOCK)
        ident = _Identity()
        ident.has_native_strength = False
        ns._backend = ident
        ns._enabled = True
        ns.set_strength(0.5)
        x = np.ones(BLOCK * 4, dtype=np.float32)
        out = np.concatenate([ns.process(b) for b in _blocks(x)])
        # identity engine: wet == dry, so any mix returns the input (delayed)
        np.testing.assert_allclose(out[FS:], 1.0, atol=1e-6)

    def test_failed_engine_falls_back_once_with_note_and_no_spam(self):
        ns = A.NoiseSuppressor(SR, BLOCK)
        orig_build = A._build_ns_backend
        calls = []

        def fake_build(name):
            calls.append(name)
            if name == "deepfilternet":
                raise RuntimeError("no onnxruntime here")
            return orig_build(name)

        records = []

        class H(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())

        h = H()
        A.logger.addHandler(h)
        A._build_ns_backend = fake_build
        try:
            ns.set_backend("deepfilternet")
            ns.enabled = True
            active = ns.prepare()
            self.assertEqual(active, "classic", ns.note)  # deepfilternet -> classic
            self.assertIn("unavailable", ns.note)
            self.assertIn("Classic", ns.note)
            x = np.zeros(BLOCK, np.float32)
            for _ in range(500):
                ns.process(x)
            self.assertEqual(calls.count("deepfilternet"), 1, "failed engine must not be retried per block")
            self.assertLessEqual(len(records), 3, records)
            # re-applying the same choice (every slider/checkbox apply does
            # this) must NOT retry ...
            ns.set_backend("deepfilternet")
            ns.prepare()
            self.assertEqual(calls.count("deepfilternet"), 1)
            # ... but an explicit dropdown pick (retry=True) retries exactly once
            ns.set_backend("deepfilternet", retry=True)
            ns.prepare()
            self.assertEqual(calls.count("deepfilternet"), 2)
        finally:
            A._build_ns_backend = orig_build
            A.logger.removeHandler(h)
            ns.close()

    def test_engine_builds_off_thread_and_passthrough_until_ready(self):
        ns = A.NoiseSuppressor(SR, BLOCK)
        ns.set_backend("classic")
        x = np.random.default_rng(2).standard_normal(BLOCK).astype(np.float32)
        ns.enabled = True  # kicks off the async build
        # Whatever the build state, process() must return same-length audio.
        y = ns.process(x)
        self.assertEqual(len(y), BLOCK)
        deadline = time.time() + 5
        while ns.active_backend is None and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(ns.active_backend, "classic")
        self.assertFalse(ns.is_loading)
        self.assertIn("Classic", ns.status_text())
        ns.close()

    def test_status_text_states(self):
        ns = A.NoiseSuppressor(SR, BLOCK)
        self.assertTrue(ns.status_text().startswith("Off"))
        bad = A.NoiseSuppressor(44100, BLOCK)
        bad.enabled = True
        self.assertIn("48 kHz", bad.status_text())
        self.assertEqual(len(bad.process(np.zeros(BLOCK, np.float32))), BLOCK)


REAL_CLIP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds", "discord_20260531_093144_09282296.wav")


def _si_sdr(est, ref):
    ref = ref - ref.mean()
    est = est - est.mean()
    a = np.dot(est, ref) / (np.dot(ref, ref) + 1e-12)
    t = a * ref
    return 10 * np.log10((np.sum(t**2) + 1e-12) / (np.sum((est - t) ** 2) + 1e-12))


def _align(out, ref, max_lag=6000):
    from scipy.signal import correlate

    c = correlate(out[: len(ref)], ref, mode="full", method="fft")
    mid = len(ref) - 1
    return int(np.argmax(c[mid : mid + max_lag]))


class StrengthAlignmentTests(unittest.TestCase):
    """Regression for the DeepFilterNet atten_lim comb-filter: with the strength
    slider below 100 the neural engines must still return CLEAN speech clean
    (delay-compensated wet/dry), and the residual level must follow the
    -40 dB * strength law."""

    def _neural(self):
        return [n for n in ("deepfilternet", "rnnoise", "max") if n in A.ns_available_backends()]

    def _run(self, name, x, strength):
        ns = A.NoiseSuppressor(SR, BLOCK)
        ns.set_backend(name)
        ns.set_strength(strength)
        ns.enabled = True
        self.assertEqual(ns.prepare(), name, ns.note)
        out = np.concatenate([ns.process(b) for b in _blocks(x)])
        lat = ns._backend.latency_samples
        ns.close()
        return out, lat

    @unittest.skipUnless(os.path.exists(REAL_CLIP), "needs the sounds library")
    def test_declared_latency_matches_measured(self):
        # Real speech is aperiodic, so the cross-correlation peak is unambiguous
        # (a synthetic harmonic voice locks one pitch period off).
        import soundfile as sf

        wav, _ = sf.read(REAL_CLIP, dtype="float32", always_2d=True)
        voice = wav.mean(axis=1)
        voice = (voice / (np.abs(voice).max() + 1e-9) * 0.5).astype(np.float32)
        x = np.concatenate([np.zeros(SR // 2, np.float32), voice])
        for name in A.ns_available_backends():
            with self.subTest(engine=name):
                out, lat = self._run(name, x, 1.0)
                lag = _align(out, x)
                self.assertEqual(lag, lat + FS, f"{name}: measured delay {lag} != declared {lat} + {FS} ring")

    @unittest.skipUnless(os.path.exists(REAL_CLIP), "needs the sounds library")
    def test_partial_strength_keeps_clean_speech_clean(self):
        import soundfile as sf

        x, sr = sf.read(REAL_CLIP, dtype="float32", always_2d=True)
        s = x.mean(axis=1)
        s = (s / (np.abs(s).max() + 1e-9) * 0.5).astype(np.float32)
        sig = np.concatenate([np.zeros(SR, np.float32), s])
        fr = np.sqrt((s[: len(s) // FS * FS].reshape(-1, FS) ** 2).mean(axis=1))
        act = np.repeat(fr > 0.02, FS)
        act = np.concatenate([act, np.zeros(len(s) - len(act), bool)])
        for name in self._neural():
            for strength in (1.0, 0.85, 0.5):
                with self.subTest(engine=name, strength=strength):
                    out, lat = self._run(name, sig, strength)
                    lag = lat + FS
                    est = out[SR + lag : SR + lag + len(s)]
                    est = np.concatenate([est, np.zeros(len(s) - len(est), np.float32)])
                    sdr = _si_sdr(est[act], s[act])
                    self.assertGreater(sdr, 12.0, f"{name} @ {strength}: clean speech came back at {sdr:.1f} dB SI-SDR")

    def test_residual_follows_strength(self):
        noise = (0.05 * np.random.default_rng(9).standard_normal(SR * 4)).astype(np.float32)
        for name in self._neural():
            with self.subTest(engine=name):
                full, _ = self._run(name, noise, 1.0)
                half, _ = self._run(name, noise, 0.5)
                att_full = _db(full[-SR:], noise[-SR:])
                att_half = _db(half[-SR:], noise[-SR:])
                self.assertLess(att_full, -25)
                # 0.5 -> dry leak at -20 dB dominates the engine's deeper floor
                self.assertTrue(-23 < att_half < -17, f"{name}: {att_half:.1f} dB at strength 0.5")


class MixerWorkerTests(unittest.TestCase):
    """AudioMixer without devices: __init__ opens nothing; we drive the input
    callback by hand and read the mix queue."""

    def _mixer(self):
        m = A.AudioMixer(None, None, sound_cache=None)
        return m

    def test_worker_keeps_order_and_drains_on_disable(self):
        m = self._mixer()
        try:
            ns = m.noise_suppressor
            ns.set_backend("gate")
            ns.enabled = True
            self.assertEqual(ns.prepare(), "gate")
            m._start_ns_worker()
            received = []
            # Replace the mix queue with a big one so nothing is dropped.
            m._mic_queue = queue.Queue(maxsize=1000)
            n_blocks = 60
            for i in range(n_blocks):
                blk = np.full((BLOCK, 1), float(i) * 1e-3, dtype=np.float32)
                m._input_callback(blk, BLOCK, None, None)
                if i == 30:
                    ns.enabled = False  # mid-stream switch-off: order must hold
                time.sleep(0.002)
            deadline = time.time() + 3
            while m._mic_queue.qsize() < n_blocks and time.time() < deadline:
                time.sleep(0.01)
            while True:
                try:
                    received.append(m._mic_queue.get_nowait())
                except queue.Empty:
                    break
            self.assertEqual(len(received), n_blocks)
            # Blocks after the switch-off are raw: their constant value is the index.
            tail_ids = [int(round(float(b[0]) * 1e3)) for b in received[40:]]
            self.assertEqual(tail_ids, list(range(40, n_blocks)), "blocks overtook each other across NS off")
            self.assertEqual(m._ns_in_q.unfinished_tasks, 0)
        finally:
            m._stop_ns_worker()
            m.noise_suppressor.close()

    def test_worker_start_stop_is_idempotent(self):
        m = self._mixer()
        try:
            m._start_ns_worker()
            t1 = m._ns_thread
            m._start_ns_worker()
            self.assertIs(m._ns_thread, t1)
            m._stop_ns_worker()
            self.assertIsNone(m._ns_thread)
            self.assertFalse(t1.is_alive())
            m._stop_ns_worker()  # no-op
        finally:
            m._stop_ns_worker()


if __name__ == "__main__":
    unittest.main(verbosity=2)
