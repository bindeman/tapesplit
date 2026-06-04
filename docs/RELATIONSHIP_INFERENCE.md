# Relationship Inference And Family Graph

TapeSplit should infer family and social relationships as reviewable candidates,
not final truth. The goal is to help a user or digitizer quickly build a family
graph from messy tapes while preserving evidence, uncertainty, and correction
history.

This is harder than alias matching because the system must distinguish:

- who is visible
- who is speaking
- who is being addressed
- who is being mentioned
- who is the subject of the event
- what relationship is claimed, implied, or only guessed

## Product Rule

Relationships are candidates until reviewed.

Safe candidates:

- `mother_candidate`
- `father_candidate`
- `parent_or_guardian_candidate`
- `grandparent_candidate`
- `maternal_grandparent_candidate`
- `paternal_grandparent_candidate`
- `sibling_candidate`
- `spouse_or_partner_candidate`
- `teacher_or_caretaker_candidate`
- `friend_or_classmate_candidate`
- `household_member_candidate`
- `family_friend_candidate`

Avoid final labels like `married`, `divorced`, `dating`, or `separated` unless
there is explicit speech or user confirmation. Tone can support
`interaction_style`, but should not create private-life claims by itself.

## Evidence Ladder

Use a confidence ladder instead of one model answer.

### Strong Evidence

Direct speech or visible labels:

```text
This is Ekaterina, Philip's mom.
Say hi to Grandma Galina.
Pavel is your dad.
This is my husband.
After the divorce...
```

These can create high-confidence candidates, still reviewable before export.

### Medium Evidence

Repeated contextual behavior:

```text
Child repeatedly calls Ekaterina "mama."
Ekaterina responds to that child across many clips.
The same adult films school, birthday, and medical moments for the same child.
The same older adult is repeatedly called "babushka" in family settings.
```

These support scoped relationship candidates, especially when they recur across
events and tapes.

### Weak Evidence

Tone, body language, age, familiarity, or domestic setting:

```text
adult speaks warmly to child
adult gives instructions
two adults appear familiar
person is present at a family holiday
```

These should become role or interaction-style evidence, not final relationship
claims.

## Gemini's Role

Gemini or another frontier model can propose relationship candidates from
compact evidence packets:

- transcript snippets
- translated snippets
- role assignments
- event summaries
- visible-person notes
- face/speaker cluster references
- previous confirmed family graph facts

Prompt shape:

```text
Given only the cited evidence, propose relationship candidates.
Return JSON.
Do not infer marriage, divorce, dating, or parenthood from tone alone.
Separate visible people from mentioned people.
Mark every claim with evidence ids and confidence.
Prefer "unknown" over guessing.
```

Gemini can also propose `interaction_style`:

```json
{
  "subject_entity_id": "person_ekaterina_candidate",
  "object_entity_id": "person_philip_candidate",
  "predicate": "interaction_style",
  "value": "caregiving / directive / affectionate",
  "confidence": 0.64,
  "evidence_ids": ["tr_00123", "role_00044"],
  "review_status": "unreviewed"
}
```

Do not convert that into `mother` without stronger evidence.

## Data Model

Add these project artifacts:

```text
.tapesplit/
  relationship_candidates.jsonl
  entity_face_links.jsonl
  face_observations.jsonl
  face_tracks.jsonl
  face_clusters.jsonl
  speaker_clusters.jsonl
  entity_thumbnails.jsonl
  corrections.jsonl
  thumbnails/
    faces/
    entities/
    events/
  embeddings/
    faces/
```

Relationship candidate:

```json
{
  "id": "relationship_candidate_000001",
  "subject_entity_id": "entity_person_ekaterina",
  "predicate": "mother_candidate",
  "object_entity_id": "entity_person_philip",
  "direction": "subject_to_object",
  "scope": {
    "family_haul_id": "haul_family",
    "canonical_event_ids": ["canonical_event_000004", "canonical_event_000005"],
    "date_range": {
      "start": "2005-09-07",
      "end": "2006-02-17",
      "precision": "known_observed_range"
    }
  },
  "confidence": 0.82,
  "supporting_signals": [
    "child addresses Ekaterina as mom",
    "Ekaterina responds to child",
    "repeated caregiving context"
  ],
  "evidence_ids": ["tr_0012", "tr_0041", "role_0008"],
  "contradicting_evidence_ids": [],
  "source": "relationship_resolver",
  "review_status": "needs_review"
}
```

Derived relationship:

```json
{
  "id": "relationship_candidate_000002",
  "subject_entity_id": "entity_person_vera",
  "predicate": "maternal_grandmother_candidate",
  "object_entity_id": "entity_person_philip",
  "derived_from_relationship_ids": [
    "relationship_candidate_ekaterina_mother_of_philip",
    "relationship_candidate_vera_mother_of_ekaterina"
  ],
  "confidence": 0.66,
  "review_status": "needs_review"
}
```

## Face Association And Thumbnails

Yes, we can associate a face and thumbnail with each person candidate, but it
should be a separate candidate link.

Do not store `person = face_cluster` as a hard fact until reviewed. Store:

```text
person/entity candidate
  may link to one or more face clusters
face cluster
  may link to one or more person/entity candidates
```

Face observation:

```json
{
  "id": "face_observation_000123",
  "source_video_id": "video_000001",
  "frame_time_s": 812.44,
  "bbox": [318, 92, 442, 232],
  "quality": {
    "sharpness": 0.78,
    "frontal_score": 0.71,
    "occlusion_score": 0.12
  },
  "thumbnail_path": "thumbnails/faces/face_observation_000123.jpg",
  "embedding_ref": "embeddings/faces/face_observation_000123.npy",
  "producer": {
    "provider": "local",
    "model": "face_detector_adapter"
  }
}
```

