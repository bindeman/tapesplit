# Local Face Stack

## Decision

For the Mac-first MVP, use:

- face detection: Apple Vision on macOS, OpenCV Haar as the cross-platform fallback
- face embeddings: ArcFace/InsightFace as the next local identity model
- identity decisions: keep face clusters as candidates until supported by context or review

Apple Vision is the best first upgrade on an M3 Max because it is local, fast,
ships with macOS, and is usually much stronger than the current OpenCV Haar
detector for low-resolution, rotated, and imperfect VHS frames. It improves the
quality and count of face crops.

Core ML is not itself the identity model. It is the Apple runtime we can use
later to accelerate a chosen model. The identity upgrade should come from a real
face embedding model, not from the detector alone.

## Recommended Identity Model

Use an ArcFace-family model through InsightFace or ONNX Runtime as the default
local identity backend:

- Mac prototype: ONNX Runtime or a Core ML converted ArcFace model
- Linux/Windows server: InsightFace/ArcFace with ONNX Runtime CUDA
- fallback: current lightweight OpenCV grayscale feature for review-only builds

The current `opencv_equalized_gray_32` feature is intentionally weak. It can help
surface review evidence, but it is not durable identity recognition. ArcFace
embeddings should become the default for clustering the same person across tapes,
events, lighting changes, and moderate age changes.

## Pipeline Shape

1. Detect faces from event and scene keyframes.
2. Save detector metadata on every `face_observations.jsonl` row.
3. Run quality checks and discard or downgrade blurry crops.
4. Embed usable crops with ArcFace.
5. Cluster embeddings conservatively.
6. Score identity candidates with visual similarity plus event co-occurrence,
   transcript name mentions, role mentions, and relationship context.
7. Show the best guess by default in the UI, with alternates and source evidence.

## Cross-Platform Policy

Backends should stay optional:

- `--backend auto`: Apple Vision on macOS when installed, otherwise OpenCV
- `--backend apple-vision`: explicit macOS detector
- `--backend opencv`: deterministic fallback for Linux, Windows, and CI

TapeSplit should keep the JSON schemas backend-agnostic. Detector
and embedding model names should be stored as metadata so a deployment
can swap in server GPU models without changing the review UI or downstream graph.

## Next Implementation Step

The current implementation supports:

```text
tapesplit detect-faces --backend auto
tapesplit detect-faces --backend apple-vision
tapesplit detect-faces --backend opencv
tapesplit cluster-faces --embedding-backend auto
tapesplit cluster-faces --embedding-backend arcface-insightface
tapesplit cluster-faces --embedding-backend opencv-gray
```

`face_clusters.jsonl` stores the feature model and clustering threshold. When we
persist embeddings instead of recomputing them, also store vector dimensions and
embedding artifact references. This lets us evaluate whether better embeddings
reduce review burden for duplicated people such as `Ekaterina / Katya / Mom` and
translated or nicknamed names such as `Filip / Filya / Phillip`.
