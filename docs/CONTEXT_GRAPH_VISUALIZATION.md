# Context Graph Visualization

TapeSplit should eventually show the context humans naturally carry in their
heads: who people are to each other, where they appear, what eras they belong
to, and how social circles change over time.

The product should not only output clips and labels. It should build a
reviewable context graph over the family archive.

## Product Goal

After processing a set of tapes, the user should be able to see:

- a family tree when relationships are known or strongly supported
- friend and social circles when formal relationships are unknown
- event-based clusters, such as school friends, work friends, neighbors, travel
  companions, wedding party, or extended family
- eras, such as early childhood, first grade, Moscow trip, Oregon years,
  Hawaii vacation, high school, college, adult life
- how a person's relationships and contexts shift across time
- evidence for every suggested edge

If no exact relationship is established, the system should still organize people
by co-occurrence, event type, place, time period, and role.

## Core Principle

Use multiple graph layers, not one flat family tree.

```text
Confirmed family tree
  reviewed parent/child/spouse/sibling/grandparent relationships

Relationship candidates
  possible mother, father, grandparent, teacher, friend, classmate, partner

Social context graph
  appears_with, same_event_as, same_place_as, addressed_by, works_with,
  goes_to_school_with, travels_with

Event graph
  events grouped by date, place, theme, people, and narrative continuity

Era graph
  time slices that summarize people, places, events, and dominant contexts
```

The family tree should stay conservative. The social context graph can be richer
because its edges are weaker and explicitly labeled.

## Relationship Edge Types

Use typed, scoped edges.

Family candidates:

- `parent_candidate`
- `mother_candidate`
- `father_candidate`
- `grandparent_candidate`
- `maternal_grandparent_candidate`
- `paternal_grandparent_candidate`
- `sibling_candidate`
- `spouse_or_partner_candidate`
- `extended_family_candidate`

Social candidates:

- `close_friend_candidate`
- `childhood_friend_candidate`
- `school_friend_candidate`
- `work_friend_candidate`
- `neighbor_candidate`
- `family_friend_candidate`
- `travel_companion_candidate`
- `classmate_candidate`
- `teacher_or_caretaker_candidate`
- `coach_or_activity_leader_candidate`
- `friend_of_friend_candidate`

Observed-context edges:

- `appears_with`
- `same_event_as`
- `same_place_as`
- `same_household_context`
- `same_school_context`
- `same_work_context`
- `same_trip_context`
- `speaks_to`
- `is_addressed_by`
- `mentions`
- `is_mentioned_by`
- `camera_focuses_on`

Observed-context edges are valuable even when they never become formal
relationships.

## Scoped Edge Model

Every edge should have scope.

```json
{
  "id": "context_edge_000123",
  "subject_entity_id": "person_dan",
  "predicate": "friend_of_friend_candidate",
  "object_entity_id": "person_alex",
  "scope": {
    "era_id": "era_2004_2006_oregon_childhood",
    "canonical_event_ids": ["canonical_event_000021", "canonical_event_000034"],
    "place_group_ids": ["place_group_school", "place_group_home"],
    "date_range": {
      "start": "2004",
      "end": "2006",
      "precision": "year"
    }
  },
  "confidence": 0.61,
  "evidence_ids": ["ev_0012", "face_cluster_link_009", "event_group_0004"],
  "supporting_signals": [
    "appears with Dan in multiple child birthday events",
    "appears in school-context events",
    "not observed without Dan elsewhere in archive"
  ],
  "review_status": "needs_review"
}
```

This lets TapeSplit express ideas like:

```text
Alex is probably Dan's friend in the 2004-2006 Oregon childhood era.
```

without claiming:

```text
Alex is a lifelong family member.
```

## Social Circle Candidates

A social circle is a cluster of people plus context.

Examples:

- `Philip's Maplewood School classmates`
- `Dan's childhood friends`
- `Pavel's work friends`
- `Moscow relatives`
- `Hawaii travel group`
- `neighbors from the old apartment`
- `wedding party`
- `Oregon hiking group`

Social circle candidate:

```json
{
  "id": "social_circle_candidate_000007",
  "label": "Dan's childhood friends",
  "circle_type": "childhood_friend_group",
  "anchor_entity_ids": ["person_dan"],
  "member_entity_ids": ["person_alex", "person_misha", "person_unknown_004"],
  "scope": {
    "era_id": "era_1998_2002_childhood",
    "canonical_event_ids": ["canonical_event_000088", "canonical_event_000102"],
    "place_group_ids": ["place_group_home", "place_group_school"]
  },
  "confidence": 0.67,
  "supporting_signals": [
    "members appear together at repeated birthday and school events",
    "Dan is the common anchor person",
    "members are similar age and share child activity contexts"
  ],
  "review_status": "needs_review"
}
```

This is useful when the exact relationship is unclear. The UI can show "Dan's
childhood circle" before it knows who is a cousin, neighbor, or classmate.

## Era Slices

An era is a bounded time/context slice. It can be calendar-based or inferred.

Examples:

