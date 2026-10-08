# TapeSplit Design Standard

The review UI is built to feel like an intentionally designed Apple product —
Photos, not a dashboard. This document is the enforceable standard: every
color, size, weight, radius, duration, and shadow in `apps/review-ui` must
come from the tokens defined here (declared at `:root` in `styles.css`).
A value that isn't a token is a defect.

Direction: iLife '26 on macOS Golden Gate — full-height shaded sidebar with
colored glyphs, one consolidated toolbar, 4:3 tiles with captions below,
glass sheets.

Grounding: Apple Human Interface Guidelines (macOS density, SF type family)
and macOS 27 Golden Gate: Liquid Glass dialed toward legibility, squarer and
consistent corner radii, a sidebar flush to the window edge. This is a
desktop app, so the macOS scale applies — iOS sizes (17pt body) would read
oversized in a dense library grid.

## Typography

Font stacks:

| Token | Stack | Use |
|---|---|---|
| `--font-stack` (alias `--font`) | `-apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, "Helvetica Neue", "Segoe UI", sans-serif` | Everything by default |
| `--font-display` | `-apple-system, BlinkMacSystemFont, "SF Pro Display", system-ui, …` | Titles from Title 3 up |
| `--font-serif` | `ui-serif, "New York", "Iowan Old Style", "Charter", Georgia, serif` | The Journal only |
| `--font-mono` | `ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace` | Timecodes, tape positions, raw ids |

SF text-style ladder — desktop-tuned. Each style is a size token plus a
prescribed weight/line-height/tracking; compose them exactly:

| Style        | Token               | Size | Weight | Line height | Tracking | Use |
|--------------|---------------------|------|--------|-------------|----------|-----|
| Display      | `--fs-display`      | 34px | 700    | 1.1         | -0.03em  | Year headers (Chronological); Journal entry titles (serif, 1.12) |
| Large Title  | `--fs-large-title`  | 26px | 700    | 1.1         | -0.02em  | Library group headers ("Tape 16"), the person sheet's name |
| Title 1      | `--fs-title1`       | 22px | 700    | 1.2         | -0.02em  | Sheet and review titles; Journal card titles and headings (serif) |
| Quote        | `--fs-quote`        | 21px | 400 italic | 1.4     | 0        | Journal pull quotes (serif) |
| Dek          | `--fs-dek`          | 19px | 400    | 1.45        | 0        | Journal standfirst (serif) |
| Title 2      | `--fs-title2`       | 17px | 700    | 1.25        | -0.01em  | Section titles (Memories), the Review drawer title, Memories card titles |
| Reading      | `--fs-reading`      | 16px | 400    | 1.7         | 0        | Journal body text (SF) |
| Title 3      | `--fs-title3`       | 15px | 600    | 1.25        | -0.01em  | Row headings; 700 for toolbar view titles and the best-guess name |
| Headline     | `--fs-headline`     | 13px | 600    | 1.3         | 0        | Tile titles, emphasized body |
| Body         | `--fs-body`         | 13px | 400    | 1.45        | 0        | Default text (`body` element) |
| Callout      | `--fs-callout`      | 12px | 400–600 | 1.4        | 0        | Secondary copy, chips, buttons, list titles |
| Footnote     | `--fs-footnote`     | 11px | 400–600 | 1.35       | 0        | Metadata lines, subtitles; 600 for section labels |
| Caption      | `--fs-caption`      | 10px | 600–700 | 1.3        | 0        | Badges, length capsules; 700 uppercase for kickers |

Second faces: `--font-serif` (New York) is the Journal's face — entry and
card titles, headings, deks and pull quotes — and never appears in app
chrome, controls or lists. `--font-mono` sets timecodes and tape positions,
always with tabular numbers.

Rules:
- No fractional or off-ladder sizes (12.5px, 13.5px, 9.5px are defects).
- Weights: 400, 500, 600, 700 only (590/650 are defects).
- Hierarchy per view: the toolbar names the view (Title 3); content groups
  take Large Title (Display for years); tiles use Headline; metadata uses
  Footnote + `--text-secondary`. Never express hierarchy with color alone.
- Numbers that count, align or tick (counts, years, timecodes) use
  `font-variant-numeric: tabular-nums`.

Documented exceptions: avatar monogram initials use `--fs-large-title`;
the ⌘K and Search fields use 16px (`--fs-search`) to match macOS Spotlight.

## Color

Semantic tokens, light and dark. Never use raw hex/rgba in component rules.

