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

1. Extract keyframes with deinterlacing (`bwdif`) so 480i field-combing never
   reaches detection or embeddings.
2. Detect faces from event and scene keyframes.
3. Save detector metadata on every `face_observations.jsonl` row.
4. Score quality as a continuous weight (size, sharpness, brightness — the old
   Haar eye-count hard gate is gone; on the reference haul it false-rejected
   496 real faces and manufactured 71% of all clusters as singletons).
   Quality informs weights and confidence, never exclusion.
5. Embed every crop the embedder can read; persist embeddings once to
   `face_embeddings.npz` + `face_embeddings.jsonl` (vector dimensions, model,
   content hash, and the free same-pass attributes: det_score, age, pose).
   Re-cluster runs read persisted vectors instead of re-running the model.
6. Cluster with constrained average-linkage agglomerative clustering:
   same-keyframe faces carry a cannot-link constraint (two faces in one frame
   are different people), and low-weight faces may join clusters but never
   seed them. Cluster ids survive re-clustering via anchor succession
   (max-Jaccard overlap of member media-span anchors); a human-labeled
   cluster that splits below the floor becomes a loud review item.
7. Score identity candidates with visual similarity plus event co-occurrence,
   transcript name mentions, role mentions, and relationship context.
8. Show the best guess by default in the UI, with alternates and source evidence.

Every human face decision (label, confirm, detach, reassign, unknown) is
stamped with `payload.anchors` — `{media_id, span, bbox}` per involved face —
and replay resolves anchors by time + bbox IoU, so corrections survive both
observation-id renumbering (re-detection) and cluster-id renumbering
(re-clustering).

## Cross-Platform Policy

Backends should stay optional:

- `--backend auto`: Apple Vision on macOS when installed, otherwise OpenCV
- `--backend apple-vision`: explicit macOS detector
- `--backend opencv`: deterministic fallback for Linux, Windows, and CI

tapesplit should keep the JSON schemas backend-agnostic. Detector
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
tapesplit cluster-faces --embedding-backend cvlface-kprpe
tapesplit cluster-faces --embedding-backend opencv-gray
```

Embedding persistence is implemented: `face_embeddings.jsonl` records the
model, vector dimensions, content hash, and span/bbox anchor per observation,
with vectors in `face_embeddings.npz`. Swapping embedders is now a cheap,
measurable experiment (recompute once, sweep thresholds offline).

`cvlface-kprpe` (AdaFace ViT-B KP-RPE, WebFace12M) is the low-resolution
upgrade path — TinyFace Rank-1 76.1 vs ~72.3 for AdaFace IR101, and buffalo_l
sits well below both in the 40–100px regime this archive lives in. Weights
(~1.8GB) load from the local Hugging Face cache only; set
`TAPESPLIT_FACE_ALLOW_DOWNLOAD=1` to permit the download. buffalo_l stays the
default and fallback, and its cosine threshold (0.65) does not transfer —
recalibrate with a sweep when enabling KP-RPE.

## Avatar candidates

`export-visualization` ranks each person's best face crops into
`avatar_candidates` (top 3: `path`, `quality`, `attribution`, `source`). An
avatar is an identity assertion, so two gates apply: the crop must clear a
quality floor (track `top_frames` outrank keyframe-era observation crops;
narrow face boxes are penalized as likely profiles), and the cluster must be
confidently attributed — linked clusters always qualify, candidate clusters
need confidence ≥ 0.6 (≥ 0.45 with direct-name evidence) and a clear margin
over rivals, except when the tied rivals share name tokens (duplicate groups
of the same person). People with no qualifying crop render as monograms in
the UI; every `confirm_identity`/`label_face_cluster` review decision links a
cluster and lights the avatar up on the next export.
