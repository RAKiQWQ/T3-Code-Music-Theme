"""Focused regressions; no commands are sent to the installed player."""
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import t3music as m


def filtered(eq, signal):
    return np.concatenate([eq.process(signal[i:i + m.CHUNK]).copy()
                           for i in range(0, len(signal), m.CHUNK)])


def tone(freq, seconds=1):
    signal = np.sin(np.arange(int(seconds * m.RATE)) * 2 * np.pi * freq / m.RATE)
    return np.column_stack((signal, signal)).astype(np.float32)


def amplitude_db(signal):
    center = signal[len(signal) // 4: -len(signal) // 4, 0]
    return 20 * math.log10(math.sqrt(2 * float(np.mean(center ** 2))))


class EqualizerTests(unittest.TestCase):
    def test_flat_and_disabled_preserve_pcm(self):
        signal = tone(440)
        for eq in (m.Equalizer(), m.Equalizer([6] * 10, enabled=False)):
            block = signal[:m.CHUNK]
            self.assertIs(eq.process(block), block)
            np.testing.assert_array_equal(filtered(eq, signal), signal)

    def test_center_gain_distant_bands_and_boost_compensation(self):
        gains = [0] * 10
        gains[5] = -6
        for freq, expected in ((1000, -6), (62, 0), (16000, 0)):
            self.assertAlmostEqual(amplitude_db(filtered(m.Equalizer(gains), tone(freq))),
                                   expected, delta=0.3)
        gains = [12] + [0] * 9
        eq = m.Equalizer(gains)
        self.assertAlmostEqual(eq.headroom_db, 12, delta=0.1)
        self.assertAlmostEqual(amplitude_db(filtered(eq, tone(31, 3))), 0, delta=0.3)
        gains[0] = -12
        self.assertAlmostEqual(amplitude_db(filtered(m.Equalizer(gains), tone(31, 3))),
                               -12, delta=0.3)

    def test_uneven_blocks_match_full_convolution_and_reset_drops_tail(self):
        eq = m.Equalizer([5, 4, 2, 0, -2, 1, 1, 2, 4, 5])
        sizes = (m.EQ_BLOCK, m.EQ_BLOCK, 17, m.CHUNK, 1, 500)
        signal = np.random.default_rng(7).normal(0, 0.1, (sum(sizes), 2)).astype(np.float32)
        taps = np.fft.irfft(eq.curve[:, 0], m.EQ_FFT)[:m.EQ_TAPS]
        expected = np.column_stack([np.convolve(signal[:, channel], taps)[:len(signal)]
                                    for channel in (0, 1)])
        parts, offset = [], 0
        for size in sizes:
            parts.append(eq.process(signal[offset:offset + size]).copy())
            offset += size
        np.testing.assert_allclose(np.concatenate(parts), expected, atol=1e-7, rtol=1e-6)
        eq.reset()
        np.testing.assert_array_equal(eq.process(np.zeros((17, 2))), 0)

    def test_invalid_curves_are_rejected(self):
        for gains in ([0] * 9, [13] * 10, [float("nan")] * 10, [float("inf")] * 10):
            with self.subTest(gains=gains), self.assertRaises(ValueError):
                m.Equalizer(gains)


class FakeTrack:
    def __init__(self, path):
        self.path = path
        self.title, self.sub = path.stem, path.parent.name
        self.done, self.closed = True, False
        self.error = ""
        self.decoded = m.RATE * 2
        self.pcm = np.zeros((self.decoded, 2), np.float32)

    @property
    def frames(self):
        return self.decoded

    def close(self):
        self.closed = True


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="t3-music-unit-")
        self.root = Path(self.temp.name).resolve()
        self.paths = [self.root / name for name in ("2 & あ%.wav", "10.wav", "20.wav")]
        for path in self.paths:
            path.touch()
        self.patches = [
            patch.object(m, "Track", FakeTrack),
            patch.object(m, "STATE_FILE", self.root / "state.json"),
            patch.object(m, "PLAYLIST_DIR", self.root / "saved"),
            patch.object(m, "LOG_FILE", self.root / "check.log"),
        ]
        for item in self.patches:
            item.start()
        self.app = m.App()

    def tearDown(self):
        self.app.unload()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def request(self, command, *args):
        reply = queue.Queue()
        self.app.handle(("cmd", json.dumps({"command": command, "args": list(args)}), reply))
        return json.loads(reply.get_nowait())

    def populate(self):
        result = self.request("playlist", "add", str(self.root))
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(self.app.playlist, self.paths)

    def test_append_search_move_remove_and_clear_preserve_current_audio(self):
        self.populate()
        track = self.app.track
        self.request("play")
        self.request("playlist", "add", str(self.paths[0]))
        self.assertEqual(len(self.app.playlist), 3)
        result = self.request("playlist", "list", "あ%")
        self.assertEqual([entry["path"] for entry in result["playlist"]], [str(self.paths[0])])
        self.request("playlist", "move", "1", "3")
        self.assertIs(self.app.track, track)
        self.assertTrue(self.app.engine.playing)
        self.assertEqual(self.app.index, 2)
        self.request("playlist", "remove", "1")
        self.assertIs(self.app.track, track)
        self.assertEqual(self.app.index, 1)
        self.request("playlist", "remove", "2")
        self.assertTrue(track.closed)
        self.assertEqual(self.app.track.path, self.paths[2])
        self.assertTrue(self.app.engine.playing)
        self.request("playlist", "clear")
        self.assertEqual(self.app.playlist, [])
        self.assertIsNone(self.app.track)
        self.assertFalse(self.app.engine.playing)

    def test_saved_queue_skips_missing_files_and_state_restores_eq_paused(self):
        self.populate()
        self.request("playlist", "move", "3", "1")
        self.request("playlist", "repeat", "one")
        self.assertTrue(self.request("playlist", "save", "Evening & あ%")["ok"])
        self.paths[1].unlink()
        self.request("playlist", "clear")
        loaded = self.request("playlist", "load", "Evening & あ%")
        self.assertTrue(loaded["ok"], loaded.get("message"))
        self.assertEqual(self.app.playlist, [self.paths[2], self.paths[0]])
        self.assertEqual(self.app.repeat, "one")
        self.request("eq", "set", "1k", "-4")
        self.request("eq", "off")
        self.request("vol", "37")
        self.request("seek", "0.5")
        self.request("play")
        self.app.save_state()
        restored = m.App()
        try:
            restored.restore_state(restored.load_state())
            self.assertEqual(restored.playlist, self.app.playlist)
            self.assertFalse(restored.engine.playing)
            self.assertAlmostEqual(restored.engine.playhead() / m.RATE, 0.5)
            self.assertAlmostEqual(restored.engine.gain, 0.37)
            self.assertEqual(restored.engine.eq.gains[5], -4)
            self.assertFalse(restored.engine.eq.enabled)
            self.assertEqual(restored.repeat, "one")
        finally:
            restored.unload()

    def test_repeat_and_failed_decode_stopping(self):
        self.populate()
        self.request("playlist", "play", "3")
        self.request("playlist", "repeat", "off")
        self.app.handle(("ended", self.app.track))
        self.assertFalse(self.app.engine.playing)
        self.assertEqual(self.app.index, 2)
        self.request("playlist", "repeat", "one")
        self.app.handle(("ended", self.app.track))
        self.assertEqual(self.app.index, 2)
        self.assertTrue(self.app.engine.playing)
        self.request("next")
        self.assertEqual(self.app.index, 0)
        self.request("playlist", "repeat", "all")
        for _ in self.paths:
            self.app.track.decoded = 0
            self.app.handle(("ended", self.app.track))
        self.assertFalse(self.app.engine.playing)

    def test_invalid_commands_do_not_mutate_queue_or_eq(self):
        self.populate()
        before = self.app.playlist.copy()
        for command, args in (
            ("playlist", ["remove", "0"]), ("playlist", ["move", "1"]),
            ("playlist", ["save", "../escape"]), ("playlist", ["save", "CON"]),
            ("eq", ["set", "1k", "13"]), ("eq", ["curve", "0", "0"]),
            ("vol", ["nan"]), ("seek", ["inf"]), ("unknown", []),
        ):
            with self.subTest(command=command, args=args):
                result = self.request(command, *args)
                self.assertFalse(result["ok"])
                self.assertEqual(self.app.playlist, before)
                self.assertEqual(self.app.engine.eq.gains, [0] * 10)

    def test_legacy_state_and_empty_queue(self):
        self.app.restore_state({"root": str(self.root), "path": str(self.paths[1]),
                                "volume": 42, "position": 0.25})
        self.assertEqual(self.app.track.path, self.paths[1])
        self.assertEqual(self.app.index, 1)
        self.request("playlist", "clear")
        self.app.save_state()
        self.app.restore_state(self.app.load_state())
        self.assertEqual(self.app.playlist, [])
        self.assertIsNone(self.app.track)

    def test_malformed_json_requests_return_errors_without_crashing(self):
        self.populate()
        for line in ('{broken', '{"command":42}', '{"command":"eq","args":[null]}'):
            with self.subTest(line=line):
                reply = queue.Queue()
                self.assertFalse(self.app.handle(("cmd", line, reply)))
                response = json.loads(reply.get_nowait())
                self.assertFalse(response["ok"])
                self.assertEqual(self.app.playlist, self.paths)

    def test_seek_and_track_change_clear_eq_tail(self):
        self.populate()
        self.app.engine.set_eq([6] + [0] * 9)
        self.app.engine.eq.process(np.ones((m.CHUNK, 2)))
        self.assertIsNotNone(self.app.engine.eq.tail)
        self.request("seek", "0.5")
        self.assertIsNone(self.app.engine.eq.tail)
        self.app.engine.eq.process(np.ones((m.CHUNK, 2)))
        self.request("next")
        self.assertIsNone(self.app.engine.eq.tail)


