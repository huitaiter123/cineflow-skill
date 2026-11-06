"""Offline contract tests; fake binaries record argv and emulate file outputs."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "cineflow" / "scripts" / "video_tools.py"
SPEC = importlib.util.spec_from_file_location("cineflow_video_tools", SCRIPT)
video_tools = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(video_tools)


class VideoToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cineflow-tools-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.log = self.root / "calls.jsonl"
        self.ffprobe = self._fake("ffprobe", "probe")
        self.ffmpeg = self._fake("ffmpeg", "encode")
        self.first = self.root / "first clip.mp4"
        self.second = self.root / "Bob's clip.mp4"
        self.first.write_bytes(b"input A")
        self.second.write_bytes(b"input B")

    def _fake(self, name, mode):
        path = self.root / name
        body = textwrap.dedent("""\
            import json, os, pathlib, sys
            args = sys.argv[1:]
            entry = {"tool": MODE, "args": args}
            if MODE == "encode" and "concat" in args:
                listing = pathlib.Path(args[args.index("-i") + 1])
                entry["list_path"] = str(listing)
                entry["list_content"] = listing.read_text(encoding="utf-8")
            with open(LOG, "a", encoding="utf-8") as target:
                target.write(json.dumps(entry) + "\\n")
            if os.environ.get("CINEFLOW_FAKE_FAILURE") == MODE:
                print("synthetic decode error", file=sys.stderr)
                sys.exit(7)
            if MODE == "probe":
                dimensions = 1920 if "mismatch" in args[-1] else 1280
                print(json.dumps({
                    "format": {"duration": "5.1", "format_name": "mov,mp4"},
                    "streams": [{"codec_type": "video", "codec_name": "h264",
                        "width": dimensions, "height": 720, "pix_fmt": "yuv420p",
                        "time_base": "1/12800", "r_frame_rate": "25/1",
                        "color_space": "bt709"}]
                }))
            else:
                pathlib.Path(args[-1]).write_bytes(b"synthetic FFmpeg output")
            """)
        path.write_text(
            "#!" + sys.executable + "\nMODE = " + repr(mode) + "\nLOG = " + repr(str(self.log)) + "\n" + body,
            encoding="utf-8",
        )
        path.chmod(0o755)
        return str(path)

    def _calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_probe_returns_json_metadata_without_shell_interpretation(self):
        malicious_name = self.root / "clip $(touch should-not-exist); `whoami`.mp4"
        malicious_name.write_bytes(b"input")
        result = video_tools.probe(malicious_name, self.ffprobe)
        self.assertEqual(result["streams"][0]["color_space"], "bt709")
        self.assertEqual(self._calls()[0]["args"][-1], str(malicious_name))
        self.assertFalse((self.root / "should-not-exist").exists())

    def test_missing_binary_gives_actionable_error(self):
        with patch.object(video_tools.shutil, "which", return_value=None):
            with self.assertRaisesRegex(video_tools.VideoToolsError, "ffprobe was not found.*--ffprobe"):
                video_tools.probe(self.first)

    def test_concat_quotes_spaces_and_apostrophes_and_cleans_list(self):
        output = self.root / "joined result.mp4"
        result = video_tools.concat([self.first, self.second], output, self.ffmpeg, self.ffprobe)
        self.assertEqual(result["mode"], "stream copy")
        self.assertTrue(output.is_file())
        call = next(c for c in self._calls() if c["tool"] == "encode")
        self.assertIn("file '" + self.first.as_posix() + "'\n", call["list_content"])
        self.assertIn("Bob'\\''s clip.mp4", call["list_content"])
        self.assertFalse(Path(call["list_path"]).exists())
        self.assertEqual(call["args"][call["args"].index("-c") + 1], "copy")
        self.assertNotIn("-vf", call["args"])
        self.assertNotIn("-colorspace", call["args"])

    def test_concat_rejects_stream_mismatch_before_encoding(self):
        mismatch = self.root / "mismatch.mp4"
        mismatch.write_bytes(b"different dimensions")
        with self.assertRaisesRegex(video_tools.VideoToolsError, "Stream parameters differ"):
            video_tools.concat([self.first, mismatch], self.root / "out.mp4", self.ffmpeg, self.ffprobe)
        self.assertFalse(any(c["tool"] == "encode" for c in self._calls()))

    def test_concat_rejects_newlines_in_list_entries(self):
        broken = self.root / "line\nbreak.mp4"
        broken.write_bytes(b"input")
        with self.assertRaisesRegex(video_tools.VideoToolsError, "line breaks"):
            video_tools.concat([self.first, broken], self.root / "out.mp4", self.ffmpeg, self.ffprobe)

    def test_existing_output_is_preserved(self):
        output = self.root / "keep.mp4"
        output.write_bytes(b"keep this exact content")
        with self.assertRaisesRegex(video_tools.VideoToolsError, "already exists"):
            video_tools.concat([self.first, self.second], output, self.ffmpeg, self.ffprobe)
        self.assertEqual(output.read_bytes(), b"keep this exact content")
        self.assertFalse(self._calls())

    def test_dangling_symlink_is_not_followed_or_replaced(self):
        output = self.root / "keep.mp4"
        output.symlink_to(self.root / "nonexistent-target.mp4")
        with self.assertRaisesRegex(video_tools.VideoToolsError, "already exists"):
            video_tools.concat([self.first, self.second], output, self.ffmpeg, self.ffprobe)
        self.assertTrue(output.is_symlink())
        self.assertFalse(output.exists())

    def test_failure_cleans_list_and_leaves_no_final_output(self):
        output = self.root / "out.mp4"
        with patch.dict(os.environ, {"CINEFLOW_FAKE_FAILURE": "encode"}):
            with self.assertRaisesRegex(video_tools.VideoToolsError, "synthetic decode error"):
                video_tools.concat([self.first, self.second], output, self.ffmpeg, self.ffprobe)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.root.glob(".cineflow-*")))
        self.assertFalse(Path(self._calls()[-1]["list_path"]).exists())

    def test_interval_sheet_caps_samples_and_reports_positions(self):
        result = video_tools.contact_sheet(
            self.first, self.root / "sheet.png", interval=2, max_frames=2,
            ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
        )
        self.assertEqual(result["sample_times_seconds"], [0, 2])
        self.assertEqual((result["columns"], result["rows"]), (2, 1))
        calls = [c for c in self._calls() if c["tool"] == "encode"]
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1]["args"][calls[1]["args"].index("-ss") + 1], "2.000000")
        self.assertFalse(list(self.root.glob(".cineflow-*")))

    def test_explicit_times_are_sorted_and_range_checked(self):
        result = video_tools.contact_sheet(
            self.first, self.root / "sheet.jpg", times=[4, 0, 4, 1.5],
            ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
        )
        self.assertEqual(result["sample_times_seconds"], [0, 1.5, 4])
        with self.assertRaisesRegex(video_tools.VideoToolsError, "earlier than"):
            video_tools.contact_sheet(
                self.first, self.root / "bad.png", times=[5.1],
                ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
            )

    def test_invalid_interval_is_rejected(self):
        for value in [0, -1, float("nan"), float("inf")]:
            with self.subTest(interval=value):
                with self.assertRaisesRegex(video_tools.VideoToolsError, "interval"):
                    video_tools.contact_sheet(
                        self.first, self.root / "bad.png", interval=value,
                        ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
                    )

    def test_publish_does_not_overwrite_a_late_arriving_file(self):
        staged, output = self.root / "staged.mp4", self.root / "late.mp4"
        staged.write_bytes(b"new")
        output.write_bytes(b"arrived while rendering")
        with self.assertRaisesRegex(video_tools.VideoToolsError, "already exists"):
            video_tools._publish(staged, output)
        self.assertEqual(output.read_bytes(), b"arrived while rendering")

    def test_cli_help_and_invalid_times(self):
        help_result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("contact-sheet", help_result.stdout)
        invalid = subprocess.run(
            [sys.executable, str(SCRIPT), "contact-sheet", str(self.first), "-o", "out.png", "--times", "nan"],
            capture_output=True, text=True,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("finite", invalid.stderr)
        self.assertNotIn("Traceback", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
