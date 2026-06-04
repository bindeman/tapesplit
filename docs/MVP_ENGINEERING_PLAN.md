# TapeSplit MVP Engineering Plan

## Objective

Build a working cloud-hybrid MVP this week that can process a folder of family
video clips or tape transfers, split and index them, identify likely family vs
unrelated footage, infer events/dates/people/context with evidence, and generate
a static review report plus machine-readable exports.

The MVP should prove the product thesis:

> Existing digitizers already create raw video files. TapeSplit turns those files
> into searchable, reviewable family timelines and album-ready metadata.

## Non-Goals For This Week

- No desktop app.
- No video enhancement or upscaling.
- No automatic final naming of people without user confirmation.
- No destructive writes to original videos.
- No attempt to understand an entire 100 GB haul in one model prompt.
- No support for every cloud provider at once.

## Core Product Model

Use a hierarchical model:

```text
FamilyHaul
  SourceVideo
    Scene
      Window
        Evidence
    Event
      grouped scenes/windows
  People
  Places
  Timeline
  ReviewReport
```

Definitions:

- `FamilyHaul`: a customer/family/order-level batch, often multiple tapes.
- `SourceVideo`: one digitized tape, DVD rip, or existing clip.
- `Scene`: a continuous visual segment from scene detection.
- `Window`: a fixed 20-30 second segment for search and fallback indexing.
- `Event`: one or more related scenes, such as "John's birthday party".
- `Evidence`: timestamped facts from transcript, OCR, frame captioning, faces,
  file names, tape labels, or user corrections.

## Recommended Week-One Architecture

```text
CLI
  ingest
  analyze
  build-events
  summarize
  export

Core
  media probing
  scene/window generation
  transcript ingestion
  OCR/keyframe ingestion
  frame caption ingestion
  evidence graph
  relatedness classifier
  event grouping
  timeline/story synthesis
  static review export

Adapters
  local ffmpeg
  local PySceneDetect
  local whisper.cpp or cloud transcript
  local OCR or cloud OCR
  cloud LLM for structured reasoning
  future local Ollama/Gemma backend
```

Start with Python because it is the fastest path through ffmpeg, scene detection,
OCR, embeddings, and data processing. Keep the model/provider boundary explicit
so the core VHS layer is not tied to one vendor.

For whole-tape VLM analysis, use map-reduce style chunking by default:

1. Create bounded source excerpts, initially 10-15 minute MP4 sidecars.
2. Analyze each chunk with source offset metadata.
3. Import chunk-local model timestamps only after adding the source offset.
4. Clamp or drop claims that cross known blue-screen/no-signal boundaries.
5. Synthesize the tape-level story from chunk evidence later.

Single whole-tape Gemini calls are still useful as a cheap exploratory pass, but
their timestamps are not authoritative enough to drive cuts or metadata exports
without chunk validation.

## Data Files

The MVP can use a project directory instead of a server database:

```text
.tapesplit/
  manifest.json
  tapes.jsonl
  scenes.jsonl
  windows.jsonl
  transcript_segments.jsonl
  frame_observations.jsonl
  ocr_observations.jsonl
  face_observations.jsonl
  people.jsonl
  events.jsonl
  timeline.json
  review.html
  costs.llm.jsonl
  costs.api.jsonl
  thumbnails/
  keyframes/
```

SQLite should replace JSONL once the first end-to-end path works, especially for
search and incremental reruns.

## Evidence Schema

Every model claim should be grounded in timestamped evidence.

```json
{
  "id": "ev_000123",
  "source_video_id": "tape_01",
  "start_s": 2518.4,
  "end_s": 2536.2,
  "type": "transcript|ocr|visual|face|filename|user",
  "text": "Happy birthday dear John",
  "confidence": 0.91,
  "metadata": {
    "provider": "whisper.cpp",
    "model": "large-v3-turbo"
  }
}
```

## Multilingual Transcript Handling

Family tapes can switch languages inside the same tape, clip, scene, or even
speaker. The transcript model must not assume one language per source video or
one language per person.

Transcript segment records should include:

```json
{
  "id": "tr_000123",
  "source_video_id": "tape_01",
  "start_s": 2518.4,
  "end_s": 2522.1,
  "text": "С днем рождения, John",
  "language": "ru",
  "language_confidence": 0.87,
  "translation_en": "Happy birthday, John",
  "speaker_id": "speaker_002",
  "provider": "whisper.cpp"
}
```

Rules:

- Detect language per segment or chunk, not only per tape.
- Preserve the original transcript text.
- Store English translation separately when generated.
- Allow a speaker/person to appear with multiple languages.
- Use both original text and translated text for search.
- Keep names in original form when possible, plus normalized aliases later.

