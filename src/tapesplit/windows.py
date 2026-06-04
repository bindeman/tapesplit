from __future__ import annotations

import math


def make_windows(video_id: str, duration_s: float, window_seconds: float) -> list[dict]:
    if duration_s <= 0:
        return []

    count = max(1, math.ceil(duration_s / window_seconds))
    windows = []
    for index in range(count):
        start_s = round(index * window_seconds, 3)
        end_s = round(min(duration_s, (index + 1) * window_seconds), 3)
        windows.append(
            {
                "id": f"{video_id}_window_{index + 1:06d}",
                "source_video_id": video_id,
                "index": index + 1,
                "start_s": start_s,
                "end_s": end_s,
                "duration_s": round(end_s - start_s, 3),
            }
        )
    return windows

