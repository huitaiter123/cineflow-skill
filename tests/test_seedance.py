"""Offline checks: no test calls a live model or incurs generation charges."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib import error, request

SPEC = importlib.util.spec_from_file_location("cineflow_seedance", Path(__file__).parents[1] / "cineflow" / "scripts" / "seedance.py")
seedance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seedance)


class Response:
    def __init__(self, body, url="https://media.example/video.mp4"):
        self.stream = io.BytesIO(body)
        self.url = url

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, size=-1): return self.stream.read(size)
    def geturl(self): return self.url


class SeedanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "shot-v1.json"
        self.frame = self.root / "frame.png"
        self.frame.write_bytes(b"\x89PNG\r\n\x1a\nframe-test")
        self.env = mock.patch.dict(os.environ, {"ARK_API_KEY": "LOCAL-SECRET-NOT-FOR-OUTPUT"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def args(self, *extra):
        return ["create", "--state", str(self.state), "--model", "test-model", "--prompt", "A leaf moves.",
                "--duration", "5", "--first-frame", str(self.frame)] + list(extra)

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = seedance.main(args)
        self.assertNotIn(os.environ["ARK_API_KEY"], out.getvalue() + err.getvalue())
        self.assertNotIn("data:image/", out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def saved(self, **changes):
        state = {"version": 1, "base_url": seedance.DEFAULT_BASE, "fingerprint": "test", "task_id": "cgt-test", "status": "running"}
        state.update(changes)
        self.state.write_text(json.dumps(state))
        return state

    def test_dry_run_never_networks_or_requires_key(self):
        os.environ.pop("ARK_API_KEY")
        with mock.patch.object(seedance.request, "build_opener") as net:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = seedance.main(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["action"], "dry-run")
        self.assertNotIn("data:image/", out.getvalue())
        self.assertFalse(self.state.exists())
        net.assert_not_called()

    def test_frames_are_correctly_encoded_in_official_fields(self):
        args = seedance.parser().parse_args(self.args("--last-frame", str(self.frame)))
        payload = seedance.build_payload(args)
        self.assertEqual([c.get("role") for c in payload["content"][1:]], ["first_frame", "last_frame"])
        self.assertEqual(payload["content"][1]["image_url"]["url"], seedance.image_data(self.frame))
        self.assertEqual(payload["model"], "test-model")

    def test_success_duplicate_refused_resume_does_not_submit(self):
        with mock.patch.object(seedance, "api_json", return_value={"id": "cgt-test"}) as api:
            self.assertEqual(self.run_cli(self.args("--submit"))[0], 0)
            self.assertEqual(self.run_cli(self.args("--submit"))[0], 2)
            code, out, _ = self.run_cli(self.args("--submit", "--resume"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["action"], "resumed")
        self.assertEqual(api.call_count, 1)
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved["task_id"], "cgt-test")
        self.assertEqual(len(saved["fingerprint"]), 64)
        self.assertNotIn("data:image/", self.state.read_text())

    def test_changed_request_cannot_reuse_state(self):
        with mock.patch.object(seedance, "api_json", return_value={"id": "cgt-test"}) as api:
            self.run_cli(self.args("--submit"))
            args = self.args("--submit", "--resume")
            args[args.index("A leaf moves.")] = "A different scene."
            self.assertEqual(self.run_cli(args)[0], 2)
            self.assertEqual(api.call_count, 1)

    def test_timeout_reserves_uncertain_state_and_blocks_recreate(self):
        with mock.patch.object(seedance, "api_json", side_effect=seedance.CliError("API connection timed out")) as api:
            self.assertEqual(self.run_cli(self.args("--submit"))[0], 2)
            self.assertEqual(json.loads(self.state.read_text())["phase"], "submission_uncertain")
            self.assertEqual(self.run_cli(self.args("--submit"))[0], 2)
            self.assertEqual(api.call_count, 1)

    def test_uncertain_state_can_recover_known_task_without_post(self):
        self.saved(task_id=None, phase="submission_uncertain")
        with mock.patch.object(seedance, "api_json", return_value={"id": "cgt-recovered", "status": "running"}) as api:
            code, _, _ = self.run_cli(["get", "--state", str(self.state), "--task-id", "cgt-recovered"])
        self.assertEqual(code, 0)
        self.assertEqual(api.call_args.args[1], "GET")
        self.assertEqual(json.loads(self.state.read_text())["task_id"], "cgt-recovered")

    def test_missing_create_id_remains_uncertain(self):
        with mock.patch.object(seedance, "api_json", return_value={"unexpected": "value"}):
            self.assertEqual(self.run_cli(self.args("--submit"))[0], 2)
        self.assertEqual(json.loads(self.state.read_text())["phase"], "submission_uncertain")

    def test_api_malformed_json_and_error_bodies_do_not_leak(self):
        for body in (b"not json", b"[]", b'{"error":{"message":"LOCAL-SECRET-NOT-FOR-OUTPUT"}}'):
            with self.subTest(body=body), mock.patch.object(seedance.request, "build_opener") as build:
                build.return_value.open.return_value = Response(body)
                self.assertEqual(self.run_cli(self.args("--submit"))[0], 2)
            self.state.unlink()

    def test_authenticated_api_disallows_redirect(self):
        req = request.Request(seedance.DEFAULT_BASE, headers={"Authorization": "Bearer secret"})
        with self.assertRaises(seedance.CliError):
            seedance.NoRedirect().redirect_request(req, None, 302, "", {}, "https://external.example")

    def test_api_and_download_headers_are_isolated(self):
        with mock.patch.object(seedance.request, "build_opener") as build:
            build.return_value.open.return_value = Response(b'{"id":"cgt-test"}')
            self.assertEqual(self.run_cli(self.args("--submit"))[0], 0)
            api_req = build.return_value.open.call_args.args[0]
            self.assertEqual(api_req.get_header("Authorization"), "Bearer " + os.environ["ARK_API_KEY"])
        self.saved(status="succeeded", video_url="https://external.example/video.mp4")
        output = self.root / "video.mp4"
        with mock.patch.object(seedance.request, "build_opener") as build:
            build.return_value.open.return_value = Response(b"video-bytes", "https://external.example/video.mp4")
            self.assertEqual(self.run_cli(["download", "--state", str(self.state), "--output", str(output)])[0], 0)
            download_req = build.return_value.open.call_args.args[0]
            self.assertIsNone(download_req.get_header("Authorization"))
        self.assertEqual(output.read_bytes(), b"video-bytes")
        redirected = seedance.DownloadRedirect().redirect_request(api_req, None, 302, "", {}, "https://cdn.example/video.mp4")
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_download_refuses_overwrite_without_network(self):
        self.saved(status="succeeded", video_url="https://external.example/video.mp4")
        output = self.root / "video.mp4"
        output.write_bytes(b"original")
        with mock.patch.object(seedance.request, "build_opener") as net:
            self.assertEqual(self.run_cli(["download", "--state", str(self.state), "--output", str(output)])[0], 2)
        self.assertEqual(output.read_bytes(), b"original")
        net.assert_not_called()

    def test_poll_resumes_without_create_and_timeout_keeps_state(self):
        self.saved()
        result = {"task_id": "cgt-test", "status": "running", "download_available": False}
        with mock.patch.object(seedance, "retrieve", return_value=result) as get, mock.patch.object(seedance.time, "monotonic", side_effect=[0, 2]):
            code, _, err = self.run_cli(["poll", "--state", str(self.state), "--wait-timeout", "1"])
        self.assertEqual(code, 2)
        self.assertIn("resume", err)
        self.assertEqual(get.call_count, 1)
        self.assertTrue(self.state.exists())

    def test_malformed_get_response_does_not_change_state(self):
        self.saved()
        before = self.state.read_bytes()
        for result in ({"id": "different", "status": "running"}, {"id": "cgt-test", "status": "bogus"}, {"id": "cgt-test", "status": "succeeded"}):
            with mock.patch.object(seedance, "api_json", return_value=result):
                self.assertEqual(self.run_cli(["get", "--state", str(self.state)])[0], 2)
            self.assertEqual(self.state.read_bytes(), before)

    def test_budget_guard_blocks_network(self):
        with mock.patch.object(seedance, "api_json") as api:
            for extra in (("--estimated-cost", "10", "--budget", "5"), ("--budget", "5"), ("--estimated-cost", "nan")):
                self.assertEqual(self.run_cli(self.args("--submit", *extra))[0], 2)
            api.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_http_error_does_not_echo_response_or_url(self):
        exception = error.HTTPError("https://example/?secret=LOCAL-SECRET-NOT-FOR-OUTPUT", 401, "Authorization: secret", {}, io.BytesIO(b"data:image/png;base64,secret"))
        with mock.patch.object(seedance.request, "build_opener") as build:
            build.return_value.open.side_effect = exception
            code, _, err = self.run_cli(self.args("--submit"))
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401", err)


if __name__ == "__main__":
    unittest.main()
