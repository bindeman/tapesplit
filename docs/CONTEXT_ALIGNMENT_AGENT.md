# Context Alignment Agent

TapeSplit should treat model output as hypotheses, not final metadata. The agent loop is:

1. **Narrative pass**
   Ask a long-context video model for the tape story, unrelated content ranges, major people, places, dates, and recurring motifs. This pass is allowed to be broad, but it is not trusted for final timestamps.

2. **Focused question passes**
   Ask narrow questions against the same tape or selected segments:
   - What exact date overlays or spoken dates appear?
   - Which named places, signs, schools, parks, restaurants, or landmarks appear?
   - Which people are named by audio or visible context?
   - Which ranges are non-family content, cartoons, TV, static, or blue screen?
   - Which event boundaries look like separate days or locations?

3. **Local evidence alignment**
   Align every candidate event and entity claim against local artifacts:
   - transcript segments
   - Gemini chunk events
   - whole-tape narrative summaries
   - OCR/date/place evidence
   - visual keyframes and face observations
   - continuity context from nearby events

4. **Reconciliation pass**
   Build an evidence packet per event or uncertain range and ask a model to choose among candidates. The prompt should be constrained: it receives candidate labels, transcript snippets, nearby events, visual clues, and must return only a decision plus evidence ids. It should not invent new facts.

5. **Review/output pass**
   Export the best guess by default, with provenance:
   - `appears to be Madison, WI`
   - source icon: direct transcript, OCR, visual clue, continuity, model-only
   - alternate candidates and an `Other` correction path

## Why Not One Better Prompt?

Whole-tape Gemini is useful for story and broad context, but our tests showed two problems:

- Some responses compress long tapes into short timestamp ranges.
- Some responses spend the output budget repeating narrative text.

The agent should therefore separate **context discovery** from **timestamp authority**. Local transcript, chunked analysis, and visual assets should anchor exact time ranges.

## First Implemented Slice

The first backend slice is `event_alignments.jsonl`.

For each canonical event, it records:

- transcript support near the event's source-local range
- stronger transcript matches elsewhere in the same source video
- relocated Gemini evidence quotes when model snippets land outside the claimed range
- transcript-derived context anchors such as "city is called X" or farm/school/home context near the range
- entity support for people, places, and dates
- project-aware person alias support from `people_groups.jsonl`
- place-role support so travel plans and ambiguous place mentions do not look like GPS-ready filming locations
- evidence snippets cited by the event
- timing/support status
- warnings that should push the event to review

This becomes the evidence fabric for the later agent loop and UI provenance badges.

For example, a model may label a range as an Alaska trip because it saw an Anchorage quote in the same broad chunk. The alignment artifact should show:

- the Anchorage quote as `relocated_transcript`
- the event as `possible_misaligned`
- nearby spoken/local anchors such as `Whitewater` or `farmhouse / farm area`
- Alaska/Anchorage as ambiguous or mentioned context unless confirmed inside the event window

## Future Agent Commands

Planned commands:

- `tapesplit agent plan PROJECT`
  Summarize missing evidence and propose model/local follow-up tasks.

- `tapesplit agent ask-gemini PROJECT --source-video-id ... --question-set places`
  Run a narrow, budgeted Gemini question pass.

- `tapesplit agent align PROJECT`
  Build or rebuild local alignment artifacts.

- `tapesplit agent reconcile PROJECT`
  Use aligned evidence packets to choose final event/place/date/person candidates.

## Prompting Strategy

Preferred prompt shape:

- One task per call.
- Small output schema.
- Explicit maximum item counts.
- Require evidence ids, quotes, or transcript timestamps.
- Forbid inventing exact dates, GPS, identity, or relationships.
- Ask for alternatives and confidence when uncertain.

Example focused question:

> Given this tape and the provided transcript snippets, identify only named places that are directly spoken, visible on signs, or strongly implied by adjacent confirmed context. Return candidate, time range, evidence, confidence, and whether it is exportable as GPS.
