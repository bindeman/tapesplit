# Entity Resolution And Alias Memory

TapeSplit needs a dedicated entity-resolution layer. Family tapes repeatedly
refer to the same person, place, object, school, room, trip, or event with
different names across languages, dates, speakers, and model outputs.

This is not just a cleanup step. It becomes one of the product's main sources of
memory.

## Problem

The same real-world entity can appear as many observed strings:

```text
Filip
Philip
Phillip
Филипп
Филя
Philip Bindeman
the boy
my son
```

Places and objects have the same issue:

```text
Maplewood School
Maplewood Elementary
Maplewood Elementary School
the school on Elm and 9th
his first grade school

Spencer Butte
the hill
the mountain
the hike

living room
front room
grandma's room
the apartment
```

Transcription and translation make this harder:

- Russian `Филипп` may be translated as `Philip`, `Filip`, or `Phillip`.
- A narrator may use nicknames or patronymics.
- OCR may drop punctuation or misread signs.
- A VLM may describe a place by type (`school`) while transcript names it
  directly (`Maplewood School`).
- A model may infer a label from context, such as `Hawaii Trip`, that never
  appears verbatim in the source.

## Core Principle

Do not overwrite raw mentions. Create candidate links between mentions.

```text
mentioned_name -> alias_candidate -> entity_candidate -> confirmed_entity
```

Raw model/text outputs should remain immutable evidence. Entity resolution
creates reviewable hypotheses over that evidence.

## Why This Is A Standalone System

This is a separate product problem from transcription, scene detection, or
summarization.

A model can say "Filip", "Philip", and "Филипп" probably refer to the same child,
but the application still needs a durable memory layer that can:

- preserve every raw mention exactly as observed
- merge aliases without losing language or source context
- separate visible people from spoken-about people
- separate narrator, camera operator, subject, teacher, parent, and bystander
  roles
- carry user corrections across future tapes in the same family haul
- export only reviewed identities and metadata

In practice this sits between named-entity recognition, entity linking,
cross-lingual alias matching, face clustering, speaker clustering, knowledge
graphs, and human review. It is likely a defensible part of TapeSplit because
generic video-intelligence APIs usually return labels and summaries, not a
family-specific memory system with provenance, uncertainty, and correction
history.

## Entity Layers

```text
Mention
  A raw observed string in transcript, OCR, VLM output, EXIF, filename, or user
  correction.

AliasCandidate
  A normalized cluster of mentions that may refer to the same label.

EntityCandidate
  A candidate person/place/object/event built from aliases plus temporal,
  visual, audio, and contextual evidence.

ConfirmedEntity
  A user-reviewed identity that exports to albums, filenames, EXIF/XMP/IPTC, or
  downstream customer metadata.

RoleAssignment
  A timestamped claim that an entity candidate played a role in a specific
  interval or event. This is intentionally separate from the identity.

RelationshipCandidate
  A reviewable claim between entities, such as parent_of, teacher_of,
  classmate_of, lives_in, attends_school, or camera_operator_for.
```

Examples:

```json
{
  "id": "mention_000123",
  "surface": "Филипп",
  "surface_language": "ru",
  "canonical_event_id": "canonical_event_000010",
  "source_video_id": "video_000001",
  "start_s": 3675.0,
  "end_s": 3683.0,
  "source": "local_transcript",
  "evidence_id": "tr_000882"
}
```

Role example:

```json
{
  "id": "role_assignment_000017",
  "entity_candidate_id": "entity_candidate_person_000001",
  "canonical_event_id": "canonical_event_000004",
  "source_video_id": "video_000001",
  "start_s": 744.0,
  "end_s": 900.0,
  "role": "event_subject",
  "confidence": 0.88,
  "evidence_ids": ["gem_ev_000005"],
  "review_status": "needs_review",
  "reason": "Gemini event says this is Filip's first day of first grade."
}
```

The same person can have a different role in another interval:

