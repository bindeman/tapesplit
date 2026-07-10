# TapeSplit Design Standard

The review UI is built to feel like an intentionally designed Apple product —
Photos, not a dashboard. This document is the enforceable standard: every
color, size, weight, radius, duration, and shadow in `apps/review-ui` must
come from the tokens defined here (declared at `:root` in `styles.css`).
A value that isn't a token is a defect.

Grounding: Apple Human Interface Guidelines (macOS density, SF type family).
This is a desktop app, so the macOS scale applies — iOS sizes (17pt body)
would read oversized in a dense library grid.

## Typography

Font stack (`--font-stack`, alias `--font`):

```
-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text",
"Helvetica Neue", "Segoe UI", sans-serif
```

SF text-style ladder — desktop-tuned. Each style is a size token plus a
prescribed weight/line-height/tracking; compose them exactly:

| Style        | Token               | Size | Weight | Line height | Tracking | Use |
|--------------|---------------------|------|--------|-------------|----------|-----|
| Large Title  | `--fs-large-title`  | 26px | 700    | 1.15        | -0.4px   | View hero title (one per screen) |
| Title 1      | `--fs-title1`       | 22px | 700    | 1.2         | -0.3px   | Sheet/drawer headers |
| Title 2      | `--fs-title2`       | 17px | 600    | 1.25        | -0.2px   | Section/group headings |
| Title 3      | `--fs-title3`       | 15px | 600    | 1.3         | -0.1px   | Sub-section headings, row headings |
| Headline     | `--fs-headline`     | 13px | 600    | 1.4         | 0        | Card titles, emphasized body |
| Body         | `--fs-body`         | 13px | 400    | 1.45        | 0        | Default text (`body` element) |
| Callout      | `--fs-callout`      | 12px | 400    | 1.4         | 0        | Secondary copy, list detail |
| Footnote     | `--fs-footnote`     | 11px | 400    | 1.35        | 0        | Metadata lines, bylines |
| Caption      | `--fs-caption`      | 10px | 500    | 1.3         | +0.1px   | Chips, badges, timestamps |

