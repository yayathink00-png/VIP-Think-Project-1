#!/usr/bin/env python3
"""Create a smarter review cut from accepted AI video segments.

The first pass is intentionally conservative: it analyzes the final segment for
obvious generated end-card failures, trims away detected bad tail content, then
renders a normalized 9:16 review cut with gentle crossfades and a logo overlay.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, text=True, capture_output=True)


def require_tool(name: str) -> None:
    if not shutil.which(name):
        fail(f"`{name}` is required but was not found in PATH.")


def ffprobe(path: Path) -> dict[str, Any]:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        fail(f"ffprobe failed for {path}:\n{result.stderr.strip()}")
    return json.loads(result.stdout)


def duration(path: Path) -> float:
    data = ffprobe(path)
    return float(data.get("format", {}).get("duration") or 0)


def extract_analysis_frames(video: Path, frames_dir: Path, fps: float) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("frame_*.jpg"):
        old.unlink()
    result = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vf",
            f"fps={fps},scale=180:-1",
            str(frames_dir / "frame_%04d.jpg"),
        ]
    )
    if result.returncode != 0:
        fail(f"analysis frame extraction failed:\n{result.stderr.strip()}")
    return sorted(frames_dir.glob("frame_*.jpg"))


def frame_metrics(path: Path) -> dict[str, float]:
    image = Image.open(path).convert("RGB")
    pixels = list(image.getdata())
    total = len(pixels) or 1
    green = 0
    white = 0
    dark = 0
    for r, g, b in pixels:
        if g > 115 and g > r * 1.25 and g > b * 1.15:
            green += 1
        if r > 205 and g > 205 and b > 205:
            white += 1
        if r < 35 and g < 35 and b < 35:
            dark += 1
    return {
        "green_ratio": green / total,
        "white_ratio": white / total,
        "dark_ratio": dark / total,
    }


def analyze_final_segment(video: Path, analysis_dir: Path, fps: float) -> dict[str, Any]:
    frames = extract_analysis_frames(video, analysis_dir / "frames", fps)
    metrics = []
    bad_start: float | None = None
    for index, frame in enumerate(frames):
        t = index / fps
        item = {"frame": frame.name, "time": round(t, 3), **frame_metrics(frame)}
        item["bad_end_card"] = item["green_ratio"] >= 0.12 or item["dark_ratio"] >= 0.75
        metrics.append(item)
        if item["bad_end_card"] and bad_start is None:
            bad_start = t

    full_duration = duration(video)
    if bad_start is None:
        safe_end = full_duration
        decision = "use_full_segment"
    else:
        safe_end = max(2.0, bad_start - 0.25)
        decision = "trim_before_detected_end_card"

    report = {
        "video": str(video),
        "duration": round(full_duration, 3),
        "analysis_fps": fps,
        "decision": decision,
        "safe_start": 0.0,
        "safe_end": round(min(safe_end, full_duration), 3),
        "metrics": metrics,
    }
    (analysis_dir / "segment05_analysis.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def make_contact_sheet(video: Path, output: Path) -> None:
    result = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vf",
            "fps=1/5,scale=180:-1,tile=5x4:padding=8:margin=8:color=white",
            "-frames:v",
            "1",
            str(output),
        ]
    )
    if result.returncode != 0:
        fail(f"contact sheet generation failed:\n{result.stderr.strip()}")


def render_smart_cut(args: argparse.Namespace) -> None:
    require_tool("ffmpeg")
    require_tool("ffprobe")

    run_dir = Path(args.run_dir).expanduser()
    edit_dir = run_dir / "edit"
    output_dir = edit_dir / args.output_subdir
    analysis_dir = edit_dir / "analysis" / f"{args.name}_analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    analysis_dir.mkdir(parents=True, exist_ok=True)

    videos = [Path(item).expanduser() for item in args.videos.split(",")]
    if len(videos) < 2:
        fail("--videos must contain at least two comma-separated files.")
    for video in videos:
        if not video.exists():
            fail(f"Missing input video: {video}")

    final_analysis = analyze_final_segment(videos[-1], analysis_dir, args.analysis_fps)
    trim_ends = [duration(video) for video in videos]
    trim_ends[-1] = float(final_analysis["safe_end"])

    transition = min(args.transition, 0.5)
    filter_parts: list[str] = []
    for index, end in enumerate(trim_ends):
        filter_parts.append(
            f"[{index}:v]trim=0:{end:.3f},setpts=PTS-STARTPTS,"
            "scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p"
            f"[v{index}]"
        )
        filter_parts.append(
            f"[{index}:a]atrim=0:{end:.3f},asetpts=PTS-STARTPTS,"
            "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
            f"[a{index}]"
        )

    current_v = "v0"
    current_duration = trim_ends[0]
    for index in range(1, len(videos)):
        out_v = f"vx{index}"
        offset = max(current_duration - transition, 0)
        filter_parts.append(
            f"[{current_v}][v{index}]xfade=transition=fade:duration={transition:.3f}:"
            f"offset={offset:.3f}[{out_v}]"
        )
        current_v = out_v
        current_duration += trim_ends[index] - transition

    current_a = "a0"
    for index in range(1, len(videos)):
        out_a = f"ax{index}"
        filter_parts.append(
            f"[{current_a}][a{index}]acrossfade=d={transition:.3f}:c1=tri:c2=tri[{out_a}]"
        )
        current_a = out_a

    logo_input = len(videos)
    final_v = current_v
    if args.logo:
        logo_path = Path(args.logo).expanduser()
        if not logo_path.exists():
            fail(f"Missing logo file: {logo_path}")
        filter_parts.append(f"[{logo_input}:v]scale={args.logo_width}:-1[logo]")
        filter_parts.append(
            f"[{current_v}][logo]overlay={args.logo_x}:{args.logo_y}:format=auto[vout]"
        )
        final_v = "vout"

    output = output_dir / f"{args.name}.mp4"
    command = ["ffmpeg", "-hide_banner", "-y"]
    for video in videos:
        command.extend(["-i", str(video)])
    if args.logo:
        command.extend(["-i", str(Path(args.logo).expanduser())])
    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            f"[{final_v}]",
            "-map",
            f"[{current_a}]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(args.crf),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    result = run(command)
    if result.returncode != 0:
        fail(f"smart render failed:\n{result.stderr.strip()}")

    contact_sheet = output_dir / f"{args.name}_contact_sheet.jpg"
    make_contact_sheet(output, contact_sheet)

    metadata = ffprobe(output)
    report = {
        "status": "completed",
        "output": str(output),
        "contact_sheet": str(contact_sheet),
        "duration": float(metadata.get("format", {}).get("duration") or 0),
        "transition_seconds": transition,
        "inputs": [
            {"file": str(video), "trim_start": 0.0, "trim_end": round(end, 3)}
            for video, end in zip(videos, trim_ends)
        ],
        "final_segment_analysis": final_analysis,
    }
    report_path = output_dir / f"{args.name}_smart_edit_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Created smart cut: {output}")
    print(f"Created contact sheet: {contact_sheet}")
    print(f"Created smart edit report: {report_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a smarter AI video review cut.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--videos", required=True, help="Comma-separated input video files in timeline order.")
    parser.add_argument("--logo", default="")
    parser.add_argument("--logo-x", type=int, default=56)
    parser.add_argument("--logo-y", type=int, default=56)
    parser.add_argument("--logo-width", type=int, default=170)
    parser.add_argument("--transition", type=float, default=0.35)
    parser.add_argument("--analysis-fps", type=float, default=2.0)
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--name", default="smart_review_cut")
    parser.add_argument("--output-subdir", default="current")
    render_smart_cut(parser.parse_args())


if __name__ == "__main__":
    main()