- `Moscow visit, September 2005`
- `Maplewood first grade, 2005-2006`
- `Spencer Butte and Oregon hikes, February 2006`
- `Hawaii trip, March 2006`
- `early childhood birthdays`
- `grandparents' apartment years`

Era slice:

```json
{
  "id": "era_2005_2006_school_year",
  "label": "First Grade Year",
  "date_range": {
    "start": "2005-09",
    "end": "2006-06",
    "precision": "month"
  },
  "anchor_entities": ["person_philip"],
  "place_group_ids": ["place_group_maplewood_school", "place_group_home"],
  "canonical_event_ids": ["canonical_event_000004", "canonical_event_000005"],
  "dominant_contexts": ["school", "home", "childhood"],
  "confidence": 0.74,
  "review_status": "needs_review"
}
```

Eras become the main lens for understanding long archives because relationships
are not static. Someone can be a close friend in one era and absent in another.

## Visualization Surfaces

### Family Tree View

Shows only confirmed or high-confidence family edges by default.

Controls:

- show candidates
- hide unreviewed
- show evidence
- filter by era
- filter by tape/source

### Social Context Map

Graph view with people as nodes and scoped context edges.

Edge thickness should be configurable. Different users will care about different
relationship signals, and digitizers may want a simple default.

Possible thickness metrics:

- number of shared events
- number of distinct days
- number of places
- review confidence
- recency inside selected era
- total observed screen time together
- number of direct interactions
- number of times one person addresses the other
- number of events where both are central, not just background
- number of repeated event types, such as birthdays, school, trips, dinners
- number of years or eras where the connection recurs
- number of reviewed/confirmed supporting facts
- number of independent evidence modalities, such as transcript, face,
  location, OCR, and event co-occurrence
- graph centrality inside the selected era or social circle
- user-confirmed closeness rating

Subjective/inferred metrics can be optional overlays:

- perceived closeness
- affection or warmth
- playfulness/goofiness
- tension or formality
- caregiving/directive interaction
- mentor/teacher-like interaction
- shared activity intensity

These should be stored as `interaction_style` or `social_style_candidate`
signals. They should not silently convert into formal relationship facts.

Example style signal:

```json
{
  "id": "interaction_style_000041",
  "subject_entity_id": "person_philip",
  "object_entity_id": "person_dan",
  "style": "playful / goofy",
  "confidence": 0.56,
  "scope": {
    "canonical_event_ids": ["canonical_event_000088"],
    "era_id": "era_2004_2006_childhood"
  },
  "evidence_ids": ["tr_00422", "gem_ev_00102"],
  "review_status": "unreviewed"
}
```

Edge color can represent:

- family
- school
- work
- travel
- neighborhood
- unknown social context

Edge pattern can represent review state:

- solid: confirmed
- dashed: candidate
- dotted: weak/context-only
- faded: historical/outside selected era
- highlighted: current selection or active evidence

Edge labels should be short and composable:

```text
family candidate
school context
appears together often
Dan's friend group
playful interactions
work context
mentioned but not visible
```

The UI should let the user switch edge weighting:

```text
Weight by:
  shared events
  total time together
  direct interactions
  reviewed confidence
  era recurrence
  perceived closeness
  custom formula
```

Default weighting should be evidence-heavy:

```text
edge_weight =
  shared_event_count
  + direct_interaction_count * 2
  + distinct_day_count
  + reviewed_confirmation_bonus
  + multimodal_evidence_bonus
```

Subjective metrics like closeness, warmth, and goofiness should be separate
view modes so users can understand they are interpretive.

### Era Timeline

Horizontal timeline of eras and events.

Selecting an era updates:

- family tree
- friend/work/social graph
- people list
- places
- event albums
- unresolved review tasks

### Person Page

For each person candidate:

- representative thumbnails over time
- known aliases
- first/last appearance
- confirmed relationships
- relationship candidates
- social circles
- common places
- common event types
- "seen with" people ranked by evidence
- open review questions

### Event Lens

For a selected event:

- who appears
- who speaks
- who is mentioned
- who is the event subject
- place/date anchors
- relationship context known at that time
- similar events
- uncertainty and review tasks

### Place Lens

For a selected place:

- people commonly seen there
- events at that place
- likely roles, such as school, workplace, home, church, park, restaurant
- eras where the place matters
- possible geocoding/GPS candidates

## Inference Signals

Friend/work/social-context inference can use:

- repeated co-appearance
- repeated same-place context
- same event type across time
- age similarity
- role terms in speech: coworker, friend, neighbor, classmate, teacher, boss
- uniforms, offices, schools, classrooms, work badges, yearbooks, trophies
- captions or OCR names
- who the camera follows
- who is addressed directly
- who appears only through another anchor person
- recurring groups around the same named person

Example:

```text
Dan appears in family birthday events.
Alex appears only when Dan appears.
Alex is similar age to Dan.
The events are in school/home child contexts.
No family terms are used for Alex.
=> Alex is a childhood_friend_candidate or friend_of_friend_candidate scoped to Dan.
```

The graph should avoid overclaiming. It can show:

```text
Alex is strongly connected to Dan in childhood events.
```

without saying:

