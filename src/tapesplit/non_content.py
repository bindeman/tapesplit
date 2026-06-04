from __future__ import annotations

import json
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tapesplit.media import ffprobe_video
from tapesplit.storage import append_jsonl, read_jsonl


@dataclass(frozen=True)
class FrameStats:
    time_s: float
    mean_r: float
    mean_g: float
    mean_b: float
    std_r: float
    std_g: float
    std_b: float

    @property
    def mean_luma(self) -> float:
        return 0.2126 * self.mean_r + 0.7152 * self.mean_g + 0.0722 * self.mean_b

    @property
    def mean_std(self) -> float:
        return (self.std_r + self.std_g + self.std_b) / 3.0


def detect_non_content_for_project(
    project_dir: Path,
    sample_interval_s: float = 2.0,
    min_range_s: float = 4.0,
) -> dict:
    project = project_dir.expanduser().resolve()
    tapes = read_jsonl(project / "tapes.jsonl")
    output_path = project / "non_content_ranges.jsonl"
    if output_path.exists():
        output_path.unlink()

    total_ranges = 0
    for tape in tapes:
        ranges = detect_non_content_ranges(
            video_path=Path(tape["path"]),
            source_video_id=tape["id"],
            sample_interval_s=sample_interval_s,
            min_range_s=min_range_s,
        )
        for item in ranges:
            append_jsonl(output_path, item)
            total_ranges += 1

    return {
        "project": str(project),
        "ranges": total_ranges,
        "sample_interval_s": sample_interval_s,
        "min_range_s": min_range_s,
        "output": str(output_path),
    }


def detect_non_content_ranges(
    video_path: Path,
    source_video_id: str,
    sample_interval_s: float = 2.0,
    min_range_s: float = 4.0,
) -> list[dict]:
    if sample_interval_s <= 0:
        raise ValueError("sample_interval_s must be greater than 0")

    probe = ffprobe_video(video_path)
    duration_s = float(probe.get("duration_s") or 0.0)
    if duration_s <= 0:
        return []

    stats = _sample_frame_stats(video_path, duration_s, sample_interval_s)
    classified = [
        {
            "time_s": stat.time_s,
            "label": classify_frame(stat),
            "stats": stat,
        }
        for stat in stats
    ]
    return _merge_classified_samples(
        source_video_id=source_video_id,
        classified=classified,
        sample_interval_s=sample_interval_s,
        duration_s=duration_s,
        min_range_s=min_range_s,
    )


def classify_frame(stats: FrameStats) -> str | None:
    if _is_black(stats):
        return "blank_black"
    if _is_blue_screen(stats):
        return "blue_screen_no_signal"
    if _is_static_like(stats):
        return "static_or_noise"
    return None


def _is_black(stats: FrameStats) -> bool:
    return stats.mean_luma < 18 and stats.mean_std < 12


def _is_blue_screen(stats: FrameStats) -> bool:
    return (
        stats.mean_b > 80
        and stats.mean_b > stats.mean_r * 1.35
        and stats.mean_b > stats.mean_g * 1.15
        and stats.mean_std < 28
    )


def _is_static_like(stats: FrameStats) -> bool:
    return stats.mean_std > 62 and 45 < stats.mean_luma < 210


def _sample_frame_stats(video_path: Path, duration_s: float, sample_interval_s: float) -> list[FrameStats]:
    sample_count = max(1, math.ceil(duration_s / sample_interval_s))
    stats = []
    with tempfile.TemporaryDirectory(prefix="tapesplit_frames_") as tmp:
        tmp_path = Path(tmp)
        for index in range(sample_count):
            time_s = min(duration_s, index * sample_interval_s)
            frame_path = tmp_path / f"frame_{index:06d}.rgb"
            _extract_raw_frame(video_path, time_s, frame_path)
            stats.append(_read_raw_frame_stats(frame_path, time_s))
    return stats


def _extract_raw_frame(video_path: Path, time_s: float, output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{time_s:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=32:32",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        str(output_path),
        "-y",
    ]
    subprocess.run(cmd, check=True)


def _read_raw_frame_stats(path: Path, time_s: float) -> FrameStats:
    data = path.read_bytes()
    if not data:
        return FrameStats(time_s, 0, 0, 0, 0, 0, 0)
    pixels = [data[i : i + 3] for i in range(0, len(data), 3)]
    channels = list(zip(*pixels))
    means = [sum(channel) / len(channel) for channel in channels]
    stds = [
        math.sqrt(sum((value - mean) ** 2 for value in channel) / len(channel))
        for channel, mean in zip(channels, means)
    ]
    return FrameStats(
        time_s=round(time_s, 3),
        mean_r=round(means[0], 3),
        mean_g=round(means[1], 3),
        mean_b=round(means[2], 3),
        std_r=round(stds[0], 3),
        std_g=round(stds[1], 3),
        std_b=round(stds[2], 3),
    )


def _merge_classified_samples(
    *,
    source_video_id: str,
    classified: list[dict],
    sample_interval_s: float,
    duration_s: float,
    min_range_s: float,
) -> list[dict]:
    ranges = []
    current_label = None
    current_start = None
    current_stats = []

    def flush(end_s: float) -> None:
        nonlocal current_label, current_start, current_stats
        if current_label is None or current_start is None:
            return
        duration = end_s - current_start
        if duration >= min_range_s:
            ranges.append(
                {
                    "source_video_id": source_video_id,
                    "start_s": round(current_start, 3),
                    "end_s": round(min(end_s, duration_s), 3),
                    "duration_s": round(min(end_s, duration_s) - current_start, 3),
                    "label": current_label,
                    "confidence": _range_confidence(current_stats),
                    "sample_count": len(current_stats),
                    "method": "ffmpeg_frame_color_stats",
                }
            )
        current_label = None
        current_start = None
        current_stats = []

    for sample in classified:
        label = sample["label"]
        time_s = sample["time_s"]
        if label is None:
            flush(time_s)
            continue
        if label != current_label:
            flush(time_s)
            current_label = label
            current_start = time_s
            current_stats = [sample["stats"]]
        else:
            current_stats.append(sample["stats"])

    flush(duration_s)
    return ranges


def _range_confidence(stats: list[FrameStats]) -> float:
    if not stats:
        return 0.0
    avg_std = sum(item.mean_std for item in stats) / len(stats)
    confidence = 0.95 if avg_std < 15 else 0.75
    return round(confidence, 3)