```json
{
  "id": "role_assignment_000018",
  "entity_candidate_id": "entity_candidate_person_000001",
  "canonical_event_id": "canonical_event_000002",
  "source_video_id": "video_000001",
  "start_s": 44.0,
  "end_s": 720.0,
  "role": "mentioned_or_context_person",
  "confidence": 0.45,
  "evidence_ids": ["gem_ev_000003"],
  "review_status": "needs_review",
  "conflicts": ["local transcript supports narrator filming his own old school"]
}
```

This prevents a person alias from accidentally turning every school event into
that person's own first day of school.

## Roles Before Narratives

For each event, generate role assignments before writing the final narrative.
The story should be a projection of grounded roles, not just a free-form model
summary.

Useful person roles:

- `narrator`
- `camera_operator`
- `speaker`
- `visible_person`
- `event_subject`
- `mentioned_person`
- `parent_or_guardian_candidate`
- `teacher_or_caretaker_candidate`
- `classmate_candidate`
- `relative_candidate`
- `unclear_person_reference`

Useful place roles:

- `explicit_location_anchor`
- `visible_place`
- `inside_place_candidate`
- `nearby_place_candidate`
- `home_base_candidate`
- `travel_destination_candidate`
- `generic_scene_type`

Useful event roles:

- `primary_event`
- `sub_event`
- `same_day_context`
- `carry_forward_context`
- `background_or_setup`
- `unrelated_or_non_family`
- `non_content`

Example distinction:

```text
Moscow, Sep 1, 2005
  narrator: adult/camera operator candidate
  event subject: narrator's visit to old school / start of academic year
  mentioned people: Lyudmila Petrovna, Zoya
  visible children: many schoolchildren, not yet linked to Filip

Maplewood School, Sep 7, 2005
  event subject: Filip/Philip starting first grade
  place anchor: Maplewood School, Elm and 9th
  evidence: spoken date, school sign, classroom, name tag
```

The first event may still be family-related, but not because it is Filip's first
day. It is likely family-related because the narrator is filming a personally
meaningful school visit.

```json
{
  "id": "entity_candidate_person_000001",
  "entity_type": "person",
  "display_label": "Filip / Philip",
  "aliases": ["Filip", "Philip", "Phillip", "Филипп"],
  "mention_ids": ["mention_000123", "mention_000201"],
  "evidence_ids": ["tr_000882", "gem_ev_000055"],
  "canonical_event_ids": ["canonical_event_000004", "canonical_event_000010"],
  "confidence": 0.83,
  "review_status": "needs_review",
  "resolution_reasons": [
    "cross_language_transliteration_match",
    "shared_full_name_candidate",
    "same_child_context",
    "same_family_archive"
  ],
  "conflicts": []
}
```

## Matching Signals

Use multiple weak signals rather than one brittle rule.

### String Signals

- exact normalized match
- casefolded match
- punctuation/diacritic stripped match
- transliteration match
- edit distance / token sort distance
- phonetic similarity
- nickname table
- full-name containment, such as `Philip Bindeman` containing `Philip`
- abbreviation or initial match, such as `Phil` for `Philip`

### Language Signals

- source language
- translated string
- transliterated string
- language-specific nickname dictionaries
- model-provided translation alternatives

For Russian/English tapes, generate comparison variants:

```text
Филипп -> filipp, filip, philip, phillip
Саша -> sasha, alexander, alex
Володя -> volodya, vladimir
Катя -> katya, ekaterina
```

### Temporal Signals

- mentions occur in the same canonical event
- mentions occur in adjacent events without a hard boundary
- mentions recur across a trip/day/school album
- mention appears inside a bounded date/place context

### Visual And Audio Signals

Future person resolution should combine names with:

- face tracks within a scene
- face clusters across clips
- speaker clusters
- camera-operator/narrator candidates
- co-occurring people
- clothing and age range

Names alone should not assert that two visible people are the same person. Names
can link mentions; face/speaker evidence links appearances.

### Context Signals

- same family haul
- same household
- same school
- same named event
- same date range
- same narrator sentence
- same transcript segment
- same object/place type

Example:

```text
"Philip Bindeman" on a name tag
+ transcript says "Филипп"
+ same event is Maplewood first grade
+ same child is visible
=> high confidence same person candidate
```

## Role And Relationship Signals