```text
Alex is Dan's best friend.
```

## Data Products

Long term artifacts:

```text
.tapesplit/
  context_edges.jsonl
  edge_metrics.jsonl
  interaction_styles.jsonl
  social_circle_candidates.jsonl
  era_slices.jsonl
  person_profiles.jsonl
  graph_review_tasks.jsonl
  graph_views.jsonl
  graph_exports/
    family_tree.json
    social_context_graph.json
    era_timeline.json
```

Person profile projection:

```json
{
  "id": "person_profile_philip",
  "entity_candidate_id": "person_philip",
  "display_label": "Philip",
  "aliases": ["Philip", "Filip", "Филипп"],
  "representative_thumbnail_paths": [
    "thumbnails/entities/person_philip_2005.jpg",
    "thumbnails/entities/person_philip_2006.jpg"
  ],
  "first_seen": "2005-09-07",
  "last_seen": "2006-03-26",
  "confirmed_relationship_ids": [],
  "candidate_relationship_ids": ["relationship_candidate_000001"],
  "social_circle_ids": ["social_circle_candidate_000003"],
  "common_place_group_ids": ["place_group_maplewood_school", "place_group_home"],
  "open_review_task_ids": ["relationship_review_task_000001"]
}
```

Edge metric projection:

```json
{
  "id": "edge_metric_000123",
  "edge_id": "context_edge_000123",
  "metric_set": "default_social_context",
  "shared_event_count": 8,
  "distinct_day_count": 4,
  "distinct_place_count": 3,
  "total_overlap_seconds": 1920,
  "direct_interaction_count": 5,
  "addressed_by_name_count": 2,
  "central_event_count": 3,
  "era_recurrence_count": 2,
  "multimodal_evidence_count": 3,
  "reviewed_confirmation_count": 1,
  "computed_weight": 0.76,
  "review_status": "unreviewed"
}
```

Saved graph view:

```json
{
  "id": "graph_view_000004",
  "label": "Philip's Childhood Social Context",
  "scope": {
    "anchor_entity_ids": ["person_philip"],
    "era_ids": ["era_2004_2006_childhood"],
    "place_group_ids": []
  },
  "edge_filters": {
    "min_weight": 0.25,
    "include_predicates": ["appears_with", "school_friend_candidate", "family_candidate"],
    "review_states": ["confirmed", "needs_review", "unreviewed"]
  },
  "edge_weight_metric": "shared_events",
  "layout": "force_directed",
  "notes": "Freeform view created during review."
}
```

## Review UX

The UI should ask questions in terms humans understand:

- "Are these people family, friends, classmates, coworkers, or unknown?"
- "Is Alex connected to Dan, or to the family more broadly?"
- "Is this person present in this event, or only mentioned?"
- "Is this the same friend group as the birthday party two months earlier?"
- "Should this group be labeled Maplewood classmates?"
- "Should this relationship only apply to the 2005 school year?"

Corrections should update the graph without mutating raw evidence.

The UI should also allow freeform structure:

- rename a social circle
- create a custom group
- pin two people as "connected"
- mark an edge as "not family, but close"
- tag an edge as "work", "school", "neighbor", "family friend", or custom text
- add a note like "Dan's friend from camp"
- split a social circle into two groups
- merge two friend groups
- mark a group as era-specific

Freeform user structure should become first-class correction evidence:

```json
{
  "id": "correction_000221",
  "correction_type": "custom_context_edge",
  "subject_entity_id": "person_alex",
  "predicate": "dans_friend_from_camp",
  "object_entity_id": "person_dan",
  "scope": {
    "era_id": "era_1998_2002_childhood"
  },
  "label": "Dan's camp friend",
  "created_by": "reviewer",
  "review_status": "confirmed"
}
```

The graph should support both structured predicates and user-defined labels.
Structured predicates make search, filtering, and exports reliable. User labels
capture the real human context that will often be too specific for a fixed
ontology.

## Implementation Phases

### Phase 1: Context Edges

- Generate `appears_with`, `same_event_as`, `same_place_as`, and
  `same_event_subject_context` edges from existing event/person/place groups.
- Add edge confidence and evidence ids.
- Search-index the edges.

### Phase 2: Era Slices

- Build eras from date groups, event groups, and major place/trip segments.
- Add era filters to report/search projections.

### Phase 3: Social Circles

- Cluster people by repeated co-occurrence, place, event type, and era.
- Label circles conservatively using evidence: school, work, travel, home,
  family holiday, childhood.

### Phase 4: Graph Review UI

- Add person pages, event lens, place lens, and open graph review tasks.
- Show candidate edges with evidence snippets and thumbnails.

### Phase 5: Interactive Visualization

- Add a real graph UI after the static report proves the data model.
- Use filters for era, edge type, confidence, reviewed/unreviewed, tape, and
  place.

## Why This Matters

This is the deeper product insight: the user does not only want sorted videos.
They want the archive to recover context.

Humans remember relationships as a mix of family, place, era, events, habits,
and social proximity. TapeSplit can represent that context explicitly, with
evidence and uncertainty, instead of forcing every connection into a brittle
family-tree edge.