| Token | Role |
|---|---|
| `--bg` / `--bg-elev` / `--bg-elev-2` | Canvas and raised surfaces (list panes, grouped rows, fallbacks) |
| `--card` | Cards, sheets, the miniplayer |
| `--control-bg` | Secondary buttons, pop-up buttons, inputs |
| `--sidebar-bg` | The shaded sidebar column, a step darker than the canvas |
| `--segment-thumb` | The segmented control's sliding thumb |
| `--text-primary` (alias `--text`) | Primary label |
| `--text-secondary` (alias `--text-2`) | Secondary label |
| `--text-tertiary` (alias `--text-3`) | Tertiary label, placeholders, sidebar section labels |
| `--on-accent` | Text and glyphs on accent fills |
| `--fill` / `--fill-secondary` | Control fills: capsule fields, tracks, quiet chips |
| `--fill-hover` / `--selection` | Row hover; the sidebar's selected row |
| `--hairline` (alias `--line`) / `--line-strong` | Separators, scrollbars |
| `--accent` / `--accent-ink` / `--accent-soft` | Interactive tint (`#007aff` light, `#0a84ff` dark), its text-on-wash tone, and its wash |
| `--danger` / `--warn` / `--ok` (each with `-ink` and `-soft`) | Status semantics only — never decorative |
| `--scrim` | Modal underlay; behind sheets and the Review drawer it adds a 10px blur |

Token families:

- **Facts** — `--when` (orange), `--where` (green), `--who` (blue),
  `--voice` (teal). Each has a solid tone for glyphs, an `-ink` for text on
  its wash, and a `-soft` wash. A sheet's When, Where and Who chips use
  them; Voice marks speakers in Review, spoken search hits and Journal
  speaker chips.
- **Source-list glyphs** — `--glyph-library` (blue), `--glyph-people`
  (orange), `--glyph-places` (green), `--glyph-albums` (yellow),
  `--glyph-journal` (red), `--glyph-search` (gray), `--glyph-review`
  (indigo). A glyph keeps its color when its row is selected.
- **Review kinds** — `--task-faces` (purple), `--task-voices` (teal),
  `--task-relationships` (orange), `--task-places` (green), `--task-people`
  (indigo), `--task-same` (violet), `--task-moments` (pink), `--task-dates`
  (amber). Each fills the tile behind its kind's white glyph and tints its
  badge at 14%.
- **Tape-strip kinds** — `--kind-travel` (blue, labeled Trips),
  `--kind-home` (orange), `--kind-school` (green), `--kind-celebration`
  (pink), `--kind-other` (purple); `--strip-footage` for footage outside any
  moment.
- **Paper** — `--paper`, `--paper-canvas`, `--paper-edge`, `--ink`,
  `--ink-2`, `--ink-accent`. The Journal only.
- **Map** — `--map-ocean`, `--map-land`, `--map-border`.

Contrast floor: WCAG AA (4.5:1 body text, 3:1 large text) in both themes —
primary and secondary labels on every surface, each `-ink` on its own wash,
and paper ink on paper. `--text-tertiary` is decorative-adjacent — never the
only carrier of meaning.

Status washes: `--ok-soft` / `--danger-soft` / `--warn-soft` (≈12–16% tints)
back status chips; the solid status colors are reserved for dots and icons.