Face cluster:

```json
{
  "id": "face_cluster_000007",
  "face_observation_ids": ["face_observation_000123", "face_observation_000188"],
  "representative_face_observation_id": "face_observation_000123",
  "representative_thumbnail_path": "thumbnails/entities/face_cluster_000007.jpg",
  "source_video_ids": ["video_000001"],
  "time_ranges": [
    {
      "source_video_id": "video_000001",
      "start_s": 744.0,
      "end_s": 900.0
    }
  ],
  "confidence": 0.86,
  "review_status": "unreviewed"
}
```

Entity-face link:

```json
{
  "id": "entity_face_link_000003",
  "entity_candidate_id": "entity_person_philip",
  "face_cluster_id": "face_cluster_000007",
  "confidence": 0.78,
  "evidence_ids": ["gem_ev_000005", "tr_0012"],
  "supporting_signals": [
    "name tag says Philip Bindeman near visible child",
    "adult addresses child as Filip during same interval",
    "face cluster appears across child-centered events"
  ],
  "review_status": "needs_review"
}
```

Entity thumbnail:

```json
{
  "id": "entity_thumbnail_000001",
  "entity_candidate_id": "entity_person_philip",
  "thumbnail_kind": "face_crop",
  "path": "thumbnails/entities/entity_person_philip.jpg",
  "source_face_observation_id": "face_observation_000123",
  "source_video_id": "video_000001",
  "frame_time_s": 812.44,
  "review_status": "unreviewed"
}
```

Use two thumbnail types:

- face crop: useful for people review
- context frame: useful for events, places, and ambiguous people

For children across years, keep multiple thumbnails by age/time range. Do not
force one profile photo to represent the same person at every age.

## Inference Pipeline

1. Extract transcript and translations.
2. Extract mentions and aliases.
3. Generate event and role assignments.
4. Detect faces on sampled frames and high-value intervals.
5. Track faces within local scenes.
6. Cluster faces across nearby scenes and, later, across tapes.
7. Detect or import speaker clusters.
8. Link name mentions to overlapping face/speaker clusters when evidence allows.
9. Propose relationship candidates from direct and contextual evidence.
10. Derive second-order relationships only from reviewed or strong candidates.
11. Surface a review queue.
12. Store corrections as durable evidence.

## Gap Detection

Create review tasks when:

- a relationship candidate has only weak evidence
- a person has aliases but no face cluster
- a face cluster recurs but has no name
- a voice recurs but has no speaker/person link
- a child calls someone mom/dad but the adult entity is unresolved
- a grandparent term appears without knowing which side of the family
- a claimed relationship has conflicting evidence
- two adults are inferred as partners only from tone or co-presence
- a person has multiple face clusters that may be the same person at different
  ages

Review task example:

```json
{
  "id": "review_task_000021",
  "task_type": "confirm_relationship",
  "question": "Is Ekaterina Philip's mother?",
  "candidate_ids": ["relationship_candidate_000001"],
  "evidence_ids": ["tr_0012", "role_0008"],
  "thumbnail_paths": [
    "thumbnails/entities/entity_person_ekaterina.jpg",
    "thumbnails/entities/entity_person_philip.jpg"
  ],
  "priority": "high"
}
```

## Difficulty

Relationship inference without face clustering:

- MVP difficulty: medium
- Can be done with transcripts, Gemini proposals, role assignments, and review
  tasks.
- Good enough to find direct mom/dad/grandma/teacher/friend references.

Face thumbnails:

- MVP difficulty: medium
- Detecting faces and saving representative crops is straightforward.
- Hard parts are bad VHS quality, motion blur, interlacing, profile views, and
  children aging over years.

Cross-tape face clustering:

- MVP difficulty: hard
- Needs careful thresholds, quality filtering, and human review.
- Should be optional and conservative in the MVP.

Speaker clustering:

- MVP difficulty: medium to hard
- Useful for narrator/camera-operator identity.
- Harder with camcorder noise, overlapping speech, music, and mixed languages.

Family graph inference:

- MVP difficulty: medium
- Direct relationship phrases are tractable.
- Derived relationships and partner/marital status require strict guardrails.

## Implementation Phases

### Phase 1: Transcript-Only Relationship Candidates

- Add `relationship_candidates.jsonl`.
- Extract kinship phrases from transcripts.
- Ask Gemini to propose candidates from compact evidence packets.
- Store confidence, scope, and evidence ids.
- Add review tasks for high-value candidates.

### Phase 2: Face Thumbnails

- Add local face detection adapter.
- Save face crops and context frames.
- Add `face_observations.jsonl`.
- Add `entity_thumbnails.jsonl`.
- Show thumbnails in the static review report.

### Phase 3: Face Tracks And Clusters

- Track faces inside events.
- Cluster high-quality observations.
- Add `face_tracks.jsonl` and `face_clusters.jsonl`.
- Link clusters to person candidates only when name/time evidence overlaps.

### Phase 4: Relationship Graph

- Add scoped relationship derivation.
- Infer maternal/paternal grandparent candidates only from reviewed parent links.
- Add graph views by person, event, tape, and relationship.

### Phase 5: Self-Improvement Loop

- Convert user corrections into regression cases.
- Track precision and review burden per relationship type.
- Improve prompts/rules only when they reduce repeated review errors.
- Maintain separate family-local memories so one customer's graph never affects
  another customer's private metadata.

## Export Policy

Export confirmed relationships only. Candidate relationships can power review UI
and internal search but should not be written into final metadata by default.

Allowed before review:

- "person mentioned"
- "possible parent/guardian"
- "appears with"
- "speaks to"
- "same event as"

Requires review:

- mother/father
- grandparent side
- spouse/partner/ex-spouse
- divorce/separation/dating
- legal names
- exact household membership