For the MVP, Whisper-style transcription can run with auto language detection.
If the model struggles with mixed Russian/English, split audio into shorter
chunks and let language be detected per chunk, then use Azure/OpenAI/Gemma for
translation and entity extraction on the resulting segments.

## Event Schema

```json
{
  "id": "event_0007",
  "title": "John's birthday party",
  "type": "birthday",
  "relatedness": "likely_family",
  "date": {
    "value": "1996-06",
    "precision": "month",
    "confidence": 0.62
  },
  "people": ["person_001", "person_004"],
  "source_video_ids": ["tape_02"],
  "start_s": 2520.0,
  "end_s": 3110.0,
  "summary": "Children gather around a birthday cake in a living room.",
  "evidence_ids": ["ev_000123", "ev_000127", "ev_000141"],
  "confidence": 0.78,
  "needs_review": true
}
```

## Family vs Unrelated Footage

Do not delete unrelated footage. Classify and quarantine it.

Labels:

- `likely_family`
- `uncertain`
- `likely_unrelated`
- `blank_or_static`
- `blue_screen_no_signal`

Positive family signals:

- recurring face clusters across multiple clips or tapes
- camcorder timestamp overlays
- handheld home-video motion
- domestic settings
- spoken names, dates, birthdays, holidays, vacations
- repeated homes, rooms, voices, and people

Negative unrelated signals:

- opening or closing credits
- studio logos
- professional subtitles or captions
- cinematic aspect ratios or shot style
- continuous soundtrack or score
- many non-recurring actors
- no recurring family faces or names

Non-content signals:

- solid blue VHS/DVD no-signal screens
- solid black screens
- static/snow
- color bars
- long silence or steady hum with no visual content
- tracking transitions between recordings

These intervals should be excluded from customer-facing event exports by default,
but preserved as source intervals in metadata so the original tape timeline
remains auditable.

The first classifier can be rule-plus-LLM:

1. Compute obvious heuristics locally.
2. Ask a cloud LLM to return structured JSON using only the evidence.
3. Store confidence and reasons.

## Safe Context Carry-Forward

Family tapes often contain explicit anchor statements, such as:

> "Here we are in Kyiv on July 4th, 1995."

Later scenes may share the same environment, clothing, weather, people, or
continuous tape position. TapeSplit should use these anchors, but never as hard
truth unless there is direct evidence in the target scene.

Model context as evidence propagation:

```json
{
  "claim": "scene_014 likely occurs in Kyiv",
  "source": "propagated_context",
  "anchor_evidence_id": "ev_000812",
  "supporting_evidence_ids": ["ev_000845", "ev_000852"],
  "confidence": 0.64,
  "validity": {
    "start_s": 1800.0,
    "end_s": 2450.0,
    "boundary_reason": "scene change to different indoor location"
  }
}
```

Carry-forward rules:

- Treat direct transcript/OCR dates and places as anchors.
- Propagate anchors only within a bounded tape range or event cluster.
- Decay confidence with time, hard scene changes, unrelated-content signals, and
  contradictory evidence.
- Stop or sharply decay propagation across blank, blue-screen, static, color-bar,
  or no-signal intervals.
- Increase confidence when faces, clothing, location appearance, background
  audio, or transcript references match the anchor segment.
- Store propagated values with lower precision when appropriate, such as
  `1995-07-04` direct anchor vs `around 1995-07-04` propagated range.
- Always keep `source = propagated_context` separate from `source = direct_ocr`
  or `source = direct_transcript`.
- Surface propagated claims in review UI as "likely carried forward from..."
  rather than confirmed metadata.

This is especially important for dates and locations. A nearby scene can inherit
"same trip/event" more safely than it can inherit an exact date or GPS location.

Family profile context can include recurring travel patterns, such as immigrant
families living in the US but visiting a home country during summer. Treat this
as a weak prior only. Combine season/date, language shifts, travel footage,
signs, storefronts, currency, relatives, and repeated places before creating a
`possible_home_country_trip` claim.

Trip context works the same way. If one clip directly anchors "Hawaii" at a
beach, adjacent clips showing tropical vegetation or the same people can inherit
`possible_location_context = Hawaii` for grouping/search/story, but not exact
GPS or final location metadata without more evidence or user confirmation.

## Model Routing

Use a hybrid model strategy:

```text
Local deterministic:
  ffmpeg
  PySceneDetect
  frame extraction
  audio extraction

Local preferred:
  whisper.cpp for transcript
  Azure OpenAI whisper fallback/benchmark for hard multilingual tapes
  OCR
  embeddings
  face clustering

Cloud optional:
  VLM frame captioning
  video indexing
  final event/timeline reasoning

Future local:
  Ollama/Gemma for captions and structured reasoning
```