Documented exceptions to the no-raw-color rule (theme-invariant by design):
content overlays on photographs (white text, black gradient scrims, the dark
glass capsules for lengths and timecodes, the glass play button), white
glyphs on identity-colored tiles, map pins (a white photo-print border and
its drop shadow, the badge's white ring), the Contacts monogram gradient,
and the app icon's own palette. White labels
on `--accent` fills follow the macOS system blue at about 4:1, set semibold.

## Materials

Glass is dialed toward opaque: content shows through as color, never as
legible detail. Each material pairs with a hairline edge or the 0.5px ring
built into the shadow tokens.

| Tier | Token | Recipe | Use |
|---|---|---|---|
| Sidebar | `--sidebar-bg` | `--blur-chrome` over ≈90% shade | The sidebar column |
| Chrome | `--material` | `--blur-chrome` (`saturate(180%) blur(20px)`) over ≈80% bg | Toolbars, the Review drawer's header, sticky sheet buttons |
| Overlay | `--material-overlay` | `--blur-overlay` (`saturate(180%) blur(40px)`) over ≈88% bg | ⌘K, menus, popovers, the toast |
| On photo | `--glass-on-photo` | `--blur-chrome` over 42% near-black | Controls that sit on a photograph (a hero's close button) |

## Spacing

4pt grid. Tokens `--sp-1` … `--sp-8` = 4, 8, 12, 16, 20, 24, 32, 40px.
Component padding and gaps sit on the grid; 10px is permitted inside dense
controls (`--sp-2x: 10px`) as the single sanctioned half-step. Inside a
control, 1–7px nudges are optical alignment, not layout. Anything else
off-grid is a defect.

Layout constants: `--gutter` (28px, 18px narrow) is the content column's
side margin; `--sidebar-w` (240px, 64px narrow); `--toolbar-h` (52px), which
the sidebar's brand row matches so the two read as one band.

## Radii

Squarer than Tahoe, one value per job:

| Token | Value | Use |
|---|---|---|
| `--r-xs` | 4px  | The tape strip, keycaps, photos set into the Journal page |
| `--r-s`  | 6px  | Sidebar and queue rows, inputs, kind tiles, filmstrip frames, the Journal page |
| `--r-m`  | 9px  | Library tiles, thumbnails, map pins, menus |
| `--r-l`  | 12px | Sheets, the Review drawer, panels, grouped lists, Memories cards, the map surface |
| `--r-xl` | 16px | The ⌘K panel |
| `--r-pill` | 999px | Buttons, capsule fields, chips, the segmented control, badges, avatars |

Nested radii: inner = outer − inset (a 12px card with 4px inset media uses 8px).

## Elevation

| Token | Use |
|---|---|
| `--shadow-1` | Tiles and cards at rest |
| `--shadow-2` | Raised: hovered tiles, popovers, menus, the toast |
| `--shadow-3` | Modal: sheets, the Review drawer, ⌘K, the miniplayer |
| `--shadow-control` | Controls: secondary and pop-up buttons, inputs, the segmented thumb |
| `--shadow-paper` | The Journal page |
| `--sheen` + `--sheen-edge` | The candy sheen on primary buttons — the Aqua nod, kept faint |

Shadows imply interactivity or modality — decorative shadows are defects.
The Journal is the one exception: its page and the photos set into it cast
the soft shadow of paper.

## Motion

| Token | Value | Use |
|---|---|---|
| `--dur-fast` | 150ms | Hover, focus, small state |
| `--dur-med`  | 180ms | Reveals: fades, tile lifts |
| `--dur-slow` | 220ms | Sheet and drawer entry, the segmented thumb's slide |
| `--ease-out` | `cubic-bezier(0.2, 0.9, 0.3, 1)` | Default — decelerate into place |
| `--ease-spring` | `cubic-bezier(0.2, 0.7, 0.3, 1.1)` | Playful pop (map pins) |

Rules: animate `transform`/`opacity`, never layout. Everything honors
`prefers-reduced-motion: reduce` (transitions collapse to 1ms; Ken Burns and
map fly-to become static). Documented exceptions: spinner (1.1s linear),
Ken Burns (14s ease-in-out alternate).

## Components (inventory + composition)

- **Sidebar**: full height, flush to the window's left edge, `--sidebar-bg`
  with a hairline right edge. A brand row as tall as the toolbar (app icon,
  name, archive byline), then sections "Archive" (Library, People, Places)
  and "Collections" (Albums, Journal, Search) under Footnote 600
  `--text-tertiary` labels. Rows are 30px, Body 400, `--r-s`, each with a
  17px glyph in its `--glyph-*` color; the selected row takes `--selection`
  and weight 600. The footer holds Review (count in a quiet `--fill`
  capsule) and a status line with a refresh button.
- **Toolbar**: one per view, sticky, `--toolbar-h` tall, Chrome material. Its
  hairline bottom edge appears once content scrolls under it. Left: the view
  title (`--font-display`, Title 3 700) over a Footnote stat line. Right, in
  this order everywhere: the view's controls, a 200px search capsule (⌘K;
  collapses to an icon below 1200px), and Review (tray glyph in
  `--glyph-review` with a quiet count, never a red bubble). Nested views
  (a Journal entry) add a back button.
- **Segmented control**: a `--fill` capsule track; a `--segment-thumb` pill
  with `--shadow-control` slides between 24px segments (`transform` and
  `width`, `--dur-slow`). Segments are Callout 500, secondary until
  selected.
- **Event card** (Library tile): a 4:3 thumbnail at `--r-m`, `--shadow-1`;
  on hover the thumbnail lifts 2px to `--shadow-2` and plays a muted
  preview; focus draws a 3px `--accent` ring around it. The caption sits
  below: title Headline 600 (two lines at most), meta Footnote secondary
  (year · place, one line). The length rides a dark glass capsule
  bottom-right, legible over the camcorder date stamp that shares that
  corner. The review dot appears on hover only.
- **Tape group**: "Tape N" in Large Title with a Body secondary detail line
  (date span · count · file) and a color legend. Under it, the tape strip:
  10px, `--r-xs`, the whole tape left to right. Hatching marks blank tape,
  `--strip-footage` marks footage outside any moment, and each moment is a
  `--kind-*` segment that opens it. Chronological groups use Display year
  headers; By Place groups use Large Title with a `--where` pin glyph.
- **Memories**: a row that bleeds to the edges of the content column; 16:9 cards at
  `--r-l`, `--shadow-1`, Ken Burns, title Title 2 700 with a Footnote date
  over a black gradient.
- **Sheets** (event/person/album/place): `--card`, `--r-l`, `--shadow-3`,
  over a `--scrim` with a 10px blur that keeps background tiles
  unidentifiable; entry `--dur-slow --ease-out`. The close button is a
  sticky glass circle (`--glass-on-photo` over a hero). The header is a
  Footnote 600 uppercase kicker and a Title 1 title, then fact chips: When
  (calendar glyph, `--when`), Where (pin, `--where`), and the tape position
  (film glyph, mono timecode); Who as person bubbles in `--who`. Scenes are a
  filmstrip of 4:3 frames with mono timecode capsules.
- **Review drawer**: a panel inset 8px from the window's right edge,
  `--r-l`, `--shadow-3`. A Chrome header and tools row (filter capsule,
  pop-up buttons, the primary Accept Best Guesses) over a 320px list on
  `--bg-elev` and a detail pane. List rows carry a 22px `--r-s` kind tile
  (white glyph on `--task-*`), a Callout 600 title without its "Resolve …:"
  prefix, and Footnote kind · confidence; the selected row fills with
  `--accent`. Diarizer ids read as "Unnamed voice A · Tape 16", raw id in the
  tooltip. Detail: media at `--r-l`, a tinted kind badge, a Title 1 title,
  the best-guess card on `--accent-soft`, then evidence and candidates as
  grouped lists with tape labels and timecode capsules.
- **Buttons and fields**: primary buttons are 28px `--accent` pills with
  `--sheen` and `--sheen-edge`, Callout 600; secondary and danger buttons sit
  on `--control-bg` with `--shadow-control` (danger text in
  `--danger-ink`). Fields are `--control-bg` with `--shadow-control`; focus is
  a 1px `--accent` line and a 4px `--accent-soft` halo, and capsules that
  wrap a field light up whole.
- **Chips/pills**: `--r-pill`; fact chips use their `-soft` wash and `-ink`
  text with a 13px glyph; quiet tokens are Footnote 500 on
  `--fill-secondary`; status chips use status colors only for status.
- **⌘K search**: an Overlay panel at `--r-xl` with a 44px field, sectioned
  results under Footnote 600 uppercase headers; moment hits are cards, spoken
  hits a grouped list with `--voice` play glyphs. The Search view uses the
  same panel with a capsule field.
- **Journal**: paper cards (3:2 photo; `--ink-accent` kicker; serif Title 1
  title; `--ink-2` dek). An entry is a page: `--paper` on `--paper-canvas`,
  `--shadow-paper`, `--r-s`, at most 740px wide. Serif Display title, serif
  Dek, Reading body in SF at 1.7, serif italic pull quotes behind a 3px
  `--ink-accent` rule, translations in SF, speakers as `--voice` chips, and
  grounding dots that wake to `--ink-accent` on hover.
- **Map pins** (Places): a photo in a white 2.5px print border at `--r-m`
  (clusters `--r-l`) with an `--accent` count badge; approximate geocode =
  dashed border; hover pop `--dur-fast --ease-spring`.
- **People**: 84px avatars (104px on the wall and person sheet); monograms
  use the Contacts gray gradient with white initials. "Same person?" cards
  sit on `--bg-elev` at `--r-l` with small Merge / Not the Same buttons.
- **Miniplayer**: `--card`, `--r-l`, `--shadow-3`, a Chrome drag header, 4:3
  video, a Headline title over a mono timecode.
- **App icon**: a cassette with a photo print tucked into it — flat layers, a
  crisp dark outline, a faint top sheen (`shell.tsx`; the favicon in
  `index.html`).
- **Narrow windows**: below 1200px the search capsule becomes an icon; below
  1080px the map's region rail hides and the Review list narrows to 260px;
  below 900px the sidebar becomes a 64px glyph rail, toolbar subtitles hide,
  and Review stacks its list over the detail.

## Places map: rendering rationale

The map is a bundled-GeoJSON stylized vector map (Natural Earth 1:110m
countries and 1:10m US states from the `world-atlas` and `us-atlas`
packages, bundled at build time) rendered by our own SVG layer — not a tile
service. Trade-off considered: MapLibre + OSM raster/vector tiles gives
street-level fidelity but pulls a runtime network dependency (against
local-first), an 800KB+ JS dependency, OSM tile-usage-policy constraints,
and a cartographic style we can't fully own. The archive's places resolve to
city granularity (Eugene, Moscow, Davos, Hilo) — country/state/city zoom is
sufficient, fully offline, and stylable to match this standard exactly
(`--map-land` on `--map-ocean`, `--map-border` hairlines, state lines past
2.2× zoom, Antarctica omitted). The camera fits every pin to the surface it
actually has and never zooms out past one world width. If street-level ever
matters, MapLibre can slot behind the same component API.
