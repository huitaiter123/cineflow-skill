#!/usr/bin/env python3
"""Seedance task CLI. Standard library only; create is a dry run unless --submit.

API reference: https://docs.volcengine.com/docs/ark/create-video-generation-task-api
Each shot/version must use its own state file. Never delete an uncertain state
to retry: recover its task ID from Ark and use get --task-id instead.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib import error, parse, request

DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"
MAX_RESPONSE = 2 * 1024 * 1024
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
STATUSES = TERMINAL | {"queued", "running"}


class CliError(Exception):
    """A short, safe error suitable for displaying to the operator."""


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CliError("API redirect refused; verify the configured base URL.")


class DownloadRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        # A fresh request ensures no API credentials are forwarded.
        return request.Request(newurl, headers={"User-Agent": "CineFlow/1"})


def validate_url(url):
    p = parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
        raise CliError("Only HTTPS URLs without embedded credentials or fragments are allowed.")
    return url


def base_url(value):
    validate_url(value)
    if parse.urlsplit(value).query:
        raise CliError("API base URL must not contain a query string.")
    return value.rstrip("/")


def task_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", value):
        raise CliError("API task ID is missing or malformed.")
    return value


def api_json(base, method, path, key, payload=None, timeout=30):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(base + path, data=body, method=method,
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json", "User-Agent": "CineFlow/1"})
    try:
        # Authenticated requests never follow redirects, including same-host redirects.
        with request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except error.HTTPError as exc:
        # Do not echo server bodies, headers, exception URLs, or request data.
        raise CliError("API request failed (HTTP %s). Check Ark task history before retrying." % exc.code) from None
    except (error.URLError, TimeoutError, OSError):
        raise CliError("API connection failed or timed out. No automatic resubmission was made.") from None
    if len(raw) > MAX_RESPONSE:
        raise CliError("API response exceeds the allowed size.")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CliError("API returned malformed JSON; check the saved task state.") from None
    if not isinstance(result, dict) or "error" in result:
        raise CliError("API returned an invalid or error response; inspect the task in Ark.")
    return result


def load_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise CliError("Cannot read task state as JSON.") from None
    if not isinstance(state, dict) or state.get("version") != 1:
        raise CliError("Unsupported or malformed task state.")
    base_url(state.get("base_url", ""))
    return state


def write_state(path, state, exclusive=False):
    path = Path(path)
    if exclusive:
        # Reserve before POST. Concurrent invocations cannot both submit.
        try:
            with path.open("x", encoding="utf-8") as f:
                os.chmod(path, 0o600)
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            raise CliError("State already exists; use --resume, get, or poll. No task submitted.") from None
    else:
        fd, name = tempfile.mkstemp(prefix=".cineflow-state-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def image_data(path):
    path = Path(path)
    mime = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp",
            ".bmp": "bmp", ".tif": "tiff", ".tiff": "tiff", ".gif": "gif",
            ".heic": "heic", ".heif": "heif"}.get(path.suffix.lower())
    if mime is None:
        raise CliError("Unsupported frame extension; use a supported image file.")
    if not path.is_file() or not 0 < path.stat().st_size < 30 * 1024 * 1024:
        raise CliError("Frame must be a nonempty image file smaller than 30 MiB.")
    return "data:image/%s;base64,%s" % (mime, base64.b64encode(path.read_bytes()).decode("ascii"))


def build_payload(args):
    model = args.model or os.environ.get("SEEDANCE_MODEL")
    if not model or not model.strip():
        raise CliError("Set --model or the local SEEDANCE_MODEL environment variable.")
    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    if not prompt or not prompt.strip():
        raise CliError("Prompt must not be empty.")
    if args.last_frame and not args.first_frame:
        raise CliError("--last-frame requires --first-frame.")
    if args.duration <= 0:
        raise CliError("Duration must be positive; verify the selected model's supported range.")
    content = [{"type": "text", "text": prompt}]
    for role, path in (("first_frame", args.first_frame), ("last_frame", args.last_frame)):
        if path:
            content.append({"type": "image_url", "image_url": {"url": image_data(path)}, "role": role})
    payload = {"model": model.strip(), "content": content, "duration": args.duration,
               "ratio": args.ratio, "resolution": args.resolution,
               "generate_audio": args.generate_audio, "watermark": args.watermark}
    if len(json.dumps(payload).encode()) >= 64 * 1024 * 1024:
        raise CliError("Encoded request must be smaller than 64 MiB.")
    return payload


def local_cost_guard(args):
    for value in (args.estimated_cost, args.budget):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise CliError("Estimated cost and budget must be finite, nonnegative numbers.")
    if args.budget is not None and args.estimated_cost is None:
        raise CliError("--budget requires --estimated-cost for the local estimate guard.")
    if args.budget is not None and args.estimated_cost > args.budget:
        raise CliError("Local estimated cost exceeds this request's budget; no task submitted.")


def get_key():
    key = os.environ.get("ARK_API_KEY", "").strip()
    if not key:
        raise CliError("Configure ARK_API_KEY in your local environment; never paste it into output.")
    return key


def create(args):
    local_cost_guard(args)
    payload = build_payload(args)
    base = base_url(args.base_url)
    fingerprint = hashlib.sha256(json.dumps({"base_url": base, "payload": payload},
                                sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    path = Path(args.state)
    if path.exists():
        state = load_state(path)
        if state.get("fingerprint") != fingerprint:
            raise CliError("State belongs to a different request; use a new shot/version state file.")
        if state.get("task_id") and args.resume:
            return {"action": "resumed", "task_id": task_id(state["task_id"]),
                    "status": state.get("status", "submitted"), "network": False}
        if state.get("task_id"):
            raise CliError("Task already submitted; use create --resume, get, or poll. No duplicate submitted.")
        raise CliError("Submission outcome is uncertain. Do not recreate; find its task ID in Ark, then use get --task-id with this state.")
    if args.resume:
        raise CliError("Cannot resume: state file does not exist.")
    summary = {"action": "dry-run", "network": False, "fingerprint": fingerprint,
               "model": payload["model"], "duration": args.duration, "ratio": args.ratio,
               "resolution": args.resolution, "image_count": len(payload["content"]) - 1,
               "estimated_cost": args.estimated_cost,
               "cost_note": "Local estimate only; actual billing is determined by Ark. --submit may incur charges."}
    if not args.submit:
        return summary
    key = get_key()
    state = {"version": 1, "fingerprint": fingerprint, "base_url": base,
             "model": payload["model"], "phase": "submission_uncertain",
             "created_at": int(time.time()), "estimated_cost": args.estimated_cost}
    write_state(path, state, exclusive=True)
    result = api_json(base, "POST", "/contents/generations/tasks", key, payload, args.request_timeout)
    state.update(task_id=task_id(result.get("id")), phase="submitted", status="submitted")
    write_state(path, state)
    return {"action": "submitted", "task_id": state["task_id"], "fingerprint": fingerprint}


def retrieve(args):
    state = load_state(args.state)
    recovered_id = getattr(args, "task_id", None)
    if recovered_id:
        recovered_id = task_id(recovered_id)
        if state.get("task_id") and state["task_id"] != recovered_id:
            raise CliError("Explicit task ID does not match the saved task.")
    ident = task_id(recovered_id or state.get("task_id"))
    result = api_json(state["base_url"], "GET", "/contents/generations/tasks/" + ident,
                      get_key(), timeout=args.request_timeout)
    if result.get("id") != ident or result.get("status") not in STATUSES:
        raise CliError("Task response has an unexpected ID or status; saved state was preserved.")
    content = result.get("content", {})
    if not isinstance(content, dict):
        raise CliError("Task response content must be an object.")
    state.update(task_id=ident, phase="submitted", status=result["status"], checked_at=int(time.time()))
    if result["status"] == "succeeded":
        video_url = content.get("video_url")
        if not isinstance(video_url, str):
            raise CliError("Succeeded task is missing content.video_url.")
        state["video_url"] = validate_url(video_url)
    # No raw error messages or content are printed, as they can echo user inputs.
    write_state(args.state, state)
    return {"task_id": ident, "status": state["status"], "download_available": bool(state.get("video_url"))}


def poll(args):
    deadline = time.monotonic() + args.wait_timeout
    while True:
        result = retrieve(args)
        if result["status"] in TERMINAL:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CliError("Polling timed out; state saved. Run poll again to resume, never create a new task.")
        time.sleep(min(args.interval, remaining))


def download(args):
    state = load_state(args.state)
    if state.get("status") != "succeeded" or not isinstance(state.get("video_url"), str):
        raise CliError("No completed video in state; use get or poll first.")
    target = Path(args.output)
    if target.exists():
        raise CliError("Output already exists; choose a new version filename.")
    url = validate_url(state["video_url"])
    req = request.Request(url, headers={"User-Agent": "CineFlow/1"})
    fd, temp_name = tempfile.mkstemp(prefix=".cineflow-download-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            try:
                # A separate opener and request carry no Authorization header.
                with request.build_opener(DownloadRedirect()).open(req, timeout=args.request_timeout) as response:
                    validate_url(response.geturl())
                    total = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > args.max_bytes:
                            raise CliError("Download exceeds --max-bytes.")
                        f.write(chunk)
            except (error.URLError, TimeoutError, OSError):
                raise CliError("Video download failed; task state was preserved. Retry download only.") from None
        if total == 0:
            raise CliError("Video download was empty.")
        # Linking is atomic and refuses an output created by a concurrent process.
        try:
            os.link(temp_name, target)
        except FileExistsError:
            raise CliError("Output already exists; choose a new version filename.") from None
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {"action": "downloaded", "output": str(target), "bytes": total}


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create", help="Dry run by default; --submit can incur charges")
    c.add_argument("--state", required=True, help="One state JSON per shot/version; parent directory must exist")
    prompt = c.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    c.add_argument("--model", help="Model ID, or set SEEDANCE_MODEL locally")
    c.add_argument("--base-url", default=os.environ.get("ARK_BASE_URL", DEFAULT_BASE))
    c.add_argument("--first-frame")
    c.add_argument("--last-frame")
    c.add_argument("--duration", type=int, required=True)
    c.add_argument("--ratio", default="16:9")
    c.add_argument("--resolution", default="720p")
    c.add_argument("--generate-audio", action=argparse.BooleanOptionalAction, default=False)
    c.add_argument("--watermark", action=argparse.BooleanOptionalAction, default=True)
    c.add_argument("--submit", action="store_true", help="Explicitly submit a PAID generation task")
    c.add_argument("--resume", action="store_true", help="Return an existing matching task ID without submitting")
    c.add_argument("--estimated-cost", type=float, help="Operator estimate in the same currency as --budget")
    c.add_argument("--budget", type=float, help="Per-request local estimate limit; not a platform spending cap")
    c.add_argument("--request-timeout", type=positive_float, default=30)
    c.set_defaults(func=create)
    for name, func in (("get", retrieve), ("poll", poll), ("download", download)):
        q = sub.add_parser(name)
        q.add_argument("--state", required=True)
        q.add_argument("--request-timeout", type=positive_float, default=30)
        if name in {"get", "poll"}:
            q.add_argument("--task-id", help="Recover an uncertain submission using its ID from Ark")
        if name == "poll":
            q.add_argument("--interval", type=positive_float, default=5)
            q.add_argument("--wait-timeout", type=positive_float, default=600)
        if name == "download":
            q.add_argument("--output", required=True)
            q.add_argument("--max-bytes", type=int, default=1024 * 1024 * 1024)
        q.set_defaults(func=func)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = args.func(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CliError as exc:
        print("Error: " + str(exc)[:360], file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, TypeError):
        # Avoid tracebacks that might contain signed URLs, headers, or image data.
        print("Error: Invalid local file, state, URL, or argument; no automatic retry was made.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