For the MVP, it is acceptable to use a frontier LLM for event synthesis and tape
story generation. Send compact evidence, not raw video.

## Cost Tracking

Every paid LLM or video-intelligence call should emit a usage record. The MVP
tracks LLM usage in `costs.llm.jsonl`:

```json
{
  "created_at": "2026-06-04T00:00:00+00:00",
  "provider": "azure_openai",
  "deployment": "gpt-5.5",
  "operation": "timeline_synthesis",
  "input_tokens": 12000,
  "output_tokens": 1800,
  "cached_input_tokens": 0,
  "estimated_cost_usd": null,
  "request_id": "..."
}
```

Leave `estimated_cost_usd` as `null` until deployment-specific pricing is
configured. Token counts still let us reconcile against provider dashboards.

To estimate dollars locally, copy `cost_rates.example.json` to
`cost_rates.json` and fill in the per-million-token rates for each deployment.
`cost_rates.json` is ignored by git because rates may be account-specific.

The example file includes public TwelveLabs-style units such as per-minute video
indexing and per-1,000 search requests. Azure estimates explicitly account for
`cached_input_tokens` when `cached_input_per_1m` is configured.

## Semantic Search

Index below the event level. Storage is small compared with source videos.

Recommended search units:

- transcript chunks
- 20-30 second windows
- scenes
- events

Search fields:

- transcript text
- OCR text
- frame captions
- event summaries
- people cluster IDs
- relatedness
- date guesses

Initial implementation can be text search plus embeddings later. The first
useful search is often transcript search.

## One-Week Build Plan

### Day 1: Project Skeleton And Ingest

- Create CLI with `ingest`, `analyze`, `export`.
- Probe videos with ffmpeg/ffprobe.
- Create `.tapesplit/manifest.json`.
- Generate source video records.
- Create fixed 30 second windows.

Deliverable:

- Running CLI that inventories a folder and creates JSONL metadata.

### Day 2: Scene Detection And Transcript

- Add PySceneDetect scene detection.
- Extract audio with ffmpeg.
- Integrate transcript path:
  - preferred: local whisper.cpp
  - fallback: accept externally generated transcript JSON/SRT/VTT
- Align transcript segments to source video time.

Deliverable:

- `scenes.jsonl`, `windows.jsonl`, `transcript_segments.jsonl`.

### Day 3: Keyframes, OCR, Frame Captions

- Extract keyframes for scenes/windows.
- Crop likely timestamp zones.
- Run OCR or allow OCR adapter stub.
- Add frame-caption adapter:
  - cloud VLM first
  - local model later

Deliverable:

- timestamped OCR and visual observations.

### Day 4: People Clusters And Relatedness

- Add face detection/embedding as optional adapter.
- Emit `person_001`, `person_002` style clusters.
- Build first relatedness classifier for family vs unrelated footage.

Deliverable:

- `people.jsonl`, relatedness score per scene/window/event.

### Day 5: Event Builder

- Merge scenes into events using:
  - time proximity
  - transcript similarity
  - frame caption similarity
  - repeated face clusters
  - OCR/date continuity
- Generate tentative event titles and types.

Deliverable:

- `events.jsonl` with evidence IDs and confidence.

### Day 6: Timeline And Review Report

- Use a frontier LLM for structured event/timeline reasoning.
- Generate:
  - `timeline.json`
  - `tape_story.md`
  - `review.html`
- Include evidence links and timestamps.

Deliverable:

- A static report usable on the family haul.

### Day 7: Run On Real Footage And Tune

- Process the user's family clips.
- Identify bad assumptions.
- Tune thresholds.
- Add missing adapter stubs.
- Create sample export package.

Deliverable:

- Demo output showing family events, unrelated quarantine, transcript search,
  tentative people clusters, and a tape/haul story.

## CLI Sketch

```bash
tapesplit ingest ./family-haul --out ./family-haul.tapesplit
tapesplit analyze ./family-haul.tapesplit --backend hybrid
tapesplit summarize ./family-haul.tapesplit
tapesplit export ./family-haul.tapesplit --format report,json,csv,folders
```

## Technical Risks

- VHS OCR may be unreliable.
- Face clustering can fail on low-quality or cross-year childhood footage.
- Family vs movie classification will need review.
- Cloud upload speed may be painful for 20-100 GB hauls.
- LLM summaries can hallucinate unless grounded in evidence.
- Video formats from digitizers will be inconsistent.

Mitigations:

- Store confidence and evidence for every claim.
- Mark uncertain outputs for review.
- Keep originals untouched.
- Prefer transcript and OCR when available.
- Use map-reduce summaries instead of huge prompts.
- Keep provider adapters interchangeable.
