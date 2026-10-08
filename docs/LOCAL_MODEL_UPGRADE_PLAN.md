# Local Model Upgrade Plan

tapesplit should treat every model as an adapter that writes grounded evidence
into stable project artifacts. The UI, graph, search, and review system should
not care whether evidence came from Apple Vision, OpenCV, ArcFace, Gemini,
Whisper, CLIP, or a server GPU model.

## Component Map

| Component | Local Mac default | Server / Windows / Linux fallback | Artifact |
| --- | --- | --- | --- |
| Face detection | Apple Vision | OpenCV, RetinaFace later | `face_observations.jsonl` |
| Face identity embeddings | InsightFace/ArcFace via ONNX Runtime CoreML provider | InsightFace/ArcFace via CUDA/CPU | `face_clusters.jsonl` |
| OCR / signs / burned dates | Apple Vision text recognition | PaddleOCR or Tesseract | `visual_text_observations.jsonl` |
| Visual embeddings | SentenceTransformers CLIP image model | CLIP/SigLIP/DINO on CUDA | `visual_embeddings.jsonl` |
| Transcription | whisper.cpp large-v3-turbo | Whisper/WhisperX/faster-whisper | `transcript_segments.jsonl` |
| Speaker diarization | planned local diarization adapter | pyannote/WhisperX/server diarization | `speaker_segments.jsonl` |
| Keyframe captions | local VLM later | Gemini/TwelveLabs/frontier VLM | `visual_captions.jsonl` |
| VHS/non-family classifier | color/noise heuristics now | trained VHS-state classifier later | `non_content_ranges.jsonl`, `content_classifications.jsonl` |
| Entity resolution | alias/context graph scorer | LLM-assisted resolver | `people_groups.jsonl`, `relationship_candidates.jsonl` |

## Priority Order

1. Apple Vision OCR for burned timestamps, signs, school names, banners, and
   visible dates.
2. Visual frame embeddings for same-place/same-event/same-era retrieval.
3. Better local transcription defaults and transcript quality metadata.
4. Speaker diarization and speaker/person linking.
5. Local VLM captioning for keyframes and sampled moments.
6. Learned content classifier for unrelated movies, TV footage, static, and
   camcorder dead air.
7. Evidence-weighted entity and relationship resolver that consumes all of the
   above.

## Evidence Rules

- Keep raw model outputs separate from final claims.
- Store model name, backend, confidence, source asset, and timestamp on every
  generated row.
- Prefer automatic best guesses in the UI, but expose alternates and evidence
  provenance.
- Never turn OCR, captions, faces, or speaker clusters into final people/place
  facts without either direct evidence, strong multi-signal agreement, or review.

## Immediate Implementation

The current implementation adds:

```text
tapesplit detect-text /path/to/project.tapesplit --backend auto
tapesplit caption-visuals /path/to/project.tapesplit --backend auto
tapesplit embed-visuals /path/to/project.tapesplit --backend auto
tapesplit build-visual-similarity /path/to/project.tapesplit
tapesplit classify-content /path/to/project.tapesplit
tapesplit speakers import /path/to/project.tapesplit speakers.rttm --source-video-id video_000001
```

`detect-text` runs OCR over `visual_assets.jsonl` keyframes. On macOS, `auto`
uses Apple Vision.

`caption-visuals` runs a local image-caption model over keyframes and writes
`visual_captions.jsonl`. The default local model is BLIP because it is practical
on a laptop; Ollama/LLaVA/Qwen-style backends can replace it later without
changing the artifact contract.

`embed-visuals` writes normalized image vectors for each visual asset. These are
not final labels; they are reusable similarity evidence for same-place,
same-event, and same-era grouping. `build-visual-similarity` turns those vectors
into candidate edges for review and search.

`classify-content` combines Gemini relatedness, OCR, captions, faces, and event
metadata into `content_classifications.jsonl`. High-confidence unrelated or
non-content classifications can hide ranges from customer-facing derived output,
but the raw evidence remains available.

`speakers import` creates the `speaker_segments.jsonl` contract now. `speakers
diarize` can run pyannote when its dependency and model access token are
available.

`build-evidence` and `search build` should index OCR text so signs and burned
dates become searchable and available to later reconciliation passes.

## Evaluation

For every model upgrade, track:

- new usable evidence count
- false-positive rate on unrelated or non-family footage
- review items reduced
- duplicated people/place groups reduced
- search success on known family queries
- whether continuity guesses become more accurate without over-merging

The next eval packet should include OCR/sign/date questions and same-place
retrieval questions in addition to event/person/place correctness.