Once aliases are grouped, decide what each entity is doing in each interval.
This should use different evidence than alias matching.

Narrator/camera-operator clues:

- first-person speech, such as "I am going to school"
- possessives, such as "my teacher", "my old school", "my mother"
- off-camera voice that consistently frames the event
- speaker cluster that appears across many clips as the person filming
- camera movement paired with spoken commentary

Event-subject clues:

- direct statements, such as "today is Filip's first day"
- repeated visual focus on the person
- name tags, birthday cakes, classroom labels, awards, or ceremonies
- other people addressing the person by name
- model descriptions that the event is centered on that person

Mentioned-person-only clues:

- a name appears in narration but the person is not visible
- the person is historical, printed, painted, fictional, or on a sign
- the mention is a class roster, label, book title, TV content, or background
  audio
- the event is about a place or object, not the named person

Relationship clues:

- kinship words in transcript: mom, dad, son, daughter, grandma, uncle
- possessive phrases: "my son", "his teacher", "our apartment"
- recurring co-presence across family events
- repeated adult-child interactions
- school/work/community roles inferred from setting and dialogue

Relationship candidates should carry scope. "Emily is Filip's teacher" may be
valid for the Maplewood first-grade event; it should not automatically become a
global lifelong relationship.

```json
{
  "id": "relationship_candidate_000004",
  "subject_entity_id": "entity_candidate_person_filip",
  "predicate": "teacher_or_caretaker_candidate",
  "object_entity_id": "entity_candidate_person_lisa",
  "valid_during": {
    "canonical_event_ids": ["canonical_event_000004"],
    "date_range": {
      "start": "2005-09-07",
      "end": "2005-09-07",
      "precision": "day"
    }
  },
  "confidence": 0.72,
  "evidence_ids": ["gem_ev_000005", "ev_000690"],
  "review_status": "needs_review"
}
```

## What Gemini Should Do

Gemini or another frontier model can be useful as a proposer and explainer:

- propose alias groups from transcript/evidence snippets
- explain why names may or may not match
- catch cross-language nickname relationships
- detect narrator perspective, such as "my old school" vs "my son's first day"
- flag contradictions

It should not be the sole source of truth. The system should store Gemini's
proposal as one evidence-backed claim:

```json
{
  "predicate": "same_entity_candidate",
  "subject": "mention_000123",
  "object": "mention_000201",
  "source": "gemini_alias_resolution",
  "confidence": 0.74,
  "evidence_ids": ["tr_000882", "gem_ev_000005"],
  "review_status": "needs_review"
}
```

Then deterministic code can combine it with string, temporal, visual, and user
signals.

## What Deterministic Code Should Do

The local resolver should be repeatable and cheap:

1. Extract mentions from transcripts, OCR, VLM metadata, and user corrections.
2. Create normalized variants for each mention.
3. Generate candidate pairs with blocking keys so it does not compare everything
   to everything.
4. Score each pair from independent signal families.
5. Cluster pairs into entity candidates.
6. Store reasons and conflicts.
7. Surface candidates for review.
8. Treat user corrections as durable evidence.

Candidate pair scoring:

```text
score =
  string_similarity
  + transliteration_similarity
  + nickname_match
  + same_full_name_bonus
  + same_event_bonus
  + nearby_time_bonus
  + same_face_cluster_bonus
  + same_speaker_cluster_bonus
  - conflicting_full_name_penalty
  - different_person_same_scene_penalty
```

## Blocking Strategy

Do not compare every mention to every other mention. Use blocking keys:

- first normalized letter
- phonetic code
- transliterated root
- entity type
- same source video
- same family haul
- same event group or nearby time range
- same place/date context

For small family projects this can be simple. For digitizer batches it needs to
stay near-linear.

## Tools To Evaluate

Candidate local dependencies:

- RapidFuzz for string and token similarity
- a phonetic library for Soundex/Metaphone-style codes
- Unidecode/text-unidecode for simple transliteration fallbacks
- language-specific transliteration tables for Russian names
- spaCy/Stanza-style NER later, if local entity extraction is needed
- sentence-transformers for semantic similarity over longer labels and
  descriptions

