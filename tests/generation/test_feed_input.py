"""Real tiny FFmpeg fixtures for the mandatory first-frame policy."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from xmax_test.assets.validator import MediaValidator
from xmax_test.errors import ValidationError
from xmax_test.generation.feed_input import FfmpegFeedPreprocessor
from xmax_test.hashing import file_sha256
from xmax_test.storage.artifacts import ArtifactStore


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class FeedInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.processor = FfmpegFeedPreprocessor(ArtifactStore(self.root / "artifacts"), MediaValidator())

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, *, vfr=False, single=False, audio=True):
        path = self.root / "source.mp4"
        cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10:duration=1"]
        if audio:
            cmd += ["-f", "lavfi", "-i", "sine=frequency=400:duration=1"]
        if vfr:
            cmd += ["-vf", "select='eq(n,0)+gte(n,3)'", "-fps_mode", "vfr"]
        if single:
            cmd += ["-frames:v", "1"]
        subprocess.run(cmd + ["-c:v", "libx264", "-c:a", "aac", str(path)], check=True, capture_output=True)
        return {"asset_id": "source", "kind": "feed_video", "path": str(path), "sha256": file_sha256(path)}

    def test_cfr_preserves_remaining_frames_audio_and_idempotency(self):
        source = self.source()
        output = self.processor.prepare(source)
        proof = output["feed_preprocessing"]
        self.assertEqual((proof["source_frames"], proof["output_frames"]), (10, 9))
        self.assertAlmostEqual(proof["removed_duration_s"], .1)
        self.assertTrue(output["media"]["has_audio"])
        self.assertEqual(file_sha256(Path(source["path"])), source["sha256"])
        self.assertEqual(self.processor.prepare(source), output)
        self.assertEqual(self.processor.prepare(output), output)
        # Verify the first retained picture, not only container/frame counts.
        def pixels(path, select):
            return subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", select,
                "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
        expected = pixels(source["path"], "select=eq(n\\,1)")
        actual = pixels(output["path"], "select=eq(n\\,0)")
        self.assertLess(sum(abs(a-b) for a,b in zip(expected, actual))/len(expected), 5)

    def test_vfr_uses_next_frame_timestamp_and_accepts_silent_video(self):
        output = self.processor.prepare(self.source(vfr=True, audio=False))
        self.assertAlmostEqual(output["feed_preprocessing"]["removed_duration_s"], .3)
        self.assertEqual(output["feed_preprocessing"]["output_frames"], 7)
        self.assertFalse(output["media"]["has_audio"])

    def test_single_frame_and_corrupt_cache_fail_before_generation(self):
        with self.assertRaises(ValidationError):
            self.processor.prepare(self.source(single=True, audio=False))
        Path(self.root / "source.mp4").unlink()
        source = self.source()
        output = self.processor.prepare(source)
        Path(output["path"]).write_bytes(b"corrupt")
        with self.assertRaises(ValidationError):
            self.processor.prepare(source)

    def test_prompt_video_is_untouched(self):
        source = {"kind": "prompt_video", "path": "not-accessed"}
        self.assertIs(self.processor.prepare(source), source)
