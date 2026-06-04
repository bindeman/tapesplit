# Visualization Backend

TapeSplit should act as the backend for a richer visualization app. The frontend
should not have to understand every raw JSONL artifact. It should consume a
small set of stable, UI-ready outputs.

## Visual Assets

Extract scene and event images:

```bash
.venv/bin/tapesplit extract-visuals /path/to/project.tapesplit --force
```

This writes:

```text
visual_assets.jsonl
keyframes/scenes/*.jpg
keyframes/events/*.jpg
thumbnails/scenes/*.jpg
thumbnails/events/*.jpg
```

Each `visual_assets.jsonl` row links a `scene` or `event` to a keyframe,
thumbnail, source video, source timestamp, and review status. These assets are
the UI covers for timelines, event cards, albums, and search results.

## Face Thumbnails

Install the optional local vision dependency:

```bash
.venv/bin/python -m pip install -e '.[vision]'
```

Then scan extracted keyframes:

```bash
.venv/bin/tapesplit detect-faces /path/to/project.tapesplit --subject-type scene
```

This writes:

```text
face_observations.jsonl
thumbnails/faces/*.jpg
```

Face observations are intentionally not identities. They are reviewable visual
anchors that can later be linked to `people_groups.jsonl` through local face
clustering, human confirmation, or both.

## Visualization Export

Export the UI-ready aggregate:

```bash
.venv/bin/tapesplit export-visualization /path/to/project.tapesplit
```

This writes `visualization.json` with:

- `media`: source video records
- `timeline.events`: event cards with people, places, dates, and thumbnails
- `timeline.scenes`: scene intervals with thumbnails
- `tracks.people`: person lanes across events
- `tracks.places`: place lanes across events
- `tracks.albums`: album lanes and covers
- `places`: normalized display records for location UIs
- `people`: display records for people UIs
- `relationships.nodes` and `relationships.edges`: graph-ready relationship and
  context edges
- `assets.visual` and `assets.faces`: raw asset records

## Location Display

Location display should be conservative:

- Exact named places display as their label.
- Generic places such as `home`, `school`, `park`, or `lake` display with scope
  when available, for example `home (Madison, Wisconsin context)`.
- Parent and nearby place links are shown as context, not GPS truth.
- Coordinates are included only when a place has reviewed/geocoded latitude and
  longitude fields.

This avoids flattening different eras of `home` into one location and avoids
pretending a generic visual place like `lake` or `classroom` is a confirmed
address.

## UI Shapes This Enables

The exported data can drive:

- horizontal source-video timelines
- family event timelines
- per-person appearance lanes
- per-place history lanes
- relationship graphs with edge weights
- map views for reviewed/geocoded places
- album grids with cover thumbnails
- face-review queues
- context drilldowns from event to evidence, scene, person, place, and source
  video timestamp

The next backend step is identity linking: connect face observations to reviewed
person groups and store those links as evidence, not as automatic truth.
