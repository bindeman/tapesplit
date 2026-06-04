# Temporal Evidence Fabric

TapeSplit should evolve into a temporal evidence fabric: an evidence-first memory
system for long, messy video collections.

The product should not depend on one model "understanding" a whole tape. It
should accumulate timestamped evidence, connect that evidence into a graph,
index it semantically, and generate narratives only from grounded claims.

## Four Layers

```text
1. Temporal evidence ledger
   Immutable observations and model outputs with source timecodes.

2. Typed evidence graph
   People, speakers, places, events, dates, narratives, and relationships.

3. Multimodal vector index
   Semantic search over transcripts, OCR, frame captions, scenes, events, and
   later images/audio/documents.

4. Relational/UI projections
   Fast tables for review, export, cost tracking, timelines, and reports.
```

Graph alone is too rigid. Vector search alone loses provenance. Event sourcing
alone is auditable but weak for search and reasoning. The hybrid gives us
temporal order, relationship structure, semantic retrieval, and explainability.

## Core Records

```text
Project / Haul
SourceAsset: original video, image, audio, or document later
MediaInterval: exact source time range
Window: fixed 15-30s unit
Scene: visual continuity segment
Event: grouped scenes/windows
Evidence: timestamped observed signal
Claim: typed assertion grounded in evidence
Entity: person, face cluster, speaker cluster, place, object, sign, event, date
Relation: typed edge between entities/claims/intervals
Narrative: generated summary with citations
Correction: user/operator override, also treated as evidence
```

## Evidence

Evidence is observed or directly extracted.

Examples:

```text
transcript: "Here we are at Roosevelt Middle School"
ocr: "ROOSEVELT MIDDLE SCHOOL"
visual: classroom with desks, chalkboard, students
visual: exterior school building
face: face_cluster_001 appears
speaker: speaker_cluster_002 speaks off camera
metadata: source file name or tape label
user: operator confirms place = Roosevelt Middle School
```

Evidence should include:

```json
{
  "id": "ev_000123",
  "source_asset_id": "video_000001",
  "start_s": 120.4,
  "end_s": 126.8,
  "modality": "ocr",
  "text": "ROOSEVELT MIDDLE SCHOOL",
  "language": "en",
  "confidence": 0.91,
  "producer": {
    "provider": "local",
    "model": "paddleocr"
  }
}
```

## Claims

Claims are interpreted assertions.

```json
{
  "id": "claim_000456",
  "subject_id": "scene_000014",
  "predicate": "likely_place",
  "value": {
    "place_id": "place_roosevelt_middle_school",
    "label": "Roosevelt Middle School",
    "place_type": "school"
  },
  "confidence": 0.74,
  "source": "inferred_from_sign_and_scene_type",
  "evidence_ids": ["ev_sign_001", "ev_classroom_002"],
  "contradicting_evidence_ids": [],
  "review_status": "unreviewed"
}
```

Do not overwrite claims when better evidence arrives. Add a new claim, link it
to the older one, and mark the older one superseded or contradicted.

## Place Reasoning

Place reasoning should use both explicit anchors and semantic containment.

Examples:

```text
OCR sign: "Roosevelt Middle School"
  -> explicit place anchor
  -> place_type = school
  -> strong evidence for exterior location

Visual scene: classroom with desks and chalkboard
  -> scene_type = classroom
  -> likely_inside_place_type = school
  -> weak/medium evidence without a named school

Later scene: gymnasium with students and school banners
  -> scene_type = gymnasium
  -> likely_inside_place_type = school
  -> possible_same_campus_as Roosevelt Middle School if adjacent or similar
```

The graph should support containment:

```text
classroom_001 INSIDE place_roosevelt_middle_school candidate
gym_001 INSIDE place_roosevelt_middle_school candidate
place_roosevelt_middle_school IS_A school
school CONTAINS classrooms, hallways, gyms, auditoriums, playgrounds
```

Use this safely:

- A classroom is likely inside a school, but could also be a church, community
  center, training room, or museum exhibit.
- An exterior sign naming a school is stronger than a generic classroom visual.
- If a named school sign appears near a classroom scene, infer
  `possible_same_place` with confidence based on temporal proximity, people,
  clothing, audio, and visual continuity.
- Do not assign exact GPS unless the place is confirmed by user input or a
  reliable geocoder plus sufficient context.

Geocoding should create a derived claim, not immediately write metadata:

```text
named_place_candidate = Maplewood Elementary School
city_candidate = Eugene
geocoder_result = Maplewood Elementary School, Eugene, OR
gps_candidate = 44.x, -123.x
confidence = high only if name + city/region disambiguate
```

If the system only sees "Maplewood Elementary School" without city/state context,
there may be many matches. Store multiple candidates and ask for review. If the
family profile or narration says Eugene, then the Eugene result becomes much
stronger.

Write GPS into exported media metadata only when:

- the user confirms it, or
- place name plus city/region/country evidence uniquely resolve with high
  confidence, and
- the export policy allows location metadata.

