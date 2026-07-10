# The Journal

The Journal is the archive telling its own story: machine-drafted posts that
read like warm family journal entries while doubling as a grounding layer —
every factual sentence cites the events and transcript segments it derives
from, every pull-quote is verbatim tape audio, every name and place is a live
link into the archive. Implementation: `src/tapesplit/journal.py`; CLI:
`tapesplit journal generate|list`; artifact: `journal_posts.jsonl`.

> Merge note: this file is the Journal section of the design standard. When
> `docs/DESIGN.md` stabilizes, fold it in as a sub-theme; token names below
> already assume DESIGN.md's vocabulary.

## Voice standard

First-person-family, warm, specific. The delight must come from real detail —
a verbatim quote, a small observed thing — never invented color. No
greeting-card sentiment, no camera-log framing ("the footage shows", "we begin
with"). Names from the people roster whenever events or quotes support them;
warm generics ("the birthday boy") when identity is unclear, never clinical
ones ("the child"). Uncertainty is hedged honestly ("probably summer 2002").
Russian quotes stay Russian with a gentle translation alongside.

## Visual identity (Journal sub-theme)

- **Display type is rounded**: post titles, kickers, and pull-quotes use
  `font-family: ui-rounded, -apple-system, system-ui` (SF Rounded on Apple
  platforms) — the one place TapeSplit departs from the standard SF text
  ladder, giving the Journal its scrapbook warmth. Body text stays on the
  standard reading stack.
- Editorial reading measure (~65ch), generous whitespace, soft date bylines.
- Pull-quotes render as margin notes: rounded type, speaker chip, a play
  button that jumps playback to the quote's tape timestamp. Tasteful — no
  kitsch, no fake paper textures.
- Citations render as unobtrusive footnote dots that highlight their source
  clip/segment on hover — grounding visible, never academic.
- A quiet "drafted from the tapes" byline discloses machine drafting.

## The grounding contract

Structural conventions (typed block stream; kicker/title/dek/hero
front-matter; provenance derived from block citations, never hand-written;
`generated` disclosure) are adapted from sphre's briefs. The contract is
enforced mechanically at generation time (`_validate_blocks`):

1. Every `paragraph` block carries citations to packet event/segment ids —
   no citation, no paragraph.
2. `pullquote` text must be a verbatim substring of the cited transcript
   segment (whitespace-normalized); speaker/timestamps are attached from the
   segment, not from the model.
3. Entity spans must appear verbatim in the block text and resolve to real
   people/place/event ids.
4. Violating blocks get one regeneration round with the violations as
   feedback, then are dropped; the rejected-block rate is reported.

Posts are claims: each post dual-writes to the claim substrate (producer
`journal/<deployment>`, assertion carrying the citation set), so narratives
participate in verification like any other derived assertion.

## Generation

`tapesplit journal generate <project>` drafts posts for the richest dated
day/school albums (2–24 events; the mega trip albums stay out until a
multi-day chapter format exists). Packets carry events (reconciled titles),
quote candidates (named-speaker segments overlapping event ranges — this
scoping also keeps TV-broadcast audio out), people/place rosters with ids,
and date confidence. Default deployment `gpt-5.6-terra`, ~$0.05/post;
`--force` regenerates, `--dry-run` prints the packet plan.
