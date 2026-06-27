import json
from pathlib import Path

from tapesplit.storage import read_jsonl
from tapesplit.visual_captions import caption_visual_assets_for_project
from tapesplit.visual_embeddings import build_visual_similarity_for_project, embed_visual_assets_for_project
from tapesplit.visual_text import detect_text_for_project


def test_detect_text_writes_ocr_observations_from_visual_assets(tmp_path: Path, monkeypatch):
    keyframe = tmp_path / "keyframes" / "events" / "event_1.jpg"
    keyframe.parent.mkdir(parents=True)
    keyframe.write_bytes(b"fake image")
    _write_jsonl(
        tmp_path / "visual_assets.jsonl",
        [
            {
                "id": "visual_asset_000001",
                "subject_type": "event",
                "subject_id": "event_1",
                "source_video_id": "video_000001",
                "start_s": 10,
                "end_s": 20,
                "time_s": 15,
                "keyframe_path": "keyframes/events/event_1.jpg",
                "thumbnail_path": "thumbnails/events/event_1.jpg",
            }
        ],
    )
    monkeypatch.setattr(
        "tapesplit.visual_text._resolve_text_recognition_backend",
        lambda backend, require_available=True: "apple-vision",
    )
    monkeypatch.setattr(
        "tapesplit.visual_text._detect_text_in_image",
        lambda _path, *, backend, min_confidence, languages: [
            {
                "text": "Maplewood School",
                "confidence": 0.91,
                "bbox": {"x": 10, "y": 20, "width": 100, "height": 18},
                "engine": "test_ocr",
            }
        ],
    )

    result = detect_text_for_project(tmp_path, subject_type="event", force=True)
    rows = read_jsonl(tmp_path / "visual_text_observations.jsonl")

    assert result["text_observations"] == 1
    assert rows[0]["text"] == "Maplewood School"
    assert rows[0]["source_subject_id"] == "event_1"
    assert rows[0]["text_backend"] == "apple-vision"


def test_embed_visual_assets_writes_vectors(tmp_path: Path, monkeypatch):
    keyframe = tmp_path / "keyframes" / "events" / "event_1.jpg"
    keyframe.parent.mkdir(parents=True)
    keyframe.write_bytes(b"fake image")
    _write_jsonl(
        tmp_path / "visual_assets.jsonl",
        [
            {
                "id": "visual_asset_000001",
                "subject_type": "event",
                "subject_id": "event_1",
                "source_video_id": "video_000001",
                "start_s": 10,
                "end_s": 20,
                "time_s": 15,
                "keyframe_path": "keyframes/events/event_1.jpg",
                "thumbnail_path": "thumbnails/events/event_1.jpg",
            }
        ],
    )
    monkeypatch.setattr(
        "tapesplit.visual_embeddings._resolve_visual_embedding_backend",
        lambda backend, require_available=True: "sentence-transformers",
    )

    class FakeEmbedder:
        model_name = "fake-clip"

        def embed_image(self, _image_path: Path) -> list[float]:
            return [0.1, 0.2, 0.3]

    monkeypatch.setattr(
        "tapesplit.visual_embeddings._create_visual_embedder",
        lambda backend, *, model_name: FakeEmbedder(),
    )

    result = embed_visual_assets_for_project(tmp_path, subject_type="event", force=True)
    rows = read_jsonl(tmp_path / "visual_embeddings.jsonl")

    assert result["visual_embeddings"] == 1
    assert rows[0]["dim"] == 3
    assert rows[0]["vector"] == [0.1, 0.2, 0.3]
    assert rows[0]["source_subject_id"] == "event_1"


def test_caption_visual_assets_writes_captions(tmp_path: Path, monkeypatch):
    keyframe = tmp_path / "keyframes" / "events" / "event_1.jpg"
    keyframe.parent.mkdir(parents=True)
    keyframe.write_bytes(b"fake image")
    _write_jsonl(
        tmp_path / "visual_assets.jsonl",
        [
            {
                "id": "visual_asset_000001",
                "subject_type": "event",
                "subject_id": "event_1",
                "source_video_id": "video_000001",
                "time_s": 15,
                "keyframe_path": "keyframes/events/event_1.jpg",
            }
        ],
    )
    monkeypatch.setattr(
        "tapesplit.visual_captions._resolve_visual_caption_backend",
        lambda backend, require_available=True: "transformers-blip",
    )

    class FakeCaptioner:
        model_name = "fake-captioner"

        def caption(self, _image_path: Path) -> str:
            return "a family birthday party in a living room"

    monkeypatch.setattr(
        "tapesplit.visual_captions._create_captioner",
        lambda backend, *, model_name, prompt: FakeCaptioner(),
    )

    result = caption_visual_assets_for_project(tmp_path, subject_type="event", force=True)
    rows = read_jsonl(tmp_path / "visual_captions.jsonl")

    assert result["visual_captions"] == 1
    assert rows[0]["caption"] == "a family birthday party in a living room"
    assert rows[0]["source_subject_id"] == "event_1"


def test_build_visual_similarity_writes_candidate_edges(tmp_path: Path):
    _write_jsonl(
        tmp_path / "visual_embeddings.jsonl",
        [
            {
                "id": "visual_embedding_000001",
                "visual_asset_id": "visual_asset_000001",
                "source_subject_type": "event",
                "source_subject_id": "event_1",
                "source_video_id": "video_000001",
                "time_s": 10,
                "embedding_model": "fake-clip",
                "vector": [1.0, 0.0],
            },
            {
                "id": "visual_embedding_000002",
                "visual_asset_id": "visual_asset_000002",
                "source_subject_type": "event",
                "source_subject_id": "event_2",
                "source_video_id": "video_000001",
                "time_s": 20,
                "embedding_model": "fake-clip",
                "vector": [0.99, 0.01],
            },
            {
                "id": "visual_embedding_000003",
                "visual_asset_id": "visual_asset_000003",
                "source_subject_type": "event",
                "source_subject_id": "event_3",
                "source_video_id": "video_000001",
                "time_s": 30,
                "embedding_model": "fake-clip",
                "vector": [0.0, 1.0],
            },
        ],
    )

    result = build_visual_similarity_for_project(tmp_path, min_similarity=0.9)
    rows = read_jsonl(tmp_path / "visual_similarity_edges.jsonl")

    assert result["visual_similarity_edges"] == 1
    assert rows[0]["left_source_subject_id"] == "event_1"
    assert rows[0]["right_source_subject_id"] == "event_2"
    assert rows[0]["predicate"] == "visually_similar_asset"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