Until then, keep GPS as `gps_candidate`, not final location metadata.

Google Maps/geocoding must be disabled by default in local development unless
`GOOGLE_MAPS_ENABLED=true` and a positive `GOOGLE_MAPS_DAILY_BUDGET_USD` are set.
The safe default is to generate geocoding queries offline, then require an
explicit command to spend API calls.

Immigrant and multi-country family archives need a recurring-travel model. A
family may live in the US but repeatedly visit a home country in the summer.
Represent this as a pattern hypothesis, not a default assumption:

```text
family_profile:
  residence_country_candidate = US
  recurring_trip_candidate = Ukraine/Russia/Israel/etc.
  seasonality_candidate = summer
```

Evidence can include:

- spoken language shifts
- narrator mentions "back home", "visiting grandma", city/country names
- airport/plane/train footage
- currency, road signs, license plates, storefronts, school/church signs
- recurring relatives/houses that appear only during summer tape ranges
- dates or tape order showing repeated July/August travel across years

Use the pattern as a prior only:

```text
summer + Russian speech + recurring apartment + travel footage
  -> possible_home_country_trip

summer alone
  -> not enough
```

This lets TapeSplit infer "likely summer trip to family home country" while
preserving uncertainty and requiring direct evidence for exact city/country.

Useful place claim predicates:

```text
scene_type
place_type
named_place_candidate
inside_place_type_candidate
same_place_candidate
same_campus_candidate
nearby_place_candidate
place_anchor
```

## Context Anchors And Carry-Forward

When the narrator says a date/place or OCR shows a sign/date, create an anchor.

```text
direct evidence > strong inference > weak carry-forward prior
```

Carry context only with scope and decay:

```json
{
  "claim": "scene_014 likely occurs at Roosevelt Middle School",
  "source": "propagated_context",
  "anchor_evidence_id": "ev_school_sign_001",
  "supporting_evidence_ids": ["ev_classroom_002", "ev_same_people_003"],
  "confidence": 0.61,
  "validity": {
    "start_s": 1800.0,
    "end_s": 2450.0,
    "boundary_reason": "hard scene change to home interior"
  }
}
```

Carry-forward stops or decays sharply at:

- hard visual/source boundary
- blank, blue-screen, static, color-bar, or no-signal interval
- unrelated movie/TV signal
- contradictory OCR/transcript
- large time gap
- different people/clothing/environment
- tape boundary unless cross-tape evidence supports continuity

Exact dates and GPS should not propagate as hard facts. They become
`possible_date_range`, `same_event_candidate`, or `same_place_candidate` unless
independently confirmed.

Example:

```text
clip_001: narrator says "we are in Hawaii" and shows a beach
clip_002: dense ferns/tropical vegetation, no direct sign
clip_003: same people/clothes on a nearby trail

clip_002 claim:
  predicate = possible_location_context
  value = Hawaii
  source = propagated_trip_context
  confidence = medium
  not_exportable_as_gps = true
```

The system can use the Hawaii context for search, event grouping, and story
generation, but it should not write Hawaii/GPS into clip_002 metadata unless
additional evidence or review confirms it.

## People And Relationships

Separate identity layers:

```text
face_track: same visible face within a local interval
face_cluster: likely same person across intervals
speaker_cluster: likely same voice
mentioned_name: name from transcript/OCR
person: user-confirmed identity
```

Initial relationship edges should be conservative:

```text
appears_with
speaks_to_candidate
mentions
same_person_candidate
likely_camera_operator_for
same_event_candidate
family_relationship_candidate
```

Avoid asserting mother/father/sibling/spouse unless directly stated in transcript
or confirmed by a user.

## Multilingual Handling

Language belongs to transcript segments and text evidence, not people or tapes.

```json
{
  "text_original": "С днем рождения, John",
  "language": "ru",
  "translation_en": "Happy birthday, John",
  "speaker_id": "speaker_002"
}
```

Search should use original text, translations, extracted names, and embeddings.
A single speaker/person may use many languages.

## Query Planning

Natural-language queries should become structured retrieval plans.

Example:

```text
"Find John at the school after the birthday party"
```

Plan:

```text
1. Resolve John -> person/face/speaker candidates.
2. Resolve school -> place_type school OR named school places.
3. Resolve birthday party -> event candidates.
4. Apply temporal constraint after birthday event.
5. Use graph filters for person/place/event.
6. Use vector search for semantic match.
7. Rerank and cite evidence/timecodes.
```

This is where temporal + graph + vector beats plain vector search.

## MVP Storage

For the MVP, do not add Neo4j.

Use:

```text
JSONL for append-only raw records
SQLite for canonical tables soon
SQLite FTS5 for transcript/OCR search
sqlite-vec or LanceDB later for local vectors
filesystem for keyframes/thumbnails/media artifacts
```

Recommended next JSONL files:

```text
evidence.jsonl
claims.jsonl
entities.jsonl
relations.jsonl
summaries.jsonl
corrections.jsonl
non_content_ranges.jsonl
```