These should be optional adapters. The core schema should not depend on one
library.

## Review UI Requirements

The UI should show entity candidates as review cards:

```text
Filip / Philip / Филипп
  Evidence:
    12:24 Maplewood School: "Philip Bindeman" name tag
    14:45 Tooth extraction: "Филипп"
    54:19 Hawaii: "Филипп"
  Suggested because:
    transliteration match
    same child context
    repeated across family tape
  Actions:
    Confirm same person
    Split alias
    Rename display label
    Mark as not sure
```

For places:

```text
Maplewood School / Maplewood Elementary / school on Elm and 9th
  Evidence:
    OCR/sign/name tag
    transcript mentions
    event context
  Actions:
    Confirm place
    Geocode candidate
    Mark generic school only
```

## Export Policy

Confirmed entities can be exported. Candidate entities should stay internal by
default.

Safe:

- album labels
- search filters
- review UI grouping
- narrative drafts with caveats

Needs review:

- person identity labels
- exact relationships
- exact GPS
- dates propagated from context
- cross-tape identity merges

## MVP Implementation Plan

### Phase 1: Alias Candidates

- Add `mentions.jsonl`.
- Add `entity_candidates.jsonl`.
- Add `role_assignments.jsonl`.
- Extract mentions from:
  - transcript segments
  - Gemini people/place/date metadata
  - OCR later
  - group labels
- Normalize names with:
  - casefolding
  - punctuation stripping
  - Russian transliteration table for common names
  - hardcoded nickname/alias map for family review
- Extend current `people_groups.jsonl` to cite `mention_ids`.
- Generate conservative role assignments for canonical events:
  - event subject
  - mentioned person
  - narrator/camera-operator candidate
  - visible/explicit place
  - carry-forward context

### Phase 2: User Corrections

- Add `corrections.jsonl`.
- Correction examples:
  - `Filip`, `Philip`, and `Филипп` are the same person.
  - Moscow school event is narrator visiting his old school, not Filip's first
    day.
  - `Lev Landau` is a painting/book/reference, not a person present.
- Rebuild groups using corrections as high-confidence evidence.
- Let one correction update multiple projections without mutating raw evidence:
  people groups, event titles, album titles, search snippets, and export
  metadata.

### Phase 3: Multimodal Entity Memory

- Add face tracks and speaker clusters.
- Link name mentions to visible/speaking entities when time ranges overlap.
- Avoid cross-age person assertions without face/user evidence.
- Link face/speaker clusters to person entities only as candidates until user
  confirmation.

### Phase 4: Model-Assisted Resolution

- Use Gemini/frontier model on compact evidence packets to propose candidate
  merges and contradictions.
- Store proposals as claims.
- Let deterministic resolver combine model claims with local signals.

## Concrete Current Correction

Current sample issue:

```text
Moscow School No. 123 event:
  likely narrator visiting/filming his old school on Sep 1, 2005

Maplewood School event:
  Philip/Filip's first day of first grade on Sep 7, 2005
```

The current Gemini event output separated the two school events by place/date
but did not capture the narrator-perspective distinction for Moscow. A
correction should become durable evidence so future summaries, albums, and
search results do not describe both as Philip's own first day of school.

Local transcript evidence now supports this distinction:

```text
00:55-01:00
  "Сегодня 1 сентября, я иду в школу снимать начало учебного года."
  Translation: "Today is September 1, I am going to school to film the
  beginning of the school year."

03:48-03:50
  "Людмила Петровна, моя учительница физики."
  Translation: "Lyudmila Petrovna, my physics teacher."
```

Those are narrator-perspective signals. They should create role assignments for
the Moscow event instead of treating Filip as the event subject.

## Open Product Questions

- Should the first public version include face clustering, or only leave
  schema hooks for it?
- Should corrections be family-local by default, with optional reusable alias
  packs for common transliterations and nicknames?
- Should the review UI ask "same person?" first, or "what role did this person
  play in this event?" first?
- Which exports should include candidate entities versus only confirmed
  entities?
- How should professional digitizers manage corrections across many batches
  without leaking one family's private identity memory into another family's
  project?
