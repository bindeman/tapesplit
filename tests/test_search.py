import json
from pathlib import Path

from tapesplit.search import build_search_index, query_search_index, similar_search_documents


def test_build_search_index_and_query_transcripts_events_and_albums(tmp_path: Path):
    _write_jsonl(
        tmp_path / "transcript_segments.jsonl",
        [
            {
                "id": "tr_000001",
                "source_video_id": "video_000001",
                "start_s": 10,
                "end_s": 14,
                "text": "Here we are at Maplewood School for the first day.",
                "language": "en",
                "provider": "imported",
            },
            {
                "id": "tr_000002",
                "source_video_id": "video_000001",
                "start_s": 40,
                "end_s": 44,
                "text": "The lava is flowing into the ocean.",
                "language": "en",
                "provider": "imported",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "canonical_events.jsonl",
        [
            {
                "id": "canonical_event_000001",
                "title": "Volcano Exploration",
                "start_s": 35,
                "end_s": 60,
                "summary": "Family observes steam, crater, and lava.",
                "metadata": {"event_type": "travel", "place_candidates": ["Kilauea Volcano", "Hawaii"]},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "albums.jsonl",
        [
            {
                "id": "album_000001",
                "title": "Hawaii Trip",
                "album_type": "travel",
                "canonical_event_ids": ["canonical_event_000001"],
                "place_label": "Hawaii",
                "start_s": 35,
                "end_s": 60,
            }
        ],
    )

    result = build_search_index(tmp_path)

    assert result["documents"] == 4
    assert result["by_type"]["transcript"] == 2

    school = query_search_index(tmp_path, "classroom teacher", limit=3)
    assert school["results"][0]["source_id"] == "tr_000001"
    assert school["results"][0]["record_type"] == "transcript"

    volcano = query_search_index(tmp_path, "volcano lava", limit=3)
    result_ids = {(row["record_type"], row["source_id"]) for row in volcano["results"]}
    assert ("event", "canonical_event_000001") in result_ids

    album = query_search_index(tmp_path, "vacation hawaii", limit=3)
    album_ids = {(row["record_type"], row["source_id"]) for row in album["results"]}
    assert ("album", "album_000001") in album_ids

    similar = similar_search_documents(tmp_path, "canonical_event_000001", record_type="event", limit=3)
    similar_ids = {(row["record_type"], row["source_id"]) for row in similar["results"]}
    assert similar["anchor"]["source_id"] == "canonical_event_000001"
    assert ("album", "album_000001") in similar_ids


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
