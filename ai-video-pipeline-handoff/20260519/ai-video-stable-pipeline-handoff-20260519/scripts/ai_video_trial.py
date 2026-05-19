#!/usr/bin/env python3
"""Codex-controlled AI video automation trial runner.

This CLI creates a gated working package for reference-video remix workflows:
source breakdown, creative requirement approval, prompt approval, generated
segment review, and edit decision output.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, parse, request


ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "data" / "runs"


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    safe = []
    for ch in value.lower():
        if ch.isalnum():
            safe.append(ch)
        elif ch in {"-", "_", " ", "."}:
            safe.append("-")
    slug = "".join(safe).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "video"


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, text=True, capture_output=True)


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path and name == "whisper":
        local_whisper = ROOT / ".venv" / "bin" / "whisper"
        if local_whisper.exists():
            path = str(local_whisper)
    if not path:
        if name == "whisper":
            fail(
                "`whisper` is not installed or not in PATH. Install openai-whisper "
                "in the project venv, then rerun this command."
            )
        fail(f"`{name}` is not installed or not in PATH. Install ffmpeg first, then rerun this command.")
    return path


def has_tool(name: str) -> str | None:
    return shutil.which(name)


def json_dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_run_file(run_dir: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = run_dir / path
    return path


def clipped_text(value: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(value) <= limit:
        return value, False
    return value[:limit].rstrip() + "\n...", True


def parse_env_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def read_metadata(video: Path) -> dict[str, Any]:
    require_tool("ffprobe")
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video),
        ]
    )
    if result.returncode != 0:
        fail(f"ffprobe failed:\n{result.stderr.strip()}")

    data = json.loads(result.stdout)
    video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    duration = float(data.get("format", {}).get("duration") or video_stream.get("duration") or 0)
    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    aspect_ratio = "unknown"
    if width and height:
        ratio = width / height
        if abs(ratio - 1) < 0.04:
            aspect_ratio = "1:1-ish"
        elif abs(ratio - 4 / 5) < 0.04:
            aspect_ratio = "4:5-ish"
        elif abs(ratio - 9 / 16) < 0.04:
            aspect_ratio = "9:16-ish"
        elif abs(ratio - 16 / 9) < 0.04:
            aspect_ratio = "16:9-ish"
        elif height > width:
            aspect_ratio = "portrait-custom"
        else:
            aspect_ratio = "landscape-custom"

    return {
        "source_video": str(video.resolve()),
        "duration_seconds": round(duration, 3),
        "width": width,
        "height": height,
        "aspect_ratio_guess": aspect_ratio,
        "frame_rate": video_stream.get("r_frame_rate", "unknown"),
        "video_codec": video_stream.get("codec_name", "unknown"),
        "audio_stream_count": len(audio_streams),
        "created_at": now_iso(),
    }


def extract_first_https_url(value: str) -> str:
    start = value.find("https://")
    if start < 0:
        fail(f"No https URL found in command output: {value[:300]}")
    url = value[start:].strip()
    if url.startswith('"') and url.endswith('"'):
        url = json.loads(url)
    return url


def segment_file_name(segment: int, take: str) -> str:
    take = take.strip() or "A"
    if take.lower().startswith("take"):
        suffix = take
    else:
        suffix = f"take{take.upper()}"
    return f"Segment{segment:02d}_{suffix}.mp4"


def update_segment_gate(run_dir: Path, segment: int, status: str, decision: str, **extra: Any) -> None:
    gate_path = run_dir / "gate_status.json"
    if not gate_path.exists():
        return
    gate = json_load(gate_path)
    reviews = gate.setdefault("segment_reviews", [])
    item = next((entry for entry in reviews if entry.get("segment") == segment), None)
    if item is None:
        item = {"segment": segment}
        reviews.append(item)
    item["status"] = status
    item["decision"] = decision
    item.update(extra)
    json_dump(gate_path, gate)


def find_segment_item(gate: dict[str, Any], segment: int) -> dict[str, Any] | None:
    return next((entry for entry in gate.get("segment_reviews", []) if entry.get("segment") == segment), None)


def parse_prompt_approval(path: Path) -> dict[int, dict[str, str]]:
    if not path.exists():
        return {}
    approvals: dict[int, dict[str, str]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "---" in line or "段落" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        try:
            segment = int(cells[0])
        except ValueError:
            continue
        approvals[segment] = {
            "time": cells[1],
            "status": cells[2],
            "reason": cells[3] if len(cells) > 3 else "",
            "approver": cells[4] if len(cells) > 4 else "",
            "approved_at": cells[5] if len(cells) > 5 else "",
        }
    return approvals


def prompt_pack_has_segment(path: Path, segment: int) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return f"## Segment {segment:02d}" in text


def extract_segment_prompt(prompt_pack_path: Path, segment: int) -> str:
    if not prompt_pack_path.exists():
        fail(f"Missing jimeng_prompt_pack.md: {prompt_pack_path}")
    text = prompt_pack_path.read_text(encoding="utf-8")
    header = f"## Segment {segment:02d}"
    start = text.find(header)
    if start < 0:
        fail(f"jimeng_prompt_pack.md does not contain Segment {segment:02d}")
    next_start = text.find("\n## Segment ", start + len(header))
    section = text[start:] if next_start < 0 else text[start:next_start]
    fence = "```text"
    fence_start = section.find(fence)
    if fence_start < 0:
        fail(f"Segment {segment:02d} does not contain a ```text prompt block")
    prompt_start = fence_start + len(fence)
    fence_end = section.find("```", prompt_start)
    if fence_end < 0:
        fail(f"Segment {segment:02d} prompt block is not closed")
    return section[prompt_start:fence_end].strip()


def update_prompt_approval_file(path: Path, segments: list[int], status: str, reason: str, approver: str) -> list[int]:
    if not path.exists():
        fail(f"Missing prompt approval file: {path}")
    updated: list[int] = []
    output_lines: list[str] = []
    approved_at = now_iso()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "---" in line or "段落" in line:
            output_lines.append(raw_line)
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 6:
            output_lines.append(raw_line)
            continue
        try:
            segment = int(cells[0])
        except ValueError:
            output_lines.append(raw_line)
            continue
        if segment not in segments:
            output_lines.append(raw_line)
            continue
        cells[2] = status
        cells[3] = reason
        cells[4] = approver
        cells[5] = approved_at
        output_lines.append("| " + " | ".join(cells) + " |")
        updated.append(segment)
    path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    return updated


def download_url(url: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with request.urlopen(req, timeout=300) as resp, output.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
    except error.URLError as exc:
        fail(f"Download failed: {exc}")
    if not output.exists() or output.stat().st_size == 0:
        fail(f"Download produced an empty file: {output}")


def extract_frames(video: Path, run_dir: Path, interval: int) -> list[str]:
    require_tool("ffmpeg")
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    result = run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vf",
            f"fps=1/{interval}",
            str(frames_dir / "frame_%04d.jpg"),
        ]
    )
    if result.returncode != 0:
        fail(f"ffmpeg frame extraction failed:\n{result.stderr.strip()}")

    return [str(p.relative_to(run_dir)) for p in sorted(frames_dir.glob("frame_*.jpg"))]


def extract_audio(video: Path, run_dir: Path) -> str | None:
    require_tool("ffmpeg")
    audio_dir = run_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    target = audio_dir / "source_audio.wav"
    result = run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(target),
        ]
    )
    if result.returncode != 0:
        return None
    return str(target.relative_to(run_dir))


def transcribe_audio(run_dir: Path, model: str, language: str, output_format: str) -> Path:
    metadata_path = run_dir / "metadata.json"
    if not metadata_path.exists():
        fail(f"Missing metadata.json in {run_dir}")
    metadata = json_load(metadata_path)
    audio_ref = metadata.get("extracted_audio")
    if not audio_ref:
        fail("No extracted_audio recorded in metadata.json. Run prepare again with an audio track.")
    audio_path = run_dir / audio_ref
    if not audio_path.exists():
        fail(f"Extracted audio not found: {audio_path}")

    whisper = require_tool("whisper")
    transcript_dir = run_dir / "transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    result = run_command(
        [
            whisper,
            str(audio_path),
            "--language",
            language,
            "--model",
            model,
            "--output_dir",
            str(transcript_dir),
            "--output_format",
            output_format,
        ]
    )
    if result.returncode != 0:
        fail(f"whisper transcription failed:\n{result.stderr.strip()}")

    json_dump(
        transcript_dir / "manifest.json",
        {
            "created_at": now_iso(),
            "tool": whisper,
            "audio": str(audio_path.relative_to(run_dir)),
            "language": language,
            "model": model,
            "output_format": output_format,
            "stdout_tail": result.stdout[-4000:],
        },
    )
    return transcript_dir


def segment_ranges(duration: float, segment_seconds: int) -> list[dict[str, Any]]:
    if duration <= 0:
        return [{"index": 1, "start": 0, "end": segment_seconds, "label": f"00:00-00:{segment_seconds:02d}"}]
    count = max(1, math.ceil(duration / segment_seconds))
    rows = []
    for idx in range(count):
        start = idx * segment_seconds
        end = min(duration, (idx + 1) * segment_seconds)
        rows.append(
            {
                "index": idx + 1,
                "start": round(start, 3),
                "end": round(end, 3),
                "label": f"{format_time(start)}-{format_time(end)}",
            }
        )
    return rows


def format_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def write_source_breakdown(run_dir: Path, metadata: dict[str, Any], segments: list[dict[str, Any]], frames: list[str]) -> None:
    lines = [
        "# Source Video Breakdown",
        "",
        f"- Source video: `{metadata['source_video']}`",
        f"- Duration: {metadata['duration_seconds']}s",
        f"- Size: {metadata['width']}x{metadata['height']} ({metadata['aspect_ratio_guess']})",
        f"- Audio streams: {metadata['audio_stream_count']}",
        "",
        "## Time-coded Breakdown",
        "",
        "| 镜头 | 时间码 | 抽帧参考 | 画面/动作 | 台词/字幕 | 运镜/构图 | BGM/音效 | 叙事意图 | 复用方式 | 改写边界 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for seg in segments:
        frame_idx = min(len(frames), max(1, seg["index"])) - 1 if frames else None
        frame_ref = frames[frame_idx] if frame_idx is not None else "待抽帧"
        lines.append(
            f"| {seg['index']} | {seg['label']} | `{frame_ref}` | 待人工/AI补充 | 待通义听悟补充 | 待补充 | 待补充 | 待补充 | 保留节奏/镜头功能 | 换人物/品牌/原句/独特画面 |"
        )
    lines.extend(
        [
            "",
            "## Transcript Intake",
            "",
            "- Paste Tingwu or other transcript here, then map lines back into the table above.",
            "- Mark whether each line is on-screen speech, voice-over, subtitle-only, or background audio.",
        ]
    )
    (run_dir / "source_video_breakdown.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def default_requirements(segment_seconds: int) -> dict[str, Any]:
    return {
        "status": "pending",
        "approval_required": True,
        "output_language": "",
        "dialogue_strategy": "",
        "dialogue_must_include": [],
        "dialogue_must_avoid": [],
        "visual_style": "",
        "visual_reference_policy": "",
        "platform": "",
        "aspect_ratio": "",
        "total_duration_seconds": "",
        "segment_duration_seconds": segment_seconds,
        "brand_requirements": {
            "logo_asset": "",
            "logo_position": "",
            "tone": "",
            "banned_words": [],
            "forbidden_visuals": [],
        },
        "compliance_boundary": [
            "不复刻原视频人物",
            "不复刻原视频品牌",
            "不保留原视频水印",
            "不照搬原句",
            "不复刻独特画面构图",
        ],
        "confirmed_by": "",
        "confirmed_at": "",
    }


def write_creative_requirements(run_dir: Path, segment_seconds: int) -> None:
    requirements = default_requirements(segment_seconds)
    json_dump(run_dir / "creative_requirements.json", requirements)
    md = [
        "# Creative Requirements Approval",
        "",
        "Status: `pending`",
        "",
        "Before generating final Jimeng/Seedance prompts, edit `creative_requirements.json` and set `status` to `confirmed`.",
        "",
        "## 必填确认项",
        "",
        "- 输出语言：普通话 / 粤语 / 英语 / 双语",
        "- 台词策略：保留原意 / 重写 / 翻译 / 本地化 / 完全换卖点",
        "- 画风：写实 / 纪录片 / 广告感 / 生活流 / 电影感 / 手持感",
        "- 平台规格：小红书 / 抖音 / TikTok / YouTube Shorts / FB/IG",
        "- 视频比例与时长：9:16 / 1:1 / 16:9；10 / 15 / 30 / 60 秒",
        "- 品牌要求：Logo 位置、品牌调性、禁用词、不能出现的画面",
        "- 合规边界：不复刻原人物、品牌、水印、原句和独特画面",
    ]
    (run_dir / "creative_requirements.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def write_adaptation_boundary(run_dir: Path, segments: list[dict[str, Any]]) -> None:
    lines = [
        "# Adaptation Boundary",
        "",
        "| 段落 | 时间 | 可复用机制 | 必须改写表达 | 风险 |",
        "|---|---|---|---|---|",
    ]
    for seg in segments:
        lines.append(
            f"| {seg['index']} | {seg['label']} | 节奏、镜头功能、叙事意图 | 人物、场景、品牌、原句、独特构图 | 待确认 |"
        )
    lines.extend(
        [
            "",
            "Rule: prompts may borrow structure and pacing, but must not request a direct clone of the source video.",
        ]
    )
    (run_dir / "adaptation_boundary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_blocked_prompt_pack(run_dir: Path) -> None:
    lines = [
        "# Jimeng Prompt Pack",
        "",
        "Status: `blocked`",
        "",
        "Final prompts are blocked until `creative_requirements.json` has `status: confirmed`.",
        "",
        "Run:",
        "",
        "```bash",
        "python3 scripts/ai_video_trial.py generate-prompts --run-dir <this-run-dir>",
        "```",
    ]
    (run_dir / "jimeng_prompt_pack.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_prompt_approval(run_dir: Path, segments: list[dict[str, Any]], status: str = "待确认") -> None:
    lines = [
        "# Prompt Approval",
        "",
        "| 段落 | 时间 | 状态 | 修改原因 | 确认人 | 确认时间 |",
        "|---|---|---|---|---|---|",
    ]
    for seg in segments:
        lines.append(f"| {seg['index']} | {seg['label']} | {status} |  |  |  |")
    lines.extend(
        [
            "",
            "Allowed status: `待确认`, `已确认`, `需修改`, `废弃`.",
            "Only prompts marked `已确认` should be copied into Jimeng/Seedance.",
        ]
    )
    (run_dir / "prompt_approval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_empty_segment_review(run_dir: Path) -> None:
    lines = [
        "# Segment Review",
        "",
        "No generated AI segment has been reviewed yet.",
        "",
        "Run after a Jimeng/Seedance segment is returned:",
        "",
        "```bash",
        "python3 scripts/ai_video_trial.py review-segment --run-dir <this-run-dir> --segment 1 --video <generated-segment.mp4>",
        "```",
        "",
        "Decision values: `可进入下一段`, `建议重跑`, `必须重跑`.",
    ]
    (run_dir / "segment_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_edit_decision_list(run_dir: Path, segments: list[dict[str, Any]]) -> None:
    lines = [
        "# Edit Decision List",
        "",
        "| 顺序 | 素材 | 建议入点/出点 | 节奏处理 | 音频/字幕 | Logo/品牌 | 状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for seg in segments:
        lines.append(
            f"| {seg['index']} | segment_{seg['index']:02d}_takeA.mp4 | {seg['label']} | 待成片确认；生成素材禁止内嵌字幕 | 后期字幕待确认 | 待品牌确认 | 待生成 |"
        )
    (run_dir / "edit_decision_list.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_gate_status(run_dir: Path, segments: list[dict[str, Any]]) -> None:
    json_dump(
        run_dir / "gate_status.json",
        {
            "created_at": now_iso(),
            "creative_requirements": "pending",
            "prompt_approval": "pending",
            "segment_reviews": [
                {"segment": seg["index"], "status": "not_returned", "decision": ""} for seg in segments
            ],
            "next_action": "Fill and confirm creative_requirements.json, then run generate-prompts.",
        },
    )


def apply_name_gate_to_gate(
    gate: dict[str, Any],
    run_dir: Path,
    final_video_name: str,
    jimeng_dialogue: str,
    workspace_url: str,
    binder: str,
) -> None:
    dialogue = gate.setdefault("jimeng_dialogue", {})
    dialogue["required_name"] = jimeng_dialogue
    dialogue["accepted_display_name"] = jimeng_dialogue
    dialogue["original_full_name"] = final_video_name
    dialogue["workspace_url"] = workspace_url
    dialogue["scope"] = "current_video_project_only"
    dialogue["reuse_policy"] = "Do not reuse this dialogue for the next video project."
    dialogue["segment_policy"] = "All segments, reruns, and takes of this video must stay in this bound dialogue."
    dialogue["bound_at"] = now_iso()
    dialogue["bound_by"] = binder
    gate["jimeng_dialogue"] = dialogue
    gate["dialogue_policy"] = (
        "One video project gets one bound Jimeng dialogue. Segment 01 creates/binds it; "
        "all later segments/reruns/takes for the same video must stay in it. "
        "A new video project must create and bind a new dialogue."
    )
    gate["name_gate"] = {
        "status": "active",
        "rule": "Every generation must confirm current video project, bound Jimeng dialogue, and final video name before prompt fill/generate.",
        "current_video_project": run_dir.name,
        "bound_jimeng_dialogue": jimeng_dialogue,
        "final_video_name": final_video_name,
        "workspace_url": workspace_url,
        "dialogue_scope": "current_video_project_only",
        "next_video_rule": "Create and bind a new Jimeng dialogue for the next video; do not reuse this dialogue.",
        "approval_requirement": "Generation approval package must include Name Confirmation section and the user must approve the current names before generation.",
        "updated_at": now_iso(),
    }


def create_human_entry_folders(run_dir: Path) -> None:
    for name in ["00_当前成片", "01_审核材料", "02_原始片段", "03_项目文档"]:
        (run_dir / name).mkdir(parents=True, exist_ok=True)
    start_here = run_dir / "_START_HERE.md"
    if not start_here.exists():
        start_here.write_text(
            "\n".join(
                [
                    "# AI 视频项目入口",
                    "",
                    "- `00_当前成片/`：当前成片和接触表",
                    "- `01_审核材料/`：片段审核、生成前审核、通知记录",
                    "- `02_原始片段/`：即梦返回片段和下载记录",
                    "- `03_项目文档/`：项目索引和流程文档",
                    "",
                    "生成前必须先通过 Name Gate 和 continue-safe。",
                ]
            )
            + "\n",
            encoding="utf-8",
        )


def prepare(args: argparse.Namespace) -> None:
    video = Path(args.video).expanduser()
    if not video.exists():
        fail(f"Video not found: {video}")
    metadata = read_metadata(video)
    run_id = args.run_id or f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(video.stem)}"
    run_dir = RUNS_DIR / run_id
    if run_dir.exists() and not args.force:
        fail(f"Run directory already exists: {run_dir}. Use --force to overwrite generated files.")
    if run_dir.exists() and args.force:
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    frames = extract_frames(video, run_dir, args.frame_interval)
    audio = extract_audio(video, run_dir)
    metadata["extracted_frames"] = frames
    metadata["extracted_audio"] = audio
    metadata["run_id"] = run_id
    metadata["run_dir"] = str(run_dir.resolve())
    json_dump(run_dir / "metadata.json", metadata)

    segments = segment_ranges(float(metadata["duration_seconds"]), args.segment_seconds)
    json_dump(run_dir / "segments.json", segments)
    write_source_breakdown(run_dir, metadata, segments, frames)
    write_creative_requirements(run_dir, args.segment_seconds)
    write_adaptation_boundary(run_dir, segments)
    write_blocked_prompt_pack(run_dir)
    write_prompt_approval(run_dir, segments)
    write_empty_segment_review(run_dir)
    write_edit_decision_list(run_dir, segments)
    write_gate_status(run_dir, segments)
    create_human_entry_folders(run_dir)
    if args.final_video_name or args.jimeng_dialogue or args.workspace_url:
        missing_identity = [
            name
            for name, value in [
                ("--final-video-name", args.final_video_name),
                ("--jimeng-dialogue", args.jimeng_dialogue),
                ("--workspace-url", args.workspace_url),
            ]
            if not value
        ]
        if missing_identity:
            fail("Name Gate setup requires all identity args: " + ", ".join(missing_identity))
        gate_path = run_dir / "gate_status.json"
        gate = json_load(gate_path)
        apply_name_gate_to_gate(
            gate,
            run_dir,
            args.final_video_name,
            args.jimeng_dialogue,
            args.workspace_url,
            args.binder,
        )
        gate["next_action"] = "Fill and confirm creative_requirements.json, then run generate-prompts. Run continue-safe before any generation."
        json_dump(gate_path, gate)
    if args.transcribe:
        transcript_dir = transcribe_audio(
            run_dir,
            args.whisper_model,
            args.whisper_language,
            args.whisper_output_format,
        )
        print(f"Created Whisper transcript: {transcript_dir}")

    print(f"Created AI video trial package: {run_dir}")
    print("Next: fill creative_requirements.json, set status to confirmed, then run generate-prompts.")


def new_video_run(args: argparse.Namespace) -> None:
    prepare_args = argparse.Namespace(
        video=args.video,
        run_id=args.run_id,
        segment_seconds=args.segment_seconds,
        frame_interval=args.frame_interval,
        transcribe=args.transcribe,
        whisper_model=args.whisper_model,
        whisper_language=args.whisper_language,
        whisper_output_format=args.whisper_output_format,
        final_video_name=args.final_video_name,
        jimeng_dialogue=args.jimeng_dialogue,
        workspace_url=args.workspace_url,
        binder=args.binder,
        force=args.force,
    )
    prepare(prepare_args)
    run_id = args.run_id or f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(Path(args.video).expanduser().stem)}"
    run_dir = RUNS_DIR / run_id
    if run_dir.exists():
        print("")
        print("Name Gate is active for this new video run.")
        print("Before generation, run:")
        print(f"python3 scripts/ai_video_trial.py continue-safe --run-dir '{run_dir}' --stage preflight --segment 1 --require-browser")


def validate_requirements(requirements: dict[str, Any]) -> list[str]:
    missing = []
    if requirements.get("status") != "confirmed":
        missing.append("status must be confirmed")
    for key in [
        "output_language",
        "dialogue_strategy",
        "visual_style",
        "platform",
        "aspect_ratio",
        "total_duration_seconds",
    ]:
        if not str(requirements.get(key, "")).strip():
            missing.append(key)
    brand = requirements.get("brand_requirements", {})
    for key in ["logo_position", "tone"]:
        if not str(brand.get(key, "")).strip():
            missing.append(f"brand_requirements.{key}")
    return missing


def generate_prompts(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    requirements_path = run_dir / "creative_requirements.json"
    if not requirements_path.exists():
        fail(f"Missing creative_requirements.json in {run_dir}")
    requirements = json_load(requirements_path)
    missing = validate_requirements(requirements)
    if missing:
        fail("Creative requirements are not confirmed. Missing: " + ", ".join(missing))

    segments = json_load(run_dir / "segments.json")
    lines = [
        "# Jimeng Prompt Pack",
        "",
        "Status: `ready_for_prompt_approval`",
        "",
        "合规改写：只借鉴参考视频的节奏、镜头功能和叙事结构，不复刻原视频的人物、品牌、水印、原句、独特画面构图或误导性承诺。",
        "",
        "## Confirmed Creative Requirements",
        "",
        f"- 输出语言：{requirements['output_language']}",
        f"- 台词策略：{requirements['dialogue_strategy']}",
        f"- 台词必须包含：{', '.join(requirements.get('dialogue_must_include', [])) or '无'}",
        f"- 台词必须避免：{', '.join(requirements.get('dialogue_must_avoid', [])) or '无'}",
        f"- 画风：{requirements['visual_style']}",
        f"- 画面参考策略：{requirements.get('visual_reference_policy', '') or '未填写'}",
        f"- 平台：{requirements['platform']}",
        f"- 比例：{requirements['aspect_ratio']}",
        f"- 总时长：{requirements['total_duration_seconds']} 秒",
        f"- 单段时长：{requirements['segment_duration_seconds']} 秒",
        f"- Logo素材：{requirements['brand_requirements'].get('logo_asset', '') or '未指定'}",
        f"- Logo 位置：{requirements['brand_requirements']['logo_position']}",
        f"- 品牌调性：{requirements['brand_requirements']['tone']}",
        "",
    ]
    for seg in segments:
        lines.extend(
            [
                f"## Segment {seg['index']:02d} / {seg['label']}",
                "",
                "### Jimeng/Seedance Prompt",
                "",
                "```text",
                f"{requirements['aspect_ratio']}，{requirements['segment_duration_seconds']} 秒，{requirements['visual_style']}。",
                f"输出语言为 {requirements['output_language']}，台词策略为 {requirements['dialogue_strategy']}。",
                f"台词必须包含：{', '.join(requirements.get('dialogue_must_include', [])) or '无特别指定'}。",
                f"台词必须避免：{', '.join(requirements.get('dialogue_must_avoid', [])) or '无特别指定'}。",
                f"画面参考策略：{requirements.get('visual_reference_policy', '') or '参考原视频节奏，但重新生成画面'}。",
                "",
                "基于参考视频的节奏和镜头功能重新创作本段，不复刻原视频人物、品牌、水印、原句或独特画面。",
                f"本段时间范围：{seg['label']}。本段目标：承接原片同一段落的叙事功能，但替换为本项目自己的角色、场景、产品表达和品牌语气。",
                "",
                "00:00-00:03：建立本段情境和动作钩子，画面真实自然，避免硬广摆拍。",
                "00:03-00:08：展示核心动作或证明过程，镜头保持连续，人物、服装、道具和光线不要跳变。",
                "00:08-00:12：给出结果、情绪转折或信息落点；台词与已确认语言策略一致，生成画面不要内嵌字幕。",
                "00:12-00:15：留下自然结尾和下一段可继承的画面状态，保留品牌/Logo 空间。",
                "",
                f"品牌要求：{requirements['brand_requirements']['tone']}；Logo 使用 {requirements['brand_requirements'].get('logo_asset', '') or '指定品牌Logo'}，位置预留在 {requirements['brand_requirements']['logo_position']}。",
                f"禁用词：{', '.join(requirements['brand_requirements'].get('banned_words', [])) or '无'}。",
                f"禁用画面：{', '.join(requirements['brand_requirements'].get('forbidden_visuals', [])) or '无'}。",
                "负面约束：不要换人，不要换衣服，不要改变行进方向，不要出现原视频水印，不要出现未授权品牌，不要生成字幕/花字/贴片文字/台词文字/文字特效，不要手部畸形，不要夸大承诺。",
                "```",
                "",
                "### Prompt Approval Checklist",
                "",
                "- 语言正确",
                "- 台词变化尺度正确",
                "- 画风正确",
                "- 人物/场景/镜头符合项目要求",
                "- 口播/BGM/负面约束完整，且明确生成阶段无字幕",
                "- 可复制到 Jimeng/Seedance",
                "",
            ]
        )
    (run_dir / "jimeng_prompt_pack.md").write_text("\n".join(lines), encoding="utf-8")
    write_prompt_approval(run_dir, segments, status="待确认")
    gate = json_load(run_dir / "gate_status.json")
    gate["creative_requirements"] = "confirmed"
    gate["prompt_approval"] = "pending"
    gate["next_action"] = "Review jimeng_prompt_pack.md and mark prompts as 已确认 in prompt_approval.md before generating videos."
    json_dump(run_dir / "gate_status.json", gate)
    print(f"Generated prompt pack: {run_dir / 'jimeng_prompt_pack.md'}")
    print("Next: approve prompts in prompt_approval.md before using Jimeng/Seedance.")


def review_segment(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    video = Path(args.video).expanduser()
    if not video.exists():
        fail(f"Generated segment video not found: {video}")
    metadata = read_metadata(video)
    review_dir = run_dir / "reviews" / f"segment_{args.segment:02d}"
    review_dir.mkdir(parents=True, exist_ok=True)
    frames = extract_frames(video, review_dir, args.frame_interval)
    metadata["review_frames"] = frames
    metadata["segment"] = args.segment
    json_dump(review_dir / "metadata.json", metadata)

    decision = args.decision or "待人工判断"
    gate_status_value = "reviewed" if args.decision else "generated_pending_review"
    lines = [
        f"# Segment {args.segment:02d} Review",
        "",
        f"Decision: `{decision}`",
        "",
        f"- Returned video: `{video.resolve()}`",
        f"- Duration: {metadata['duration_seconds']}s",
        f"- Size: {metadata['width']}x{metadata['height']} ({metadata['aspect_ratio_guess']})",
        "",
        "## Review Frames",
        "",
    ]
    for frame in frames:
        lines.append(f"- `{frame}`")
    lines.extend(
        [
            "",
            "## Checklist",
            "",
            "| 检查项 | 结论 | 备注 |",
            "|---|---|---|",
            "| 人物一致 | 待确认 |  |",
            "| 服装一致 | 待确认 |  |",
            "| 场景连续 | 待确认 |  |",
            "| 台词/口型 | 待确认 |  |",
            "| 动作自然 | 待确认 |  |",
            "| 品牌/Logo空间 | 待确认 |  |",
            "| 无水印/无明显穿帮 | 待确认 |  |",
            "",
            "## Actual Ending State",
            "",
            "- 人物：待确认",
            "- 姿态：待确认",
            "- 道具：待确认",
            "- 场景：待确认",
            "- 镜头方向：待确认",
            "- 光线：待确认",
            "",
            "## Next Segment Carry-over",
            "",
            "- 下一段必须继承上一段真实结尾状态，不从原始设想硬接。",
        ]
    )
    (review_dir / "segment_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    top_review = run_dir / "segment_review.md"
    with top_review.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## Segment {args.segment:02d}\n\n")
        fh.write(f"- Decision: `{decision}`\n")
        fh.write(f"- Review file: `{review_dir.relative_to(run_dir) / 'segment_review.md'}`\n")
        fh.write(f"- Reviewed at: {now_iso()}\n")

    gate = json_load(run_dir / "gate_status.json")
    for item in gate.get("segment_reviews", []):
        if item.get("segment") == args.segment:
            item["status"] = gate_status_value
            item["decision"] = decision
            item["review_file"] = str((review_dir / "segment_review.md").relative_to(run_dir))
    gate["next_action"] = (
        "If decision is 可进入下一段, carry actual ending state into the next prompt. Otherwise rerun the segment."
        if args.decision
        else f"User must review Segment {args.segment:02d} before continuing."
    )
    json_dump(run_dir / "gate_status.json", gate)
    print(f"Created segment review: {review_dir / 'segment_review.md'}")


def record_review_decision(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    gate_path = run_dir / "gate_status.json"
    if not gate_path.exists():
        fail(f"Missing gate_status.json: {gate_path}")

    gate = json_load(gate_path)
    item = find_segment_item(gate, args.segment)
    if item is None:
        fail(f"Segment {args.segment:02d} is missing from gate_status.json")

    if args.decision == "可进入下一段":
        item["status"] = "reviewed"
        gate["next_action"] = f"Extract Segment {args.segment:02d} tail frame, then prepare Segment {args.segment + 1:02d} preflight for user review."
    else:
        item["status"] = "must_rerun"
        gate["next_action"] = f"Prepare Segment {args.segment:02d} rerun preflight for user review."
        for downstream in gate.get("segment_reviews", []):
            downstream_segment = downstream.get("segment")
            if isinstance(downstream_segment, int) and downstream_segment > args.segment:
                downstream["status"] = f"blocked_by_segment_{args.segment:02d}_rerun"
                downstream["decision"] = f"Segment {args.segment:02d} requires rerun before this segment can continue"

    item["decision"] = args.decision
    item["reviewed_by"] = args.reviewer
    item["reviewed_at"] = now_iso()
    if args.note:
        item["review_note"] = args.note
    json_dump(gate_path, gate)

    review_file = item.get("review_file")
    if review_file:
        review_path = run_dir / review_file
        if review_path.exists():
            with review_path.open("a", encoding="utf-8") as fh:
                fh.write("\n## User Review Decision\n\n")
                fh.write(f"- Decision: `{args.decision}`\n")
                fh.write(f"- Reviewer: `{args.reviewer}`\n")
                fh.write(f"- Reviewed at: {now_iso()}\n")
                if args.note:
                    fh.write(f"- Note: {args.note}\n")

    print(f"Recorded Segment {args.segment:02d} decision: {args.decision}")


def verify_jimeng_dialogue(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    gate_path = run_dir / "gate_status.json"
    if not gate_path.exists():
        fail(f"Missing gate_status.json: {gate_path}")
    gate = json_load(gate_path)
    dialogue = gate.get("jimeng_dialogue", {})
    required_name = args.name or dialogue.get("required_name")
    if not required_name:
        fail("Missing required Jimeng dialogue name. Set gate_status.json jimeng_dialogue.required_name first.")
    workspace_url = args.workspace_url or dialogue.get("workspace_url", "")

    if not has_tool("opencli"):
        fail("OpenCLI is missing. Cannot verify Jimeng dialogue.")
    required_js = json.dumps(required_name, ensure_ascii=False)
    script = (
        "(() => {"
        f"const required={required_js};"
        "const body=document.body.innerText || '';"
        "const url=location.href;"
        "const inputs=[...document.querySelectorAll('input')].map(i=>i.value||'').filter(Boolean);"
        "const exactCount=body.split(required).length-1;"
        "return {url, title:document.title, exactCount, bodyHas:body.includes(required), inputHas:inputs.includes(required), bodyTail:body.slice(-1200)};"
        "})()"
    )
    result = run_command(["opencli", "browser", args.session, "eval", script])
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        fail("Could not inspect current Jimeng page.")
    try:
        page = json.loads(result.stdout)
    except json.JSONDecodeError:
        fail(f"Could not parse OpenCLI page inspection output:\n{result.stdout}")

    url_ok = not workspace_url or page.get("url") == workspace_url
    # inputHas often means the name is only in the search box, not an actual dialogue.
    name_visible_as_dialogue = bool(page.get("bodyHas")) and not bool(page.get("inputHas") and page.get("exactCount", 0) <= 1)
    status = "verified" if url_ok and name_visible_as_dialogue else "blocked"

    verify_dir = run_dir / "preflight"
    verify_dir.mkdir(parents=True, exist_ok=True)
    report_path = verify_dir / "jimeng_dialogue_verify.md"
    lines = [
        "# Jimeng Dialogue Verification",
        "",
        f"Status: `{status}`",
        f"Created at: `{now_iso()}`",
        "",
        f"- Required name: `{required_name}`",
        f"- Expected workspace URL: `{workspace_url or 'not set'}`",
        f"- Current URL: `{page.get('url', '')}`",
        f"- URL match: `{url_ok}`",
        f"- Name visible on page: `{page.get('bodyHas')}`",
        f"- Name only in input/search box: `{page.get('inputHas')}`",
        f"- Exact text count: `{page.get('exactCount')}`",
        "",
        "## Decision",
        "",
    ]
    if status == "verified":
        lines.append("Dialogue name and workspace passed verification.")
    else:
        lines.append("Do not generate. Select, create, or rename the correct Jimeng dialogue first.")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dialogue["last_verify_status"] = status
    dialogue["last_verify_report"] = str(report_path.relative_to(run_dir))
    dialogue["last_verified_at"] = now_iso()
    dialogue["last_seen_url"] = page.get("url", "")
    gate["jimeng_dialogue"] = dialogue
    gate["next_action"] = (
        "Prepare Segment 03 rerun preflight for user review."
        if status == "verified"
        else "Select, create, or rename the Jimeng dialogue to the required name before generation."
    )
    json_dump(gate_path, gate)

    print(f"Jimeng dialogue verification: {status}")
    print(f"Report: {report_path}")
    if status != "verified" and args.strict:
        raise SystemExit(2)


def download_segment(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    if args.url:
        url = args.url
    else:
        if not has_tool("opencli"):
            fail("OpenCLI is missing. Pass --url or install OpenCLI first.")
        script = (
            "(() => { "
            "const videos=[...document.querySelectorAll('video')].map(v=>v.currentSrc||v.src).filter(Boolean); "
            "return videos[videos.length-1] || ''; "
            "})()"
        )
        result = run_command(["opencli", "browser", args.session, "eval", script])
        if result.returncode != 0:
            if result.stderr.strip():
                print(result.stderr.strip(), file=sys.stderr)
            fail("Could not read the current Jimeng video URL from the browser page.")
        url = extract_first_https_url(result.stdout.strip())

    output = Path(args.output).expanduser() if args.output else run_dir / "returned" / segment_file_name(args.segment, args.take)
    download_url(url, output)
    metadata = read_metadata(output)
    download_manifest = {
        "created_at": now_iso(),
        "segment": args.segment,
        "take": args.take,
        "url_source": "manual-url" if args.url else f"opencli:{args.session}",
        "output": str(output.resolve()),
        "metadata": metadata,
    }
    manifest_path = run_dir / "downloads" / f"segment_{args.segment:02d}_{args.take}.json"
    json_dump(manifest_path, download_manifest)
    gate_status_value = "downloaded"
    gate_path = run_dir / "gate_status.json"
    if gate_path.exists():
        gate = json_load(gate_path)
        existing = next((entry for entry in gate.get("segment_reviews", []) if entry.get("segment") == args.segment), {})
        if existing.get("status") == "reviewed":
            gate_status_value = "reviewed"
    update_segment_gate(
        run_dir,
        args.segment,
        gate_status_value,
        f"Downloaded {output.name}",
        downloaded_file=str(output.relative_to(run_dir)) if output.is_relative_to(run_dir) else str(output.resolve()),
        download_manifest=str(manifest_path.relative_to(run_dir)),
    )
    print(f"Downloaded segment video: {output}")
    print(f"Metadata: {metadata['width']}x{metadata['height']}, {metadata['duration_seconds']}s, {metadata['video_codec']}")


def find_segment_video(run_dir: Path, segment: int) -> Path:
    returned = run_dir / "returned"
    if not returned.exists():
        fail(f"Missing returned directory: {returned}")
    patterns = [
        f"Segment{segment:02d}_*.mp4",
        f"segment_{segment:02d}_*.mp4",
        f"segment{segment:02d}_*.mp4",
    ]
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(returned.glob(pattern))
    if not matches:
        fail(f"No returned video found for Segment {segment:02d} in {returned}")
    return sorted(matches, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def find_segment_video_optional(run_dir: Path, segment: int) -> Path | None:
    returned = run_dir / "returned"
    if not returned.exists():
        return None
    patterns = [
        f"Segment{segment:02d}_*.mp4",
        f"segment_{segment:02d}_*.mp4",
        f"segment{segment:02d}_*.mp4",
    ]
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(returned.glob(pattern))
    if not matches:
        return None
    return sorted(matches, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def trim_logo(input_path: Path, output_path: Path) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError:
        fail("Pillow is required for logo trimming. Install pillow or pass a pre-trimmed logo.")

    image = Image.open(input_path).convert("RGBA")
    pixels = image.load()
    width, height = image.size
    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        for x in range(width):
            r, g, b, alpha = pixels[x, y]
            if alpha > 10 and not (r > 245 and g > 245 and b > 245):
                xs.append(x)
                ys.append(y)
    if not xs:
        fail(f"No visible logo content found: {input_path}")
    box = (
        max(min(xs) - 12, 0),
        max(min(ys) - 12, 0),
        min(max(xs) + 13, width),
        min(max(ys) + 13, height),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cropped = image.crop(box)
    cropped.save(output_path)
    return cropped.size


def make_contact_sheet(frames_dir: Path, output_path: Path, prefix: str) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        fail("Pillow is required for contact sheet generation.")

    frames = sorted(frames_dir.glob(f"{prefix}_*.jpg"))[:6]
    if not frames:
        fail(f"No frames found in {frames_dir}")
    thumbs = []
    for idx, path in enumerate(frames, 1):
        image = Image.open(path).convert("RGB")
        image.thumbnail((180, 320))
        canvas = Image.new("RGB", (180, 340), "white")
        canvas.paste(image, ((180 - image.width) // 2, 0))
        ImageDraw.Draw(canvas).text((8, 322), f"Frame {idx}", fill=(0, 0, 0))
        thumbs.append(canvas)
    sheet = Image.new("RGB", (180 * len(thumbs), 340), "white")
    for idx, thumb in enumerate(thumbs):
        sheet.paste(thumb, (idx * 180, 0))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, quality=92)


def edit_trial(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    require_tool("ffmpeg")
    require_tool("ffprobe")
    edit_dir = run_dir / "edit"
    edit_dir.mkdir(parents=True, exist_ok=True)

    segment_ids = [int(item.strip()) for item in args.segments.split(",") if item.strip()]
    videos = [Path(path).expanduser() for path in split_csv(args.videos)] if args.videos else [
        find_segment_video(run_dir, segment) for segment in segment_ids
    ]
    if len(videos) != len(segment_ids):
        fail("--videos count must match --segments count")

    concat_list = edit_dir / f"{args.name}_concat_list.txt"
    concat_lines = [f"file '{video.resolve()}'" for video in videos]
    concat_list.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
    rough_cut = edit_dir / f"{args.name}_rough_no_logo.mp4"
    result = run_command(["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(rough_cut)])
    if result.returncode != 0:
        fail(f"ffmpeg concat failed:\n{result.stderr.strip()}")

    logo_path = Path(args.logo).expanduser()
    if not logo_path.exists():
        fail(f"Logo not found: {logo_path}")
    logo_trimmed = edit_dir / f"{args.name}_logo_trimmed.png"
    trim_logo(logo_path, logo_trimmed)
    review_cut = edit_dir / f"{args.name}_logo.mp4"
    filter_complex = (
        f"[1:v]scale={args.logo_width}:-1[logo];"
        f"[0:v][logo]overlay={args.logo_x}:{args.logo_y}:format=auto"
    )
    result = run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(rough_cut),
            "-i",
            str(logo_trimmed),
            "-filter_complex",
            filter_complex,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(args.crf),
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(review_cut),
        ]
    )
    if result.returncode != 0:
        fail(f"ffmpeg logo overlay failed:\n{result.stderr.strip()}")

    frames_dir = edit_dir / f"{args.name}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_prefix = args.name
    result = run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(review_cut),
            "-vf",
            f"fps=1/{args.frame_interval},scale=360:-1",
            str(frames_dir / f"{frame_prefix}_%03d.jpg"),
        ]
    )
    if result.returncode != 0:
        fail(f"ffmpeg contact frames failed:\n{result.stderr.strip()}")
    contact_sheet = edit_dir / f"{args.name}_contact_sheet.jpg"
    make_contact_sheet(frames_dir, contact_sheet, frame_prefix)

    output_metadata = read_metadata(review_cut)
    report_path = edit_dir / f"{args.name}_qc_report.md"
    lines = [
        "# Edit QC Report",
        "",
        f"Status: `剪辑流程试跑完成`",
        "",
        "## Inputs",
        "",
        "| Segment | File | Duration | Specs |",
        "|---|---|---:|---|",
    ]
    for segment, video in zip(segment_ids, videos):
        meta = read_metadata(video)
        lines.append(
            f"| Segment {segment:02d} | `{video.relative_to(run_dir) if video.is_relative_to(run_dir) else video}` | {meta['duration_seconds']}s | {meta['width']}x{meta['height']}, {meta['frame_rate']}, {meta['video_codec']} |"
        )
    lines.extend(
        [
            "",
            "## Outputs",
            "",
            f"- Rough cut: `{rough_cut.relative_to(run_dir)}`",
            f"- Review cut: `{review_cut.relative_to(run_dir)}`",
            f"- Contact sheet: `{contact_sheet.relative_to(run_dir)}`",
            f"- Logo: `{logo_trimmed.relative_to(run_dir)}`",
            "",
            "## Specs",
            "",
            f"- Duration: {output_metadata['duration_seconds']}s",
            f"- Size: {output_metadata['width']}x{output_metadata['height']}",
            f"- Frame rate: {output_metadata['frame_rate']}",
            f"- Logo position: x={args.logo_x}, y={args.logo_y}, width={args.logo_width}px",
            "",
            "## Notes",
            "",
            "- 本命令只做拼接、Logo、抽帧和 QC 报告，不额外烧录字幕。",
            "- 如果源视频已有内嵌字幕，正式版应选择遮盖、裁切或重跑。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    gate_path = run_dir / "gate_status.json"
    if gate_path.exists():
        gate = json_load(gate_path)
        gate["editing_trial"] = {
            "status": "completed",
            "rough_cut": str(rough_cut.relative_to(run_dir)),
            "review_cut": str(review_cut.relative_to(run_dir)),
            "contact_sheet": str(contact_sheet.relative_to(run_dir)),
            "qc_report": str(report_path.relative_to(run_dir)),
        }
        gate["next_action"] = f"Review {review_cut.relative_to(run_dir)} and {report_path.relative_to(run_dir)}."
        json_dump(gate_path, gate)

    print(f"Created review cut: {review_cut}")
    print(f"Created contact sheet: {contact_sheet}")
    print(f"Created QC report: {report_path}")


def extract_tail_frame(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    require_tool("ffmpeg")
    video = Path(args.video).expanduser() if args.video else find_segment_video(run_dir, args.segment)
    if not video.exists():
        fail(f"Segment video not found: {video}")
    metadata = read_metadata(video)
    duration = float(metadata.get("duration_seconds") or 0)
    seek_time = max(duration - args.offset_seconds, 0)
    review_dir = run_dir / "reviews" / f"segment_{args.segment:02d}"
    review_dir.mkdir(parents=True, exist_ok=True)
    tail_frame = review_dir / "tail_frame.jpg"
    result = run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{seek_time:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            str(tail_frame),
        ]
    )
    if result.returncode != 0:
        fail(f"ffmpeg tail-frame extraction failed:\n{result.stderr.strip()}")

    carryover = review_dir / "carryover_state.md"
    lines = [
        f"# Segment {args.segment:02d} Carryover State",
        "",
        f"Created at: `{now_iso()}`",
        f"Source video: `{video.relative_to(run_dir) if video.is_relative_to(run_dir) else video}`",
        f"Tail frame: `{tail_frame.relative_to(run_dir)}`",
        f"Captured at: `{seek_time:.2f}s` of `{duration:.2f}s`",
        "",
        "## Use For Next Segment",
        "",
        "- Upload or visually reference this tail frame before generating the next segment.",
        "- Keep the same person, clothing, lighting direction, scene continuity, and camera direction unless the prompt explicitly changes scene.",
        "- Do not let Jimeng generate subtitles, text stickers, watermarks, or Logo in the generated video.",
        "",
        "## Manual Notes",
        "",
        "- 人物：待确认",
        "- 姿态：待确认",
        "- 道具：待确认",
        "- 场景：待确认",
        "- 镜头方向：待确认",
        "- 光线：待确认",
    ]
    carryover.write_text("\n".join(lines) + "\n", encoding="utf-8")

    gate_path = run_dir / "gate_status.json"
    if gate_path.exists():
        gate = json_load(gate_path)
        carryovers = gate.setdefault("carryovers", {})
        carryovers[f"segment_{args.segment:02d}"] = {
            "tail_frame": str(tail_frame.relative_to(run_dir)),
            "carryover_state": str(carryover.relative_to(run_dir)),
            "created_at": now_iso(),
        }
        gate["next_action"] = f"Use {tail_frame.relative_to(run_dir)} as continuity reference before Segment {args.segment + 1:02d}."
        json_dump(gate_path, gate)

    print(f"Created tail frame: {tail_frame}")
    print(f"Created carryover note: {carryover}")


def final_export(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    gate_path = run_dir / "gate_status.json"
    if not gate_path.exists():
        fail(f"Missing gate_status.json: {gate_path}")
    gate = json_load(gate_path)
    if args.segments:
        segments = [int(item) for item in split_csv(args.segments)]
    else:
        segments = [int(item.get("segment")) for item in gate.get("segment_reviews", []) if item.get("segment")]
    if not segments:
        fail("No segments selected for final export")

    blockers = []
    for segment in segments:
        item = find_segment_item(gate, segment)
        if not item or item.get("status") != "reviewed":
            blockers.append(f"Segment {segment:02d} is not reviewed")
        if find_segment_video_optional(run_dir, segment) is None:
            blockers.append(f"Segment {segment:02d} returned video missing")
    if blockers and not args.allow_unreviewed:
        fail("Final export blocked:\n- " + "\n- ".join(blockers))

    edit_args = argparse.Namespace(
        run_dir=str(run_dir),
        segments=",".join(str(segment) for segment in segments),
        videos=args.videos,
        logo=args.logo,
        logo_x=args.logo_x,
        logo_y=args.logo_y,
        logo_width=args.logo_width,
        frame_interval=args.frame_interval,
        crf=args.crf,
        name=args.name,
    )
    edit_trial(edit_args)
    if gate_path.exists():
        gate = json_load(gate_path)
        gate["final_export"] = {
            "status": "completed",
            "segments": segments,
            "name": args.name,
            "created_at": now_iso(),
            "review_cut": f"edit/{args.name}_logo.mp4",
            "qc_report": f"edit/{args.name}_qc_report.md",
        }
        gate["next_action"] = f"Review final export edit/{args.name}_logo.mp4 and edit/{args.name}_qc_report.md."
        json_dump(gate_path, gate)


def gate_status(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    gate_path = run_dir / "gate_status.json"
    if not gate_path.exists():
        fail(f"Missing gate_status.json: {gate_path}")
    gate = json_load(gate_path)
    print(f"Run: {run_dir}")
    print(f"Next action: {gate.get('next_action', '')}")
    print("")
    print("| Segment | Status | Decision | File |")
    print("|---|---|---|---|")
    for item in gate.get("segment_reviews", []):
        file_ref = item.get("downloaded_file") or item.get("review_file") or ""
        print(f"| {item.get('segment')} | {item.get('status', '')} | {item.get('decision', '')} | {file_ref} |")
    if gate.get("editing_trial"):
        print("")
        print("Editing trial:")
        for key, value in gate["editing_trial"].items():
            print(f"- {key}: {value}")


def approve_prompt(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    segments = [int(item) for item in split_csv(args.segments)]
    if not segments:
        fail("--segments must contain at least one segment number")
    prompt_pack_path = run_dir / "jimeng_prompt_pack.md"
    for segment in segments:
        if not prompt_pack_has_segment(prompt_pack_path, segment):
            fail(f"jimeng_prompt_pack.md does not contain Segment {segment:02d}")

    approval_path = run_dir / "prompt_approval.md"
    updated = update_prompt_approval_file(
        approval_path,
        segments,
        args.status,
        args.reason,
        args.approver,
    )
    missing = sorted(set(segments) - set(updated))
    if missing:
        fail("Could not update prompt approval rows for: " + ", ".join(str(item) for item in missing))

    gate_path = run_dir / "gate_status.json"
    if gate_path.exists():
        gate = json_load(gate_path)
        approvals = gate.setdefault("prompt_approvals", {})
        for segment in updated:
            approvals[f"segment_{segment:02d}"] = {
                "status": args.status,
                "reason": args.reason,
                "approver": args.approver,
                "updated_at": now_iso(),
            }
        all_approvals = parse_prompt_approval(approval_path)
        if all(item.get("status") == "已确认" for item in all_approvals.values()):
            gate["prompt_approval"] = "confirmed"
        else:
            gate["prompt_approval"] = "partial"
        gate["next_action"] = f"Run preflight-jimeng for Segment {updated[0]:02d} before opening Jimeng generation."
        json_dump(gate_path, gate)

    print(f"Updated prompt approval: {approval_path}")
    print("Segments: " + ", ".join(f"{segment:02d}" for segment in updated))


def add_preflight_check(checks: list[dict[str, str]], item: str, status: str, detail: str) -> None:
    checks.append({"item": item, "status": status, "detail": detail})


def preflight_jimeng(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")

    gate_path = run_dir / "gate_status.json"
    requirements_path = first_existing_path(
        run_dir / "creative_requirements.json",
        run_dir / "run_docs" / "planning" / "creative_requirements.json",
    )
    segments_path = run_dir / "segments.json"
    approval_path = first_existing_path(
        run_dir / "prompt_approval.md",
        run_dir / "run_docs" / "planning" / "prompt_approval.md",
    )
    prompt_pack_path = first_existing_path(
        run_dir / "jimeng_prompt_pack.md",
        run_dir / "jimeng_workspace" / "prompts" / "jimeng_prompt_pack.md",
    )

    gate = json_load(gate_path) if gate_path.exists() else {}
    requirements = json_load(requirements_path) if requirements_path.exists() else {}
    segments = json_load(segments_path) if segments_path.exists() else []
    approvals = parse_prompt_approval(approval_path)
    segment = args.segment
    expected_confirmation = args.confirmation_phrase or f"确认生成 Segment {segment:02d}"
    checks: list[dict[str, str]] = []

    segment_known = any(item.get("index") == segment for item in segments)
    add_preflight_check(
        checks,
        "Segment exists",
        "PASS" if segment_known else "BLOCK",
        f"Segment {segment:02d} {'is present in segments.json' if segment_known else 'is missing from segments.json'}",
    )

    missing_requirements = validate_requirements(requirements) if requirements else ["creative_requirements.json"]
    add_preflight_check(
        checks,
        "Creative requirements confirmed",
        "PASS" if not missing_requirements else "BLOCK",
        "All required creative fields are confirmed" if not missing_requirements else "Missing: " + ", ".join(missing_requirements),
    )

    approval = approvals.get(segment, {})
    approval_status = approval.get("status", "missing")
    add_preflight_check(
        checks,
        "Prompt approval",
        "PASS" if approval_status == "已确认" else "BLOCK",
        f"prompt_approval.md status for Segment {segment:02d}: {approval_status}",
    )

    add_preflight_check(
        checks,
        "Prompt pack section",
        "PASS" if prompt_pack_has_segment(prompt_pack_path, segment) else "BLOCK",
        f"jimeng_prompt_pack.md {'contains' if prompt_pack_has_segment(prompt_pack_path, segment) else 'does not contain'} Segment {segment:02d}",
    )

    current_gate = find_segment_item(gate, segment) if gate else None
    current_status = (current_gate or {}).get("status", "missing")
    if current_status in {"not_returned", "awaiting_generation_approval", "generated_pending_review", "downloaded"}:
        status_result = "PASS"
    elif current_status == "must_rerun":
        status_result = "PASS" if args.allow_rerun else "BLOCK"
    elif current_status == "reviewed":
        status_result = "WARN"
    else:
        status_result = "BLOCK"
    add_preflight_check(
        checks,
        "Current segment gate",
        status_result,
        f"gate_status.json status for Segment {segment:02d}: {current_status}",
    )

    if segment == 1:
        previous_ok = True
        previous_detail = "Segment 01 may start a new Jimeng video dialogue"
    else:
        previous = find_segment_item(gate, segment - 1) if gate else None
        previous_decision = (previous or {}).get("decision", "")
        previous_ok = (previous or {}).get("status") == "reviewed" and "可进入" in previous_decision
        previous_detail = (
            f"Segment {segment - 1:02d}: status={(previous or {}).get('status', 'missing')}, "
            f"decision={previous_decision or 'missing'}"
        )
    add_preflight_check(
        checks,
        "Previous segment accepted",
        "PASS" if previous_ok else "BLOCK",
        previous_detail,
    )

    aspect_ratio = str(requirements.get("aspect_ratio", ""))
    add_preflight_check(
        checks,
        "Aspect ratio expectation",
        "PASS" if args.expected_aspect in aspect_ratio else "WARN",
        f"Expected Jimeng ratio {args.expected_aspect}; requirements say: {aspect_ratio or 'missing'}",
    )

    duration = str(requirements.get("segment_duration_seconds", ""))
    add_preflight_check(
        checks,
        "Duration expectation",
        "PASS" if duration == str(args.expected_duration) else "WARN",
        f"Expected Jimeng duration {args.expected_duration}s; requirements say: {duration or 'missing'}",
    )

    no_text_sources = "\n".join(
        [
            str(requirements.get("visual_style", "")),
            "\n".join(requirements.get("compliance_boundary", [])),
        ]
    )
    no_text_terms = ["无字幕", "不要让模型生成字幕", "不要生成字幕", "無字幕"]
    add_preflight_check(
        checks,
        "No generated text constraint",
        "PASS" if any(term in no_text_sources for term in no_text_terms) else "WARN",
        "Generation-stage no-subtitle/no-text constraint found" if any(term in no_text_sources for term in no_text_terms) else "No explicit no-subtitle constraint found in requirements",
    )

    if segment == 1:
        dialogue_detail = "Use a new Jimeng video dialogue for Segment 01"
    else:
        dialogue_detail = gate.get(
            "dialogue_policy",
            "Segment 02+ should continue inside the confirmed Segment 01 video dialogue.",
        )
    add_preflight_check(checks, "Jimeng dialogue policy", "MANUAL", dialogue_detail)
    identity = jimeng_identity(gate, run_dir)
    identity_missing = [
        label
        for label, value in [
            ("final_video_name", identity.get("final_video_name", "")),
            ("bound_dialogue_name", identity.get("bound_dialogue_name", "")),
            ("workspace_url", identity.get("workspace_url", "")),
        ]
        if not value
    ]
    add_preflight_check(
        checks,
        "Name identity bound",
        "PASS" if not identity_missing else "BLOCK",
        (
            f"Final video: {identity.get('final_video_name')}; "
            f"bound dialogue: {identity.get('bound_dialogue_name')}; "
            f"workspace: {identity.get('workspace_url')}"
        )
        if not identity_missing
        else "Missing: " + ", ".join(identity_missing),
    )
    add_preflight_check(
        checks,
        "Per-video dialogue rule",
        "MANUAL",
        "Confirm this dialogue belongs to the current video only; do not reuse it for a different video project.",
    )
    add_preflight_check(checks, "Jimeng model", "MANUAL", f"Verify model is {args.expected_model}")
    add_preflight_check(checks, "Reference type", "MANUAL", f"Verify reference type is {args.expected_reference}")

    if args.confirm_text:
        confirmed = args.allow_operator_confirm and args.confirm_text.strip() == expected_confirmation
        if args.allow_operator_confirm:
            confirm_detail = f"Received: {args.confirm_text.strip()}; expected: {expected_confirmation}"
        else:
            confirm_detail = (
                "Operator-supplied confirmation text is not enough. Present this preflight to the user and wait "
                f"for exact user approval: {expected_confirmation}"
            )
    else:
        confirmed = False
        confirm_detail = f"Missing explicit confirmation. Expected exact text: {expected_confirmation}"
    add_preflight_check(
        checks,
        "Human generation confirmation",
        "PASS" if confirmed else "BLOCK",
        confirm_detail,
    )

    blocking = [check for check in checks if check["status"] == "BLOCK"]
    report_status = "ready_for_manual_generate" if not blocking else "blocked"
    preflight_dir = run_dir / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    report_path = preflight_dir / f"segment_{segment:02d}_preflight.md"
    lines = [
        f"# Segment {segment:02d} Jimeng Preflight",
        "",
        f"Status: `{report_status}`",
        f"Created at: `{now_iso()}`",
        "",
        "## Jimeng Settings To Verify",
        "",
        f"- Session: `{args.session}`",
        f"- Mode: `视频生成`",
        f"- Model: `{args.expected_model}`",
        f"- Reference type: `{args.expected_reference}`",
        f"- Aspect ratio: `{args.expected_aspect}`",
        f"- Duration: `{args.expected_duration}s`",
        "- Generation-stage text: `无字幕 / 无花字 / 无贴片文字 / 无 Logo`",
        "",
        "## Checks",
        "",
        "| Check | Status | Detail |",
        "|---|---|---|",
    ]
    for check in checks:
        detail = check["detail"].replace("|", "\\|")
        lines.append(f"| {check['item']} | `{check['status']}` | {detail} |")
    lines.extend(
        [
            "",
            "## Next Action",
            "",
        ]
    )
    if blocking:
        lines.append("Do not click generate yet. Resolve the blocking checks above, then rerun preflight.")
    else:
        lines.append(f"All blocking checks passed. Present this report and a current Jimeng screenshot to the user before clicking generate for Segment {segment:02d}.")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if gate_path.exists():
        gate = json_load(gate_path)
        preflights = gate.setdefault("preflights", {})
        preflights[f"segment_{segment:02d}"] = {
            "status": report_status,
            "report": str(report_path.relative_to(run_dir)),
            "checked_at": now_iso(),
            "blocking_checks": [check["item"] for check in blocking],
        }
        gate["next_action"] = (
            f"Resolve preflight blockers in {report_path.relative_to(run_dir)}."
            if blocking
            else f"Manually generate Segment {segment:02d} in Jimeng, then run download-segment."
        )
        json_dump(gate_path, gate)

    print(f"Preflight status: {report_status}")
    print(f"Report: {report_path}")
    if blocking:
        print("Blocking checks:")
        for check in blocking:
            print(f"- {check['item']}: {check['detail']}")
        if args.strict:
            raise SystemExit(2)


def signed_dingtalk_webhook(webhook: str, secret: str | None) -> str:
    if not secret:
        return webhook
    timestamp = str(round(datetime.now().timestamp() * 1000))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = parse.quote_plus(base64.b64encode(digest))
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp}&sign={sign}"


def post_dingtalk_markdown(
    webhook: str,
    secret: str | None,
    title: str,
    text: str,
    at_mobiles: list[str],
    at_all: bool,
) -> dict[str, Any]:
    url = signed_dingtalk_webhook(webhook, secret)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text,
        },
        "at": {
            "atMobiles": at_mobiles,
            "isAtAll": at_all,
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=15) as resp:
            response_body = resp.read().decode("utf-8")
    except error.URLError as exc:
        fail(f"DingTalk notification failed: {exc}")
    try:
        return json.loads(response_body)
    except json.JSONDecodeError:
        return {"raw_response": response_body}


def notify_dingtalk(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")

    review_file = resolve_run_file(run_dir, args.review_file)
    if review_file and not review_file.exists():
        fail(f"Review file not found: {review_file}")
    prompt_file = resolve_run_file(run_dir, args.prompt_file)
    if prompt_file and not prompt_file.exists():
        fail(f"Prompt file not found: {prompt_file}")
    screenshot_file = resolve_run_file(run_dir, args.screenshot_file)
    if screenshot_file and not screenshot_file.exists():
        fail(f"Screenshot file not found: {screenshot_file}")
    prompt_text = prompt_file.read_text(encoding="utf-8").strip() if prompt_file else ""
    prompt_preview, prompt_truncated = clipped_text(prompt_text, args.prompt_preview_chars)

    webhook = args.webhook or os.environ.get("DINGTALK_WEBHOOK")
    secret = args.secret or os.environ.get("DINGTALK_SECRET")
    at_mobiles = split_csv(args.at_mobiles or os.environ.get("DINGTALK_AT_MOBILES"))
    at_all = args.at_all or parse_env_bool(os.environ.get("DINGTALK_AT_ALL"))
    decision = args.decision or "待审核"
    reviewer_hint = args.reviewer or "请对应审核人处理"
    mentions = " ".join(f"@{mobile}" for mobile in at_mobiles)
    total_segments = args.total_segments
    segments_path = run_dir / "segments.json"
    if total_segments is None and segments_path.exists():
        total_segments = len(json_load(segments_path))
    segment_progress = f"Segment {args.segment:02d}/{total_segments:02d}" if total_segments else f"Segment {args.segment:02d}"

    stage = slugify(args.stage)
    title_prefix = "AI视频生成前待确认" if stage == "preflight" else "AI视频片段待审核"
    title = f"{title_prefix} {segment_progress}"
    approval_phrase = args.approval_phrase or f"确认生成 Segment {args.segment:02d}"
    lines = [
        f"### {title}",
        "",
        f"- 项目：{run_dir.name}",
        f"- 片段进度：{segment_progress}",
        f"- 总片段数：{total_segments if total_segments else '未识别'}",
        f"- 当前判断：{decision}",
        f"- 审核要求：{reviewer_hint}",
        f"- 即梦后台：{args.jimeng_url or '请登录即梦后台查看对应生成记录'}",
        f"- 质检记录：{review_file.name if review_file else '已在本地生成，请联系中控查看'}",
    ]
    if screenshot_file:
        lines.append(f"- 当前页面截图：{screenshot_file.name}")
    if prompt_preview:
        lines.extend(
            [
                "",
                "#### 已填入即梦的提示词（可复制）",
                "",
                "```text",
                prompt_preview,
                "```",
            ]
        )
        if prompt_truncated:
            lines.append(f"提示词过长，钉钉仅展示前 {args.prompt_preview_chars} 字；完整内容见本地提示词文件。")
    lines.append("")
    if stage == "preflight":
        lines.append(f"请审核上方提示词和即梦页面设置；确认无误后在 Codex 对话回复：`{approval_phrase}`。未确认前不会点击生成。")
    else:
        lines.append("请到即梦后台检查：画面是否可用、口播是否准确、是否无内嵌字幕、是否可进入下一段或需要重跑。")
    if mentions:
        lines.extend(["", mentions])
    text = "\n".join(lines)

    manifest = {
        "created_at": now_iso(),
        "channel": "dingtalk",
        "stage": stage,
        "segment": args.segment,
        "total_segments": total_segments,
        "decision": decision,
        "reviewer": reviewer_hint,
        "jimeng_url": args.jimeng_url or "",
        "review_file": str(review_file.resolve()) if review_file else "",
        "prompt_file": str(prompt_file.resolve()) if prompt_file else "",
        "prompt_preview_chars": args.prompt_preview_chars,
        "prompt_truncated": prompt_truncated,
        "screenshot_file": str(screenshot_file.resolve()) if screenshot_file else "",
        "approval_phrase": approval_phrase,
        "at_mobiles": at_mobiles,
        "at_all": at_all,
        "dry_run": args.dry_run,
        "message_title": title,
        "message_text": text,
    }

    notify_dir = run_dir / "notifications"
    notify_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = notify_dir / f"{timestamp}_segment_{args.segment:02d}_{stage}_dingtalk.json"

    if args.dry_run:
        manifest["status"] = "dry_run"
        json_dump(manifest_path, manifest)
        print(f"Dry run DingTalk notification written: {manifest_path}")
        print(text)
        return

    if not webhook:
        fail("Missing DingTalk webhook. Set DINGTALK_WEBHOOK or pass --webhook.")
    response = post_dingtalk_markdown(webhook, secret, title, text, at_mobiles, at_all)
    manifest["status"] = "sent"
    manifest["response"] = response
    json_dump(manifest_path, manifest)
    print(f"Sent DingTalk notification. Manifest: {manifest_path}")


def first_actionable_segment(gate: dict[str, Any]) -> int | None:
    reviews = gate.get("segment_reviews", [])
    for item in reviews:
        segment = item.get("segment")
        if not isinstance(segment, int):
            continue
        status = item.get("status", "")
        if status == "generated_pending_review":
            return segment
        if status != "reviewed":
            previous = find_segment_item(gate, segment - 1) if segment > 1 else None
            if segment == 1 or (previous or {}).get("status") == "reviewed":
                return segment
    return None


def jimeng_identity(gate: dict[str, Any], run_dir: Path) -> dict[str, str]:
    dialogue = gate.get("jimeng_dialogue", {})
    smart_edit = gate.get("smart_edit", {})
    final_name = (
        dialogue.get("original_full_name")
        or smart_edit.get("name", "").replace("_smart_v1", "")
        or run_dir.name
    )
    return {
        "project_name": run_dir.name,
        "final_video_name": final_name,
        "bound_dialogue_name": dialogue.get("accepted_display_name") or dialogue.get("required_name") or "",
        "workspace_url": dialogue.get("workspace_url", ""),
        "dialogue_scope": dialogue.get("scope", "current_video_project_only"),
        "reuse_policy": dialogue.get("reuse_policy", "Do not reuse this dialogue for another video project."),
        "segment_policy": dialogue.get("segment_policy", "All segments/reruns/takes of this video must stay in this dialogue."),
    }


def write_generation_approval_package(
    run_dir: Path,
    segment: int,
    prompt_path: Path,
    tail_frame: Path | None,
    reference_video: Path | None,
    dialogue_name: str,
    approval_phrase: str,
    identity: dict[str, str] | None = None,
) -> Path:
    preflight_dir = run_dir / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    approval_path = preflight_dir / f"segment{segment:02d}_generation_approval.md"
    lines = [
        f"# Segment {segment:02d} Generation Approval",
        "",
        "Status: `awaiting_user_generation_approval`",
        "",
        "## Name Confirmation",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Current video project | `{(identity or {}).get('project_name', run_dir.name)}` |",
        f"| Final video name | `{(identity or {}).get('final_video_name', '未设置')}` |",
        f"| Bound Jimeng dialogue for this video | `{(identity or {}).get('bound_dialogue_name', dialogue_name or '未设置')}` |",
        f"| Jimeng workspace URL | `{(identity or {}).get('workspace_url', '未设置')}` |",
        f"| Dialogue scope | `{(identity or {}).get('dialogue_scope', 'current_video_project_only')}` |",
        "",
        "Name rule:",
        "",
        "```text",
        "One video project = one bound Jimeng dialogue. All Segment / rerun / take work for this video must stay in this dialogue. A new video must create and bind a new dialogue.",
        "```",
        "",
        "## Dialogue",
        "",
        "Accepted Jimeng dialogue name:",
        "",
        "```text",
        dialogue_name or "未设置",
        "```",
        "",
        "## Continuity Reference",
        "",
    ]
    if reference_video:
        lines.extend(
            [
                "Required reference video:",
                "",
                "```text",
                str(reference_video.relative_to(run_dir)),
                "```",
                "",
                "Jimeng operation:",
                "",
                "```text",
                "Before generation, use the previous approved video card menu item: 用作参考视频",
                "```",
                "",
            ]
        )
    if tail_frame:
        lines.extend(
            [
                "Tail frame file:",
                "",
                "```text",
                str(tail_frame.relative_to(run_dir)),
                "```",
                "",
            ]
        )
    else:
        lines.extend(["Tail frame file: `not required or not available`", ""])
    lines.extend(
        [
            "## Prompt File",
            "",
            "```text",
            str(prompt_path.relative_to(run_dir)),
            "```",
            "",
            "## Required User Approval",
            "",
            "Codex must not click generate until the user replies exactly:",
            "",
            "```text",
            approval_phrase,
            "```",
            "",
        ]
    )
    approval_path.write_text("\n".join(lines), encoding="utf-8")
    return approval_path


def continue_pipeline(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    gate_path = run_dir / "gate_status.json"
    if not gate_path.exists():
        fail(f"Missing gate_status.json: {gate_path}")
    gate = json_load(gate_path)
    segment = args.segment or first_actionable_segment(gate)
    if not segment:
        print("Pipeline has no actionable segment.")
        return

    item = find_segment_item(gate, segment)
    if item is None:
        fail(f"Segment {segment:02d} is missing from gate_status.json")
    status = item.get("status", "")
    if status == "generated_pending_review":
        print(f"Segment {segment:02d} is already generated and pending user review.")
        print(f"Review file: {item.get('review_file', '')}")
        return
    if status == "reviewed":
        print(f"Segment {segment:02d} is already reviewed.")
        return

    if segment > 1:
        previous = find_segment_item(gate, segment - 1) or {}
        if previous.get("status") != "reviewed":
            fail(f"Segment {segment - 1:02d} is not reviewed; cannot prepare Segment {segment:02d}.")

    prompt_pack_path = run_dir / "jimeng_prompt_pack.md"
    base_prompt = extract_segment_prompt(prompt_pack_path, segment)
    dialogue = gate.get("jimeng_dialogue", {})
    identity = jimeng_identity(gate, run_dir)
    dialogue_name = dialogue.get("accepted_display_name") or dialogue.get("required_name") or ""
    tail_frame = run_dir / "reviews" / f"segment_{segment - 1:02d}" / "tail_frame.jpg" if segment > 1 else None
    if tail_frame and not tail_frame.exists():
        fail(f"Missing continuity tail frame: {tail_frame}")
    reference_video = find_segment_video_optional(run_dir, segment - 1) if segment > 1 else None
    if segment > 1 and reference_video is None:
        fail(f"Missing approved previous segment video for reference: Segment {segment - 1:02d}")

    preflight_dir = run_dir / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = preflight_dir / f"segment_{segment:02d}_prompt_with_tail_frame.txt"
    continuity_lines = []
    if segment > 1:
        continuity_lines.append(
            f"必须先将 Segment {segment - 1:02d} 已通过完整视频通过右键/更多菜单「用作参考视频」加入参考，再生成本段。"
            f"参考视频文件：{reference_video.relative_to(run_dir) if reference_video else ''}。"
            f"同时以 Segment {segment - 1:02d} 已通过片段的真实尾帧作为画面连续性参考。"
            f"参考尾帧文件：reviews/segment_{segment - 1:02d}/tail_frame.jpg。"
        )
    if dialogue_name:
        continuity_lines.append(f"在同一个已确认即梦对话「{dialogue_name}」中继续生成，不新建对话。")
    if segment > 1:
        continuity_lines.append("保持上一段结尾的人物、服装、光线、场景、镜头方向和画面质感连续。")
    prompt_text = "".join(continuity_lines).strip()
    if prompt_text:
        prompt_text += "\n\n"
    prompt_text += base_prompt
    prompt_path.write_text(prompt_text.strip() + "\n", encoding="utf-8")

    approval_phrase = args.approval_phrase or f"确认生成 Segment {segment:02d}"
    approval_path = write_generation_approval_package(
        run_dir,
        segment,
        prompt_path,
        tail_frame,
        reference_video,
        dialogue_name,
        approval_phrase,
        identity,
    )

    gate = json_load(gate_path)
    item = find_segment_item(gate, segment)
    if item is None:
        fail(f"Segment {segment:02d} disappeared from gate_status.json")
    item["status"] = "awaiting_generation_approval"
    item["decision"] = f"Segment {segment:02d} generation preflight prepared; waiting for user approval."
    item["generation_prompt_file"] = str(prompt_path.relative_to(run_dir))
    item["generation_approval_file"] = str(approval_path.relative_to(run_dir))
    if tail_frame:
        item["continuity_tail_frame"] = str(tail_frame.relative_to(run_dir))
    if reference_video:
        item["reference_video_required"] = True
        item["reference_video"] = str(reference_video.relative_to(run_dir))
        item["reference_video_operation"] = "Use previous approved video menu item: 用作参考视频 before clicking generate."
    gate["next_action"] = f"Review {approval_path.relative_to(run_dir)} and reply exactly: {approval_phrase}"
    json_dump(gate_path, gate)

    if args.notify_dingtalk:
        notify_args = argparse.Namespace(
            run_dir=str(run_dir),
            segment=segment,
            stage="preflight",
            total_segments=args.total_segments,
            review_file=str(approval_path.resolve()),
            prompt_file=str(prompt_path.resolve()),
            prompt_preview_chars=args.prompt_preview_chars,
            screenshot_file=None,
            approval_phrase=approval_phrase,
            jimeng_url=dialogue.get("workspace_url", ""),
            decision="生成前待确认",
            reviewer=f"请确认 Segment {segment:02d} 生成前信息；确认后回复：{approval_phrase}",
            webhook=args.webhook,
            secret=args.secret,
            at_mobiles=args.at_mobiles,
            at_all=args.at_all,
            dry_run=args.dry_run,
        )
        notify_dingtalk(notify_args)

    print(f"Prepared Segment {segment:02d} generation preflight.")
    print(f"Prompt: {prompt_path}")
    print(f"Approval: {approval_path}")
    print(f"Next approval phrase: {approval_phrase}")


def resolve_existing_run_path(run_dir: Path, value: str | None) -> Path | None:
    path = resolve_run_file(run_dir, value)
    if path and path.exists():
        return path
    if value and not Path(value).is_absolute():
        edit_path = run_dir / "edit" / value
        if edit_path.exists():
            return edit_path
    return None


def first_existing_path(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def reconcile_state(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    gate_path = run_dir / "gate_status.json"
    if not gate_path.exists():
        fail(f"Missing gate_status.json: {gate_path}")

    gate = json_load(gate_path)
    report: dict[str, Any] = {
        "created_at": now_iso(),
        "run_dir": str(run_dir.resolve()),
        "mode": "write" if args.write else "dry_run",
        "checks": [],
        "changes": [],
        "blockers": [],
    }

    def check(name: str, status: str, detail: str) -> None:
        report["checks"].append({"name": name, "status": status, "detail": detail})

    for item in gate.get("segment_reviews", []):
        segment = item.get("segment")
        downloaded = resolve_existing_run_path(run_dir, item.get("downloaded_file"))
        review_file = resolve_existing_run_path(run_dir, item.get("review_file"))
        check(
            f"Segment {segment:02d} downloaded file" if isinstance(segment, int) else "Segment downloaded file",
            "PASS" if downloaded else "WARN",
            str(downloaded.relative_to(run_dir)) if downloaded and downloaded.is_relative_to(run_dir) else "missing or not recorded",
        )
        check(
            f"Segment {segment:02d} review file" if isinstance(segment, int) else "Segment review file",
            "PASS" if review_file else "WARN",
            str(review_file.relative_to(run_dir)) if review_file and review_file.is_relative_to(run_dir) else "missing or not recorded",
        )

    smart_edit = gate.get("smart_edit", {})
    smart_output = resolve_existing_run_path(run_dir, smart_edit.get("output"))
    smart_contact = resolve_existing_run_path(run_dir, smart_edit.get("contact_sheet"))
    smart_report = resolve_existing_run_path(run_dir, smart_edit.get("report"))
    check("Smart edit output", "PASS" if smart_output else "WARN", str(smart_output.relative_to(run_dir)) if smart_output else "missing")
    check("Smart edit contact sheet", "PASS" if smart_contact else "WARN", str(smart_contact.relative_to(run_dir)) if smart_contact else "missing")
    check("Smart edit report", "PASS" if smart_report else "WARN", str(smart_report.relative_to(run_dir)) if smart_report else "missing")

    accepted_segments = [int(item) for item in split_csv(args.accept_segments)]
    if accepted_segments:
        if not smart_output:
            report["blockers"].append("Cannot accept segment(s) for edit because smart edit output is missing.")
        for segment in accepted_segments:
            item = find_segment_item(gate, segment)
            if item is None:
                report["blockers"].append(f"Segment {segment:02d} is missing from gate_status.json.")
                continue
            old_status = item.get("status", "")
            old_decision = item.get("decision", "")
            decision = args.decision or "可进入剪辑/成片（用户接受瑕疵）"
            if old_status != "reviewed" or old_decision != decision:
                report["changes"].append(
                    {
                        "field": f"segment_{segment:02d}",
                        "from": {"status": old_status, "decision": old_decision},
                        "to": {"status": "reviewed", "decision": decision},
                    }
                )
                if args.write and not report["blockers"]:
                    item["status"] = "reviewed"
                    item["decision"] = decision
                    item["reviewed_by"] = args.reviewer
                    item["reviewed_at"] = now_iso()
                    item["review_note"] = args.note or "User accepted this segment for smart edit despite known quality caveats."

    if smart_output:
        output_meta = read_metadata(smart_output)
        smart_edit.update(
            {
                "status": "completed",
                "output": str(smart_output.relative_to(run_dir)) if smart_output.is_relative_to(run_dir) else str(smart_output),
                "contact_sheet": str(smart_contact.relative_to(run_dir)) if smart_contact and smart_contact.is_relative_to(run_dir) else smart_edit.get("contact_sheet", ""),
                "report": str(smart_report.relative_to(run_dir)) if smart_report and smart_report.is_relative_to(run_dir) else smart_edit.get("report", ""),
                "sha256": file_sha256(smart_output),
                "duration_seconds": output_meta["duration_seconds"],
                "width": output_meta["width"],
                "height": output_meta["height"],
                "frame_rate": output_meta["frame_rate"],
                "reconciled_at": now_iso(),
            }
        )
        gate["smart_edit"] = smart_edit

    if report["blockers"]:
        report["result"] = "blocked"
    elif report["changes"] and args.write:
        report["result"] = "updated"
        gate["next_action"] = "Review smart edit output, then proceed to subtitles/CTA/final delivery package."
    elif report["changes"]:
        report["result"] = "dry_run_changes_available"
    else:
        report["result"] = "already_consistent"

    report_dir = run_dir / "run_docs" / "state"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"reconcile_state_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    json_dump(report_path, report)
    if args.write and not report["blockers"]:
        json_dump(gate_path, gate)

    print(f"Reconcile result: {report['result']}")
    print(f"Report: {report_path}")
    if report["blockers"]:
        fail("State reconciliation blocked:\n- " + "\n- ".join(report["blockers"]))
    if report["changes"] and not args.write:
        print("Dry run only. Re-run with --write to apply changes.")


def copy_tree_resolved(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=False)


def package_delivery(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    destination_root = Path(args.destination_root).expanduser()
    package_name = args.name or run_dir.name
    package_dir = destination_root / package_name
    package_dir.mkdir(parents=True, exist_ok=True)

    required_dirs = ["00_当前成片", "01_审核材料", "02_原始片段", "03_项目文档"]
    missing = [name for name in required_dirs if not (run_dir / name).exists()]
    if missing:
        fail("Run is missing delivery entry folders: " + ", ".join(missing))

    for name in required_dirs:
        copy_tree_resolved(run_dir / name, package_dir / name)

    for name in ["RUN_INDEX.json", "_START_HERE.md", "gate_status.json"]:
        source = run_dir / name
        if source.exists():
            shutil.copy2(source, package_dir / name)

    current_video = package_dir / "00_当前成片" / args.current_video_name
    if not current_video.exists():
        candidates = sorted((package_dir / "00_当前成片").glob("*.mp4"))
        if not candidates:
            fail(f"No current mp4 found under {package_dir / '00_当前成片'}")
        current_video = candidates[0]
    meta = read_metadata(current_video)
    digest = file_sha256(current_video)

    readme = package_dir / "README_安全归档说明.md"
    readme.write_text(
        "\n".join(
            [
                "# AI 视频项目安全归档说明",
                "",
                "本目录是从自动化工作目录复制出来的安全交付包，原工作目录未删除、未移动。",
                "",
                "## 日常查看",
                "",
                "- `00_当前成片/`：当前智能剪辑审核版、接触表、报告",
                "- `01_审核材料/`：Segment 审核、生成前审核、钉钉通知记录",
                "- `02_原始片段/`：即梦返回片段和下载记录",
                "- `03_项目文档/`：项目入口、索引、即梦工作记录、整理后的文档",
                "",
                "## 安全校验",
                "",
                "- 本归档没有软链接，文件已实拷贝，脱离原目录也能打开。",
                f"- 当前成片：`00_当前成片/{current_video.name}`",
                f"- 成片规格：{meta['width']}x{meta['height']}，{meta['frame_rate']}，约 {meta['duration_seconds']} 秒",
                f"- 成片 SHA256：`{digest}`",
                "",
                "## 注意",
                "",
                "自动化脚本仍以原工作目录为主。本目录适合人工审核、备份、转交和归档。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = {
        "created_at": now_iso(),
        "source_run_dir": str(run_dir.resolve()),
        "package_dir": str(package_dir.resolve()),
        "current_video": str(current_video.relative_to(package_dir)),
        "current_video_sha256": digest,
        "current_video_metadata": meta,
        "entry_folders": required_dirs,
        "symlink_count": sum(1 for path in package_dir.rglob("*") if path.is_symlink()),
    }
    json_dump(package_dir / "DELIVERY_MANIFEST.json", manifest)
    print(f"Created delivery package: {package_dir}")
    print(f"Current video SHA256: {digest}")


def bind_name_gate(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    gate_path = run_dir / "gate_status.json"
    if not gate_path.exists():
        fail(f"Missing gate_status.json: {gate_path}")

    gate = json_load(gate_path)
    existing = jimeng_identity(gate, run_dir)
    requested = {
        "final_video_name": args.final_video_name,
        "bound_dialogue_name": args.jimeng_dialogue,
        "workspace_url": args.workspace_url,
    }
    for key, value in requested.items():
        if value and existing.get(key) and existing.get(key) != value and not args.force:
            fail(
                f"Refusing to overwrite {key}: existing `{existing.get(key)}` vs requested `{value}`. "
                "Use --force only after user explicitly confirms the rename/rebind."
            )

    apply_name_gate_to_gate(
        gate,
        run_dir,
        args.final_video_name,
        args.jimeng_dialogue,
        args.workspace_url,
        args.binder,
    )
    gate["next_action"] = "Run continue-safe before any generation, editing, notification, or delivery action."
    json_dump(gate_path, gate)

    report = {
        "created_at": now_iso(),
        "run_dir": str(run_dir.resolve()),
        "changes": {"from": existing, "to": jimeng_identity(gate, run_dir)},
        "identity": jimeng_identity(gate, run_dir),
    }
    report_path = run_dir / "run_docs" / "state" / f"name_gate_bind_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    json_dump(report_path, report)
    print(f"Bound name gate: {report_path}")
    print(f"Final video name: {report['identity']['final_video_name']}")
    print(f"Jimeng dialogue: {report['identity']['bound_dialogue_name']}")


def delivery_package_path(args: argparse.Namespace, run_dir: Path, gate: dict[str, Any]) -> Path:
    destination_root = Path(args.destination_root).expanduser()
    name_gate = gate.get("name_gate", {})
    package_name = args.package_name or name_gate.get("final_video_name") or jimeng_identity(gate, run_dir)["final_video_name"] or run_dir.name
    return destination_root / package_name.replace("×", "x")


def add_safety_check(report: dict[str, Any], name: str, status: str, detail: str) -> None:
    report["checks"].append({"name": name, "status": status, "detail": detail})
    if status == "BLOCK":
        report["blockers"].append(f"{name}: {detail}")
    elif status == "WARN":
        report["warnings"].append(f"{name}: {detail}")


def continue_safe(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.exists():
        fail(f"Run directory not found: {run_dir}")
    gate_path = run_dir / "gate_status.json"
    if not gate_path.exists():
        fail(f"Missing gate_status.json: {gate_path}")
    gate = json_load(gate_path)
    stage = args.stage
    report: dict[str, Any] = {
        "created_at": now_iso(),
        "run_dir": str(run_dir.resolve()),
        "stage": stage,
        "checks": [],
        "warnings": [],
        "blockers": [],
    }

    identity = jimeng_identity(gate, run_dir)
    name_gate = gate.get("name_gate", {})
    add_safety_check(
        report,
        "Name gate active",
        "PASS" if name_gate.get("status") == "active" else "BLOCK",
        name_gate.get("rule", "name_gate.status is not active"),
    )
    add_safety_check(
        report,
        "Current video project name",
        "PASS" if name_gate.get("current_video_project", run_dir.name) == run_dir.name else "BLOCK",
        f"run={run_dir.name}; name_gate={name_gate.get('current_video_project')}",
    )
    add_safety_check(
        report,
        "Final video name",
        "PASS" if identity.get("final_video_name") else "BLOCK",
        identity.get("final_video_name") or "missing",
    )
    add_safety_check(
        report,
        "Bound Jimeng dialogue",
        "PASS" if identity.get("bound_dialogue_name") else "BLOCK",
        identity.get("bound_dialogue_name") or "missing",
    )
    add_safety_check(
        report,
        "Dialogue scope",
        "PASS" if identity.get("dialogue_scope") == "current_video_project_only" else "BLOCK",
        identity.get("dialogue_scope") or "missing",
    )
    if identity.get("workspace_url"):
        add_safety_check(report, "Workspace URL", "PASS", identity["workspace_url"])
    else:
        add_safety_check(report, "Workspace URL", "BLOCK", "missing")

    if args.require_browser or stage in {"preflight", "generation"}:
        browser = gate.get("jimeng_dedicated_browser", {})
        add_safety_check(
            report,
            "Dedicated browser verified",
            "PASS" if browser.get("status") == "verified_opencli_readable" else "BLOCK",
            browser.get("status", "missing"),
        )
        add_safety_check(
            report,
            "Dedicated browser session policy",
            "PASS" if browser.get("opencli_session") else "BLOCK",
            browser.get("opencli_session", "missing"),
        )

    if stage in {"preflight", "generation"}:
        if args.segment is None:
            add_safety_check(report, "Segment selected", "BLOCK", "--segment is required for preflight/generation safety")
        else:
            item = find_segment_item(gate, args.segment)
            if item is None:
                add_safety_check(report, "Segment exists in gate", "BLOCK", f"Segment {args.segment:02d} missing")
            else:
                add_safety_check(report, "Segment exists in gate", "PASS", f"status={item.get('status')}")
                if args.segment > 1:
                    prev = find_segment_item(gate, args.segment - 1) or {}
                    add_safety_check(
                        report,
                        "Previous segment reviewed",
                        "PASS" if prev.get("status") == "reviewed" else "BLOCK",
                        f"Segment {args.segment - 1:02d} status={prev.get('status', 'missing')}",
                    )
        if stage == "generation" and not args.user_confirmed:
            add_safety_check(
                report,
                "Explicit user confirmation",
                "BLOCK",
                "Generation stages require --user-confirmed after the approval package is shown.",
            )

    if stage in {"edit", "package", "handoff"}:
        smart = gate.get("smart_edit", {})
        smart_output = resolve_existing_run_path(run_dir, smart.get("output"))
        add_safety_check(
            report,
            "Smart edit output",
            "PASS" if smart_output else "BLOCK",
            str(smart_output.relative_to(run_dir)) if smart_output and smart_output.is_relative_to(run_dir) else "missing",
        )
        if smart_output and smart.get("sha256"):
            digest = file_sha256(smart_output)
            add_safety_check(
                report,
                "Smart edit hash",
                "PASS" if digest == smart.get("sha256") else "BLOCK",
                f"actual={digest}; expected={smart.get('sha256')}",
            )
        for item in gate.get("segment_reviews", []):
            segment = item.get("segment")
            if isinstance(segment, int):
                add_safety_check(
                    report,
                    f"Segment {segment:02d} reviewed",
                    "PASS" if item.get("status") == "reviewed" else "BLOCK",
                    f"status={item.get('status')}; decision={item.get('decision', '')}",
                )

    if stage in {"package", "handoff"}:
        package_dir = delivery_package_path(args, run_dir, gate)
        manifest = package_dir / "DELIVERY_MANIFEST.json"
        add_safety_check(
            report,
            "Delivery package manifest",
            "PASS" if manifest.exists() else "BLOCK",
            str(manifest),
        )
        if manifest.exists():
            data = json_load(manifest)
            add_safety_check(
                report,
                "Delivery package symlinks",
                "PASS" if int(data.get("symlink_count", -1)) == 0 else "BLOCK",
                f"symlink_count={data.get('symlink_count')}",
            )
            video = package_dir / data.get("current_video", "")
            if video.exists():
                digest = file_sha256(video)
                add_safety_check(
                    report,
                    "Delivery video hash",
                    "PASS" if digest == data.get("current_video_sha256") else "BLOCK",
                    f"actual={digest}; expected={data.get('current_video_sha256')}",
                )
            else:
                add_safety_check(report, "Delivery current video", "BLOCK", "missing")

    report["result"] = "blocked" if report["blockers"] else ("warn" if report["warnings"] else "pass")
    report_dir = run_dir / "run_docs" / "state"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"continue_safe_{stage}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    json_dump(report_path, report)
    print(f"Continue-safe result: {report['result']}")
    print(f"Report: {report_path}")
    if report["blockers"]:
        fail("Continue-safe blocked:\n- " + "\n- ".join(report["blockers"]))


def check(_: argparse.Namespace) -> None:
    for tool in ["ffmpeg", "ffprobe", "whisper"]:
        path = shutil.which(tool)
        if not path and tool == "whisper":
            local_whisper = ROOT / ".venv" / "bin" / "whisper"
            if local_whisper.exists():
                path = str(local_whisper)
        print(f"{tool}: {path or 'missing'}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit(1)


def check_opencli(_: argparse.Namespace) -> None:
    opencli = has_tool("opencli")
    print(f"opencli: {opencli or 'missing'}")
    if not opencli:
        fail("OpenCLI is missing. Install with: npm_config_cache=\"$PWD/.npm-cache\" npm install -g @jackwener/opencli")

    version = run_command(["opencli", "--version"])
    print(version.stdout.strip() or version.stderr.strip())

    doctor = run_command(["opencli", "doctor"])
    doctor_output = doctor.stdout.strip()
    print(doctor_output)
    if doctor.returncode != 0 or "[FAIL]" in doctor_output or "[MISSING] Extension" in doctor_output:
        print(doctor.stderr.strip(), file=sys.stderr)
        fail(
            "OpenCLI Browser Bridge is not ready. Install the Chrome extension from "
            "tools/opencli/opencli-extension-v1.0.15, then log into jimeng.jianying.com in Chrome."
        )


def jimeng_opencli_history(args: argparse.Namespace) -> None:
    if not has_tool("opencli"):
        fail("OpenCLI is missing. Run `python3 scripts/ai_video_trial.py check-opencli` first.")
    result = run_command(
        [
            "opencli",
            "jimeng",
            "history",
            "--limit",
            str(args.limit),
            "-f",
            args.format,
            "--site-session",
            "persistent",
            "--window",
            args.window,
        ]
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        fail("OpenCLI Jimeng history failed. Check Chrome login, extension connection, and site permissions.")


def open_jimeng_video_page(args: argparse.Namespace) -> None:
    if not has_tool("opencli"):
        fail("OpenCLI is missing. Run `python3 scripts/ai_video_trial.py check-opencli` first.")
    url = "https://jimeng.jianying.com/ai-tool/generate?type=video&workspace=0"
    result = run_command(["opencli", "browser", "jimeng-video", "--window", args.window, "open", url])
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        fail("Could not open Jimeng video page through OpenCLI.")
    print("Next: run `opencli browser jimeng-video state` to inspect controls, or keep the tab open for adapter authoring.")


def transcribe(args: argparse.Namespace) -> None:
    transcript_dir = transcribe_audio(
        Path(args.run_dir),
        args.model,
        args.language,
        args.output_format,
    )
    print(f"Created Whisper transcript: {transcript_dir}")
    print("Next: correct domain terms/brand words, then use the corrected transcript as the prompt dialogue anchor.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI video automation trial runner")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Check local video dependencies")
    p_check.set_defaults(func=check)

    p_check_opencli = sub.add_parser("check-opencli", help="Check OpenCLI + Browser Bridge for Jimeng browser automation")
    p_check_opencli.set_defaults(func=check_opencli)

    p_history = sub.add_parser("jimeng-opencli-history", help="Smoke-test Jimeng OpenCLI connection by reading generation history")
    p_history.add_argument("--limit", type=int, default=5)
    p_history.add_argument("--format", default="table", choices=["table", "plain", "json", "yaml", "md", "csv"])
    p_history.add_argument("--window", default="background", choices=["foreground", "background"])
    p_history.set_defaults(func=jimeng_opencli_history)

    p_open_video = sub.add_parser("open-jimeng-video", help="Open Jimeng video generation page through OpenCLI Browser Bridge")
    p_open_video.add_argument("--window", default="foreground", choices=["foreground", "background"])
    p_open_video.set_defaults(func=open_jimeng_video_page)

    p_prepare = sub.add_parser("prepare", help="Create a gated trial package from a source video")
    p_prepare.add_argument("--video", required=True, help="Path to source video")
    p_prepare.add_argument("--run-id", help="Optional run id under data/runs")
    p_prepare.add_argument("--segment-seconds", type=int, default=15)
    p_prepare.add_argument("--frame-interval", type=int, default=2)
    p_prepare.add_argument("--transcribe", action="store_true", help="Run local Whisper after audio extraction")
    p_prepare.add_argument("--whisper-model", default="small", help="Whisper model for --transcribe")
    p_prepare.add_argument("--whisper-language", default="Chinese", help="Whisper language for --transcribe")
    p_prepare.add_argument("--whisper-output-format", default="all", help="Whisper output format for --transcribe")
    p_prepare.add_argument("--final-video-name", help="Activate Name Gate with the user's intended final video name")
    p_prepare.add_argument("--jimeng-dialogue", help="Activate Name Gate with the per-video Jimeng dialogue name")
    p_prepare.add_argument("--workspace-url", help="Activate Name Gate with the Jimeng workspace URL")
    p_prepare.add_argument("--binder", default="user", help="Who confirmed the Name Gate identity")
    p_prepare.add_argument("--force", action="store_true")
    p_prepare.set_defaults(func=prepare)

    p_new_run = sub.add_parser("new-video-run", help="Create a new video run with mandatory Name Gate binding")
    p_new_run.add_argument("--video", required=True, help="Path to source video")
    p_new_run.add_argument("--final-video-name", required=True, help="User's intended final delivery name")
    p_new_run.add_argument("--jimeng-dialogue", required=True, help="Per-video Jimeng dialogue name to create/bind")
    p_new_run.add_argument("--workspace-url", required=True, help="Jimeng workspace URL")
    p_new_run.add_argument("--run-id", help="Optional run id under data/runs")
    p_new_run.add_argument("--segment-seconds", type=int, default=15)
    p_new_run.add_argument("--frame-interval", type=int, default=2)
    p_new_run.add_argument("--transcribe", action="store_true", help="Run local Whisper after audio extraction")
    p_new_run.add_argument("--whisper-model", default="small")
    p_new_run.add_argument("--whisper-language", default="Chinese")
    p_new_run.add_argument("--whisper-output-format", default="all")
    p_new_run.add_argument("--binder", default="user")
    p_new_run.add_argument("--force", action="store_true")
    p_new_run.set_defaults(func=new_video_run)

    p_transcribe = sub.add_parser("transcribe", help="Run local Whisper on an existing prepared run")
    p_transcribe.add_argument("--run-dir", required=True)
    p_transcribe.add_argument("--model", default="small")
    p_transcribe.add_argument("--language", default="Chinese")
    p_transcribe.add_argument("--output-format", default="all")
    p_transcribe.set_defaults(func=transcribe)

    p_prompts = sub.add_parser("generate-prompts", help="Generate Jimeng prompts after creative requirements are confirmed")
    p_prompts.add_argument("--run-dir", required=True)
    p_prompts.set_defaults(func=generate_prompts)

    p_review = sub.add_parser("review-segment", help="Review a returned generated AI video segment")
    p_review.add_argument("--run-dir", required=True)
    p_review.add_argument("--segment", type=int, required=True)
    p_review.add_argument("--video", required=True)
    p_review.add_argument("--decision", choices=["可进入下一段", "建议重跑", "必须重跑"], help="Optional final decision")
    p_review.add_argument("--frame-interval", type=int, default=1)
    p_review.set_defaults(func=review_segment)

    p_review_decision = sub.add_parser("record-review-decision", help="Record the user's review decision for a generated segment")
    p_review_decision.add_argument("--run-dir", required=True)
    p_review_decision.add_argument("--segment", type=int, required=True)
    p_review_decision.add_argument("--decision", required=True, choices=["可进入下一段", "建议重跑", "必须重跑"])
    p_review_decision.add_argument("--reviewer", default="user")
    p_review_decision.add_argument("--note", help="Optional review note")
    p_review_decision.set_defaults(func=record_review_decision)

    p_verify_dialogue = sub.add_parser("verify-jimeng-dialogue", help="Verify current Jimeng page matches the required dialogue identity")
    p_verify_dialogue.add_argument("--run-dir", required=True)
    p_verify_dialogue.add_argument("--session", default="jimeng-video")
    p_verify_dialogue.add_argument("--name", help="Override required dialogue name. Defaults to gate_status.json jimeng_dialogue.required_name.")
    p_verify_dialogue.add_argument("--workspace-url", help="Override expected Jimeng workspace URL")
    p_verify_dialogue.add_argument("--strict", action="store_true", help="Exit non-zero when verification is blocked")
    p_verify_dialogue.set_defaults(func=verify_jimeng_dialogue)

    p_download = sub.add_parser("download-segment", help="Download the current Jimeng result video into returned/SegmentXX_takeX.mp4")
    p_download.add_argument("--run-dir", required=True)
    p_download.add_argument("--segment", type=int, required=True)
    p_download.add_argument("--take", default="A", help="Take suffix, e.g. A, B, or takeB")
    p_download.add_argument("--session", default="jimeng-seg2", help="OpenCLI browser session to inspect when --url is omitted")
    p_download.add_argument("--url", help="Direct mp4 URL. If omitted, read the last <video> src from the OpenCLI session.")
    p_download.add_argument("--output", help="Optional output mp4 path")
    p_download.set_defaults(func=download_segment)

    p_edit = sub.add_parser("edit-trial", help="Create a rough cut, overlay logo, extract QC frames, and write an edit report")
    p_edit.add_argument("--run-dir", required=True)
    p_edit.add_argument("--segments", required=True, help="Comma-separated segment numbers, e.g. 1,2")
    p_edit.add_argument("--videos", help="Optional comma-separated video paths matching --segments. Defaults to latest returned files.")
    p_edit.add_argument("--logo", required=True, help="Logo image path")
    p_edit.add_argument("--logo-x", type=int, default=56)
    p_edit.add_argument("--logo-y", type=int, default=56)
    p_edit.add_argument("--logo-width", type=int, default=170)
    p_edit.add_argument("--frame-interval", type=int, default=5)
    p_edit.add_argument("--crf", type=int, default=18)
    p_edit.add_argument("--name", default="review_cut", help="Output filename prefix under edit/")
    p_edit.set_defaults(func=edit_trial)

    p_tail = sub.add_parser("extract-tail-frame", help="Extract the accepted segment tail frame for next-segment continuity")
    p_tail.add_argument("--run-dir", required=True)
    p_tail.add_argument("--segment", type=int, required=True)
    p_tail.add_argument("--video", help="Optional segment video path. Defaults to latest returned SegmentXX mp4.")
    p_tail.add_argument("--offset-seconds", type=float, default=0.25, help="Capture this many seconds before the end")
    p_tail.set_defaults(func=extract_tail_frame)

    p_final = sub.add_parser("final-export", help="Create a final reviewed export from accepted segment videos")
    p_final.add_argument("--run-dir", required=True)
    p_final.add_argument("--segments", help="Comma-separated segment numbers. Defaults to all gate_status segments.")
    p_final.add_argument("--videos", help="Optional comma-separated video paths matching --segments. Defaults to latest returned files.")
    p_final.add_argument("--logo", required=True, help="Logo image path")
    p_final.add_argument("--logo-x", type=int, default=56)
    p_final.add_argument("--logo-y", type=int, default=56)
    p_final.add_argument("--logo-width", type=int, default=170)
    p_final.add_argument("--frame-interval", type=int, default=5)
    p_final.add_argument("--crf", type=int, default=18)
    p_final.add_argument("--name", default="final_cut", help="Output filename prefix under edit/")
    p_final.add_argument("--allow-unreviewed", action="store_true", help="Allow export even when selected segments are not reviewed")
    p_final.set_defaults(func=final_export)

    p_gate = sub.add_parser("gate-status", help="Print the current gated workflow state")
    p_gate.add_argument("--run-dir", required=True)
    p_gate.set_defaults(func=gate_status)

    p_reconcile = sub.add_parser("reconcile-state", help="Check and optionally fix gate_status against real files")
    p_reconcile.add_argument("--run-dir", required=True)
    p_reconcile.add_argument("--accept-segments", default="", help="Comma-separated segments to mark reviewed for edit/package, e.g. 5")
    p_reconcile.add_argument("--decision", help="Decision text to write for accepted segments")
    p_reconcile.add_argument("--reviewer", default="user")
    p_reconcile.add_argument("--note", help="Optional reconciliation note")
    p_reconcile.add_argument("--write", action="store_true", help="Apply changes. Without this, only writes a dry-run report.")
    p_reconcile.set_defaults(func=reconcile_state)

    p_package = sub.add_parser("package-delivery", help="Create a safe delivery package under the Codex archive folder")
    p_package.add_argument("--run-dir", required=True)
    p_package.add_argument("--destination-root", default="/Users/yangyi/Documents/Codex/Advertising Automation-Assest section")
    p_package.add_argument("--name", help="Package folder name. Defaults to run id.")
    p_package.add_argument("--current-video-name", default="当前智能剪辑_v1.mp4")
    p_package.set_defaults(func=package_delivery)

    p_bind_name = sub.add_parser("bind-name-gate", help="Bind per-video final name and Jimeng dialogue identity")
    p_bind_name.add_argument("--run-dir", required=True)
    p_bind_name.add_argument("--final-video-name", required=True)
    p_bind_name.add_argument("--jimeng-dialogue", required=True)
    p_bind_name.add_argument("--workspace-url", required=True)
    p_bind_name.add_argument("--binder", default="user")
    p_bind_name.add_argument("--force", action="store_true", help="Allow rebinding after explicit user confirmation")
    p_bind_name.set_defaults(func=bind_name_gate)

    p_continue_safe = sub.add_parser("continue-safe", help="Hard safety gate before generation, edit, package, or handoff")
    p_continue_safe.add_argument("--run-dir", required=True)
    p_continue_safe.add_argument("--stage", required=True, choices=["preflight", "generation", "edit", "package", "handoff"])
    p_continue_safe.add_argument("--segment", type=int, help="Required for preflight/generation safety checks")
    p_continue_safe.add_argument("--user-confirmed", action="store_true", help="Only set after the user explicitly confirmed the current approval package")
    p_continue_safe.add_argument("--require-browser", action="store_true", help="Require dedicated Jimeng browser verification for this safety check")
    p_continue_safe.add_argument("--destination-root", default="/Users/yangyi/Documents/Codex/Advertising Automation-Assest section")
    p_continue_safe.add_argument("--package-name", help="Delivery package folder name. Defaults to name gate final video name")
    p_continue_safe.set_defaults(func=continue_safe)

    p_approve = sub.add_parser("approve-prompt", help="Mark one or more segment prompts in prompt_approval.md")
    p_approve.add_argument("--run-dir", required=True)
    p_approve.add_argument("--segments", required=True, help="Comma-separated segment numbers, e.g. 3,4,5")
    p_approve.add_argument("--status", default="已确认", choices=["待确认", "已确认", "需修改", "废弃"])
    p_approve.add_argument("--reason", default="用户要求继续跑完当前 AI 视频自动化人物链路")
    p_approve.add_argument("--approver", default="Codex")
    p_approve.set_defaults(func=approve_prompt)

    p_preflight = sub.add_parser("preflight-jimeng", help="Write a guarded Jimeng generation preflight report for one segment")
    p_preflight.add_argument("--run-dir", required=True)
    p_preflight.add_argument("--segment", type=int, required=True)
    p_preflight.add_argument("--session", default="jimeng-video", help="OpenCLI/Jimeng browser session name used for manual verification")
    p_preflight.add_argument("--confirm-text", help="Exact user confirmation text, e.g. 确认生成 Segment 03")
    p_preflight.add_argument("--confirmation-phrase", help="Override the expected confirmation phrase")
    p_preflight.add_argument("--allow-operator-confirm", action="store_true", help="Only use after the user has explicitly approved the current preflight in chat")
    p_preflight.add_argument("--allow-rerun", action="store_true", help="Allow preflight when the current segment is marked must_rerun")
    p_preflight.add_argument("--expected-model", default="Seedance 2.0 Fast VIP")
    p_preflight.add_argument("--expected-reference", default="全能参考")
    p_preflight.add_argument("--expected-aspect", default="9:16")
    p_preflight.add_argument("--expected-duration", type=int, default=15)
    p_preflight.add_argument("--strict", action="store_true", help="Exit non-zero when blocking checks remain")
    p_preflight.set_defaults(func=preflight_jimeng)

    p_notify = sub.add_parser("notify-dingtalk", help="Send a DingTalk robot review notification")
    p_notify.add_argument("--run-dir", required=True)
    p_notify.add_argument("--segment", type=int, required=True)
    p_notify.add_argument("--stage", default="review", choices=["preflight", "review"], help="Notification stage; used in title and manifest filename")
    p_notify.add_argument("--total-segments", type=int, help="Total segment count. Defaults to segments.json length.")
    p_notify.add_argument("--review-file", help="Segment review markdown path")
    p_notify.add_argument("--prompt-file", help="Prompt text file to embed in the DingTalk markdown for copy/review")
    p_notify.add_argument("--prompt-preview-chars", type=int, default=2400, help="Max prompt characters to include in DingTalk")
    p_notify.add_argument("--screenshot-file", help="Optional current-page screenshot path to record in the notification manifest")
    p_notify.add_argument("--approval-phrase", help="Exact confirmation phrase reviewers should reply with for preflight approval")
    p_notify.add_argument("--jimeng-url", help="Jimeng backend/workspace URL for reviewers")
    p_notify.add_argument("--decision", default="待审核", help="Review status, e.g. 待审核 / 建议重跑 / 可进入下一段")
    p_notify.add_argument("--reviewer", help="Reviewer hint shown in the DingTalk message")
    p_notify.add_argument("--webhook", help="DingTalk robot webhook. Prefer DINGTALK_WEBHOOK env var.")
    p_notify.add_argument("--secret", help="DingTalk robot signing secret. Prefer DINGTALK_SECRET env var.")
    p_notify.add_argument("--at-mobiles", help="Comma-separated mobile numbers to @. Prefer DINGTALK_AT_MOBILES env var.")
    p_notify.add_argument("--at-all", action="store_true", help="Mention everyone in the DingTalk group")
    p_notify.add_argument("--dry-run", action="store_true", help="Write and print the notification without sending")
    p_notify.set_defaults(func=notify_dingtalk)

    p_continue = sub.add_parser("continue-pipeline", help="Advance the AI video pipeline to the next safe approval gate")
    p_continue.add_argument("--run-dir", required=True)
    p_continue.add_argument("--segment", type=int, help="Segment to prepare. Defaults to the first actionable segment.")
    p_continue.add_argument("--approval-phrase", help="Exact confirmation phrase for generation approval")
    p_continue.add_argument("--notify-dingtalk", action="store_true", help="Send the preflight approval card to DingTalk")
    p_continue.add_argument("--total-segments", type=int, help="Total segment count for DingTalk. Defaults to segments.json length.")
    p_continue.add_argument("--prompt-preview-chars", type=int, default=2400, help="Max prompt characters to include in DingTalk")
    p_continue.add_argument("--webhook", help="DingTalk robot webhook. Prefer DINGTALK_WEBHOOK env var.")
    p_continue.add_argument("--secret", help="DingTalk robot signing secret. Prefer DINGTALK_SECRET env var.")
    p_continue.add_argument("--at-mobiles", help="Comma-separated mobile numbers to @. Prefer DINGTALK_AT_MOBILES env var.")
    p_continue.add_argument("--at-all", action="store_true", help="Mention everyone in the DingTalk group")
    p_continue.add_argument("--dry-run", action="store_true", help="Prepare files and print DingTalk notification without sending")
    p_continue.set_defaults(func=continue_pipeline)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    os.chdir(ROOT)
    args.func(args)


if __name__ == "__main__":
    main()