Display variant: `--font-display-rounded` (`ui-rounded` / SF Rounded) is the
one sanctioned second face — a deliberate warm accent reserved for editorial
surfaces (e.g. the Journal view's titles), never for core app chrome, body
text, or controls. Journal's own sub-theme section will extend this document.

Rules:
- No fractional or off-ladder sizes (12.5px, 13.5px, 9.5px are defects).
- Weights: 400, 500, 600, 700 only (590/650 are defects).
- Hierarchy per screen: one Large Title; Title 2 for groups; Headline for
  cards; Footnote + `--text-secondary` for metadata. Never express hierarchy
  with color alone.

Documented exceptions: avatar monogram glyphs may use `--fs-large-title`;
the ⌘K search field uses 16px (`--fs-search`) to match macOS Spotlight.

## Color

Semantic tokens, light and dark. Never use raw hex/rgba in component rules.

| Token | Role |
|---|---|
| `--bg` / `--bg-elev` / `--bg-elev-2` | Canvas and raised surfaces |
| `--card` | Card/sheet surface |
| `--text-primary` (alias `--text`) | Primary label |
| `--text-secondary` (alias `--text-2`) | Secondary label |
| `--text-tertiary` (alias `--text-3`) | Tertiary label / placeholders |
| `--fill` / `--fill-secondary` | Control fills (quiet buttons, inputs, tracks) |
| `--hairline` (alias `--line`) / `--line-strong` | Separators |
| `--accent` / `--accent-soft` | Interactive tint + its 10-16% wash |
| `--danger` / `--warn` / `--ok` | Status semantics only — never decorative |
| `--scrim` | Modal underlay |

Contrast floor: WCAG AA (4.5:1 body text, 3:1 large text) in both themes.
`--text-tertiary` is decorative-adjacent — never the only carrier of meaning.

Status washes: `--ok-soft` / `--danger-soft` / `--warn-soft` (≈12-14% tints)
back status chips; the solid status colors are reserved for dots and icons.

Documented exceptions to the no-raw-color rule (theme-invariant by design):
content overlays (white text + black gradient scrims over photographs — they
sit on imagery, not on the theme), the entity-category dot palette
(purple/cyan/orange/… identity colors), and the brand-mark gradient.

## Materials

Three vibrancy tiers, always paired with a hairline border on the leading edge:

| Tier | Token | Recipe | Use |
|---|---|---|---|
| Chrome | `--material` | `blur(28px) saturate(1.6)` over 78-82% bg | Sidebar, toolbars |
| Overlay | `--material-overlay` | `blur(40px) saturate(1.8)` over 72-76% bg | Sheets, ⌘K, popovers |
| Thin | `--material-thin` | `blur(16px) saturate(1.4)` over 60% bg | Badges over imagery, hover chrome |

## Spacing

4pt grid. Tokens `--sp-1` … `--sp-8` = 4, 8, 12, 16, 20, 24, 32, 40px.
Component padding and gaps sit on the grid; 10px is permitted inside dense
controls (`--sp-2x: 10px`) as the single sanctioned half-step. Anything else
off-grid is a defect.

## Radii

| Token | Value | Use |
|---|---|---|
| `--r-xs` | 5px  | Chips, badges, small controls |
| `--r-s`  | 8px  | Buttons, inputs, nav rows |
| `--r-m`  | 12px | Cards, tiles |
| `--r-l`  | 18px | Sheets, drawers |
| `--r-xl` | 26px | Hero cards, ⌘K panel |
| `--r-pill` | 999px | Pills, avatars |

Nested radii: inner = outer − inset (an 12px card with 4px inset media uses 8px).

## Elevation

| Token | Use |
|---|---|
| `--shadow-1` | Cards at rest |
| `--shadow-2` | Raised: hover cards, popovers, miniplayer |
| --shadow-3` | Modal sheets |

Shadows imply interactivity or modality — decorative shadows are defects.

## Motion

| Token | Value | Use |
|---|---|---|
| `--dur-fast` | 150ms | Hover, focus, small state |
| `--dur-med`  | 250ms | Reveals, sheet secondary motion |
| `--dur-slow` | 400ms | Sheet entry, view transitions |
| `--ease-out` | `cubic-bezier(0.2, 0.9, 0.3, 1)` | Default — decelerate into place |
| `--ease-spring` | `cubic-bezier(0.2, 0.7, 0.3, 1.1)` | Playful pop (hover previews, pins) |

Rules: animate `transform`/`opacity`, never layout. Everything honors
`prefers-reduced-motion: reduce` (transitions collapse to 1ms; Ken Burns and
map fly-to become static). Documented exceptions: spinner (1.1s linear),
Ken Burns (12s ease-in-out alternate).

## Components (inventory + composition)

- **Sidebar**: Chrome material; nav rows `--fs-body`/500, `--r-s`,
  active = `--accent-soft` fill + `--accent` label.
- **Event card**: `--r-m`, `--shadow-1` → `--shadow-2` on hover
  (`--dur-fast --ease-out`); title Headline, meta Footnote secondary;
  review dot appears on hover only.
- **Memories hero card**: 21:9, `--r-xl`, Ken Burns; overlay title Title 1
  on Thin material.
- **Sheets** (event/person/album/place): Overlay material, `--r-l`,
  `--shadow-3`, header Title 1, entry `--dur-slow --ease-out`.
- **Chips/pills**: Caption, `--r-pill`, `--fill` (or Thin material over
  imagery); status chips use status colors only for status.
- **Segmented control**: `--fill` track, `--card` raised thumb,
  `--shadow-1`, `--dur-fast`.
- **⌘K search**: Overlay material panel `--r-xl`, 16px field, sectioned
  results with Title 3 headers.
- **Map pins** (Places): thumbnail stack, 2px `--card` ring + `--shadow-2`;
  cluster = stacked covers + Caption count badge; approximate geocode =
  dashed ring; selection pop `--dur-fast --ease-spring`.

## Places map: rendering rationale

The map is a bundled-GeoJSON stylized vector map (Natural Earth 1:50m,
simplified, committed as a static asset) rendered by our own SVG layer —
not a tile service. Trade-off considered: MapLibre + OSM raster/vector tiles
gives street-level fidelity but pulls a runtime network dependency (against
local-first), an 800KB+ JS dependency, OSM tile-usage-policy constraints,
and a cartographic style we can't fully own. The archive's places resolve to
city granularity (Eugene, Moscow, Davos, Hilo) — country/state/city zoom is
sufficient, fully offline, and stylable to match this standard exactly
(hairline borders, `--bg-elev` landmass, `--accent` selection). If
street-level ever matters, MapLibre can slot behind the same component API.
