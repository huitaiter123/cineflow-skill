#!/usr/bin/env python3
"""Small, local FFmpeg helpers for CineFlow; Python 3.9+, no pip dependencies."""

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


class VideoToolsError(Exception):
    """An actionable error suitable for display without a traceback."""


def _tool(name, override=None):
    candidate = override or os.environ.get(name.upper() + "_BIN") or name
    resolved = shutil.which(candidate)
    if not resolved:
        raise VideoToolsError(
            "{} was not found. Install FFmpeg or pass --{} /path/to/{} "
            "before the command.".format(name, name, name)
        )
    return resolved


def _run(arguments, timeout=300):
    try:
        result = subprocess.run(
            [str(value) for value in arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoToolsError("{} timed out.".format(Path(arguments[0]).name)) from exc
    except OSError as exc:
        raise VideoToolsError("Could not run {}: {}".format(arguments[0], exc)) from exc
    if result.returncode:
        detail = result.stderr.strip()[-2000:] or "No diagnostic output."
        raise VideoToolsError(
            "{} failed (exit {}): {}".format(
                Path(arguments[0]).name, result.returncode, detail
            )
        )
    return result.stdout


def _input_path(value):
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise VideoToolsError("Input file does not exist: {}".format(path))
    return path


def _output_path(value):
    # Do not resolve the final component: an existing dangling symlink is also
    # an occupied output name, and must never redirect or replace another file.
    raw = Path(value).expanduser().absolute()
    path = raw.parent.resolve() / raw.name
    if os.path.lexists(path):
        raise VideoToolsError("Output already exists; choose a new name: {}".format(path))
    if not path.parent.is_dir():
        raise VideoToolsError("Output directory does not exist: {}".format(path.parent))
    return path


def _publish(staged, output):
    if not staged.is_file() or staged.stat().st_size == 0:
        raise VideoToolsError("FFmpeg did not create a nonempty output file.")
    # Staging beside the destination allows an atomic, no-replace hard link.
    # Even a second process creating the destination during rendering wins.
    try:
        os.link(staged, output)
    except FileExistsError as exc:
        raise VideoToolsError("Output already exists; choose a new name: {}".format(output)) from exc
    except OSError as exc:
        raise VideoToolsError("Could not publish output without overwriting: {}".format(exc)) from exc


def probe(input_file, ffprobe=None):
    """Return FFprobe's stream and format metadata as a JSON-compatible dict."""
    source = _input_path(input_file)
    raw = _run([
        _tool("ffprobe", ffprobe), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_data", str(source),
    ])
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VideoToolsError("ffprobe returned invalid JSON.") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("streams"), list):
        raise VideoToolsError("ffprobe did not return stream metadata.")
    return metadata


def _duration(metadata):
    candidates = [metadata.get("format", {}).get("duration")]
    candidates.extend(stream.get("duration") for stream in metadata["streams"])
    for value in candidates:
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    return None


def contact_sheet(input_file, output_file, interval=2.0, times=None,
                  columns=4, width=320, max_frames=24, ffmpeg=None, ffprobe=None):
    """Seek to selected times, then tile thumbnails; never overwrite an output."""
    source, output = _input_path(input_file), _output_path(output_file)
    if output.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise VideoToolsError("Contact-sheet output must end in .png, .jpg or .jpeg.")
    if not 1 <= columns <= 20 or not 32 <= width <= 4096 or not 1 <= max_frames <= 200:
        raise VideoToolsError("Use 1–20 columns, width 32–4096, and 1–200 maximum frames.")
    executable = _tool("ffmpeg", ffmpeg)
    metadata = probe(source, ffprobe)
    if not any(s.get("codec_type") == "video" for s in metadata["streams"]):
        raise VideoToolsError("The input has no video stream.")
    duration = _duration(metadata)
    if times is not None:
        samples = sorted(set(times))
        if not samples or len(samples) > max_frames:
            raise VideoToolsError("Provide 1–{} distinct sample times.".format(max_frames))
        if any(not math.isfinite(t) or t < 0 for t in samples):
            raise VideoToolsError("Sample times must be finite, nonnegative seconds.")
        if duration is not None and samples[-1] >= duration:
            raise VideoToolsError("Sample times must be earlier than the video duration.")
    else:
        if not math.isfinite(interval) or interval <= 0:
            raise VideoToolsError("The sample interval must be finite and greater than zero.")
        if duration is None:
            raise VideoToolsError("Duration is unknown; provide explicit --times in seconds.")
        samples = []
        for index in range(max_frames):
            timestamp = index * interval
            if timestamp >= duration:
                break
            samples.append(timestamp)

    with tempfile.TemporaryDirectory(prefix=".cineflow-", dir=output.parent) as temporary:
        work = Path(temporary)
        for index, timestamp in enumerate(samples):
            frame = work / "frame-{:04d}.png".format(index)
            _run([
                executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                "-ss", "{:.6f}".format(timestamp), "-i", str(source),
                "-map", "0:v:0", "-an", "-frames:v", "1",
                "-vf", "scale={}:-2".format(width),
                "-update", "1", str(frame),
            ])
            if not frame.is_file() or frame.stat().st_size == 0:
                raise VideoToolsError("No frame could be decoded at {:.6f} seconds.".format(timestamp))
        actual_columns = min(columns, len(samples))
        rows = math.ceil(len(samples) / actual_columns)
        staged = work / output.name
        _run([
            executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-framerate", "1", "-i", str(work / "frame-%04d.png"),
            "-vf", "tile=layout={}x{}:nb_frames={}:padding=8:margin=8:color=black".format(
                actual_columns, rows, len(samples)
            ),
            "-frames:v", "1", "-update", "1", str(staged),
        ])
        _publish(staged, output)
    return {
        "output": str(output), "source": str(source), "columns": actual_columns,
        "rows": rows, "sample_times_seconds": samples,
        "order": "left to right, then top to bottom; times identify requested seek positions",
    }


def _stream_signature(metadata):
    fields = (
        "codec_type", "codec_name", "profile", "width", "height", "pix_fmt",
        "sample_aspect_ratio", "r_frame_rate", "time_base", "sample_fmt",
        "sample_rate", "channels", "channel_layout", "extradata_size", "extradata",
        "level", "field_order", "color_range", "color_space", "color_transfer", "color_primaries",
    )
    streams = metadata["streams"]
    if not streams or not any(s.get("codec_type") == "video" for s in streams):
        raise VideoToolsError("Concat inputs must contain video streams.")
    if any(s.get("codec_type") not in {"video", "audio"} for s in streams):
        raise VideoToolsError("Simple concat supports video/audio streams only.")
    return [tuple((field, stream.get(field)) for field in fields) for stream in streams]


def _concat_line(path):
    value = path.as_posix()
    if "\n" in value or "\r" in value:
        raise VideoToolsError("Concat input filenames cannot contain line breaks.")
    # FFmpeg concat uses its own quoting rules, not shell quoting. Leave the
    # quoted string to escape a literal apostrophe, then reopen the string.
    return "file '" + value.replace("'", "'\\''") + "'\n"


def concat(input_files, output_file, ffmpeg=None, ffprobe=None):
    """Stream-copy matching clips with the concat demuxer; no color conversion."""
    if len(input_files) < 2:
        raise VideoToolsError("Concat needs at least two input files.")
    sources = [_input_path(value) for value in input_files]
    output = _output_path(output_file)
    executable = _tool("ffmpeg", ffmpeg)
    lines = [_concat_line(path) for path in sources]
    expected = _stream_signature(probe(sources[0], ffprobe))
    for source in sources[1:]:
        if _stream_signature(probe(source, ffprobe)) != expected:
            raise VideoToolsError(
                "Stream parameters differ: {}. Normalize codec, dimensions, frame rate, "
                "time base and audio layout before using simple concat.".format(source)
            )
    with tempfile.TemporaryDirectory(prefix=".cineflow-", dir=output.parent) as temporary:
        work = Path(temporary)
        listing = work / "inputs.ffconcat"
        listing.write_text("ffconcat version 1.0\n" + "".join(lines), encoding="utf-8")
        staged = work / output.name
        _run([
            executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-map", "0", "-c", "copy", str(staged),
        ])
        _publish(staged, output)
    return {"output": str(output), "inputs": [str(path) for path in sources], "mode": "stream copy"}


def _sample_times(value):
    try:
        times = [float(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use comma-separated seconds, e.g. 0,2.5,7.") from exc
    if not times or any(not math.isfinite(t) or t < 0 for t in times):
        raise argparse.ArgumentTypeError("Times must be finite, nonnegative seconds.")
    return times


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="CineFlow local video tools. Requires FFmpeg/FFprobe; never overwrites outputs."
    )
    parser.add_argument("--ffmpeg", help="FFmpeg executable (or set FFMPEG_BIN)")
    parser.add_argument("--ffprobe", help="FFprobe executable (or set FFPROBE_BIN)")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("probe", help="Print stream and format metadata as JSON")
    inspect.add_argument("input")
    sheet = commands.add_parser(
        "contact-sheet", help="Tile interval samples or explicit per-shot timestamps into an image"
    )
    sheet.add_argument("input")
    sheet.add_argument("-o", "--output", required=True, help="New .png/.jpg path; parent directory must exist")
    selection = sheet.add_mutually_exclusive_group()
    selection.add_argument("--interval", type=float, default=2.0, help="Sample every N seconds (default: 2)")
    selection.add_argument("--times", type=_sample_times, help="Per-shot seconds, e.g. 0,4.5,9; ordered chronologically")
    sheet.add_argument("--columns", type=int, default=4)
    sheet.add_argument("--width", type=int, default=320, help="Thumbnail width in pixels (default: 320)")
    sheet.add_argument("--max-frames", type=int, default=24, help="Maximum samples, 1–200 (default: 24)")
    join = commands.add_parser("concat", help="Stream-copy compatible clips; no re-encoding or color conversion")
    join.add_argument("inputs", nargs="+")
    join.add_argument("-o", "--output", required=True, help="New video path; parent directory must exist")
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            result = probe(args.input, args.ffprobe)
        elif args.command == "contact-sheet":
            result = contact_sheet(
                args.input, args.output, args.interval, args.times, args.columns,
                args.width, args.max_frames, args.ffmpeg, args.ffprobe,
            )
        else:
            result = concat(args.inputs, args.output, args.ffmpeg, args.ffprobe)
    except (VideoToolsError, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