class AudioAndThemeTests(unittest.TestCase):
    def test_playhead_excludes_audio_waiting_in_the_eq_block(self):
        engine = m.Engine(queue.Queue())
        engine.set_track(FakeTrack(Path("tone.wav")), pos=m.EQ_BLOCK, playing=True)
        engine.ready = np.zeros((m.EQ_BLOCK - m.CHUNK, 2), np.float32)
        engine.pad = m.CHUNK // 2
        self.assertEqual(engine.playhead(), m.CHUNK // 2)
        engine.seek(m.RATE)
        self.assertIsNone(engine.ready)
        self.assertEqual(engine.playhead(), m.RATE)

    @unittest.skipUnless(os.name == "nt" and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "Windows and FFmpeg required")
    def test_real_decoder_preserves_stereo_24_bit_samples(self):
        with tempfile.TemporaryDirectory(prefix="t3-music-pcm-") as temp:
            path = Path(temp) / "24bit.wav"
            left = (np.arange(9600) * 997 % 8000000 - 4000000).astype(np.int32)
            samples = np.column_stack((left, -left + 1))
            values = samples.reshape(-1) & 0xFFFFFF
            packed = np.column_stack((values & 255, values >> 8 & 255, values >> 16 & 255))
            with wave.open(str(path), "wb") as out:
                out.setparams((2, 3, m.RATE, 0, "NONE", "not compressed"))
                out.writeframes(packed.astype(np.uint8).tobytes())
            track = m.Track(path)
            try:
                deadline = time.monotonic() + 10
                while not track.done and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(track.done)
                self.assertEqual(track.decoded, len(samples), track.error)
                np.testing.assert_allclose(track.pcm[:track.decoded], samples / 8388608,
                                           atol=1e-7, rtol=0)
            finally:
                track.close()

    @unittest.skipUnless(os.name == "nt", "Windows SoundCard backend required")
    def test_writer_releases_only_supplied_frames(self):
        from soundcard.mediafoundation import _ffi
        signal = np.arange(74, dtype=np.float32).reshape(37, 2)
        class Output:
            def _render_available_frames(self):
                return 4800
            def _render_buffer(self, frames):
                self.frames = frames
                self.buffer = _ffi.new("char[]", frames * 8)
                return [self.buffer]
            def _render_release(self, frames):
                self.released = frames
        out = Output()
        m.write_frames(out, signal)
        self.assertEqual(out.frames, 37)
        self.assertEqual(out.released, 37)
        self.assertEqual(bytes(_ffi.buffer(out.buffer, 37 * 8)), signal.tobytes())

    def test_spectrum_tracks_tone_eq_and_volume_and_css_fits_budget(self):
        track = FakeTrack(Path("tone.wav"))
        track.pcm = tone(1000, 2)
        analyzer = m.Analyzer()
        original = analyzer.levels(track, m.RATE // 2, m.FRAMES)
        eq = m.Equalizer([0] * 5 + [-12] + [0] * 4)
        cut = analyzer.levels(track, m.RATE // 2, m.FRAMES, eq, 0.1)
        edges = np.geomspace(m.F_LO, m.F_HI, m.BARS + 1)
        band = original[0].argmax()
        self.assertLess(edges[band], 1100)
        self.assertGreater(edges[band + 1], 900)
        self.assertLess(cut[0, band], original[0, band])
        css = m.static_css() + m.dynamic_css(
            gen=3, title='Music "quoted" url(test)', sub="Artist & Album",
            playing=True, pos=60, dur=242, levels=np.full((m.FRAMES, m.BARS), 99))
        self.assertLess(len(css), 50000)
        self.assertNotIn(chr(92), css)
        self.assertNotIn("url(test)", css)


@unittest.skipUnless(os.name == "nt", "Windows background process required")
class BackgroundStartupTests(unittest.TestCase):
    def test_concurrent_pythonw_start_is_single_instance_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory(prefix="t3-music-start-") as temp:
            root = Path(temp)
            env = {**os.environ, "T3_MUSIC_HOME": str(root),
                   "T3_MUSIC_USERDATA": str(root / "userdata"), "T3_MUSIC_HEADLESS": "1"}
            (root / "t3music.state.json").write_text(
                json.dumps({"volume": 0, "playlist": []}), "utf-8")
            command = [sys.executable, str(Path(m.__file__).resolve()), "--json"]
            processes = []
            try:
                for _ in range(2):
                    processes.append(subprocess.Popen(
                        command + ["start"], env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, creationflags=m.NO_WINDOW))
                results = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace"))
                    results.append(json.loads(stdout))
                self.assertTrue(all(result["ok"] for result in results))
                self.assertEqual(results[0]["pid"], results[1]["pid"])
                self.assertEqual(results[0]["track_count"], 0)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
                subprocess.run(command + ["stop"], env=env, capture_output=True,
                               timeout=30, creationflags=m.NO_WINDOW)
                deadline = time.monotonic() + 8
                while (root / "t3music.port").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse((root / "t3music.port").exists())
                # The port disappears immediately before the process releases
                # its lifetime lock; wait on that lock before temp cleanup.
                import msvcrt
                while (root / "t3music.lock").exists():
                    try:
                        with open(root / "t3music.lock", "r+b") as lock:
                            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
