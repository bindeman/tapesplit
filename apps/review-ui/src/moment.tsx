// The moment sheet's evidence: when it was filmed and how TapeSplit knows,
// where, and who is talking. Each pane shows the evidence itself (the frame a
// date was read from, the clues behind a place, the voices on the
// soundtrack), the way the website's How It Works chapters do.
import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { AudioLines, CalendarDays, MapPin, Play } from "lucide-react";
import { assetUrl, loadJournalPosts, loadStamps } from "./api";
import { MiniMap } from "./PlacesMap";
import type { MiniMapPin } from "./PlacesMap";
import { formatTime, humanVoiceLabel, placeDisplayLabel } from "./format";
import type {
  ContinuityContextAsset,
  DateRef,
  EventRecord,
  MediaRecord,
  PlaceRecord,
  PlaceRoleAsset,
  SpeakerSegment,
  StampFrame,
} from "./types";

export type MomentRange = { videoId: string; start: number; end: number };

type PlayFrom = (videoId: string, startS: number, endS?: number) => void;

const LONG_MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

// Source seconds on the moment's tape. Reconciled ranges win; otherwise the
// archive-timeline span is shifted back by the tape's offset.
export function momentRange(event: EventRecord, media: MediaRecord[]): MomentRange | null {
  const videoId = event.source_video_ids[0];
  if (!videoId) {
    return null;
  }
  const tape = media.find((row) => row.id === videoId);
  const offset = tape?.offset_s ?? 0;
  const duration = tape?.duration_s ?? 0;
  const local = (value: number) => (offset > 0 && duration > 0 && value > duration + 5 ? value - offset : value);
  const ranges = (event.reconciliation?.selected_source_ranges ?? []).filter(
    (range) => range.source_video_id === videoId && typeof range.start_s === "number",
  );
  if (ranges.length) {
    const start = Math.min(...ranges.map((range) => local(range.start_s ?? 0)));
    const end = Math.max(...ranges.map((range) => local(range.end_s ?? range.start_s ?? 0)));
    return { videoId, start: Math.max(0, start), end: Math.max(start, end) };
  }
  const start = Math.max(0, (event.start_s ?? offset) - offset);
  const end = typeof event.end_s === "number" ? Math.max(start, event.end_s - offset) : start + 60;
  return { videoId, start, end };
}

// The date a moment is filed under, if it has one it can stand behind.
export function momentDate(event: EventRecord): DateRef | undefined {
  const usable = event.dates.filter((date) => date.date_value && !date.excluded_as_event_date);
  return usable.find((date) => date.precision === "day") ?? usable[0];
}

function longDate(value: string): string {
  const match = /^((?:19|20)\d{2})(?:-(\d{2}))?(?:-(\d{2}))?/.exec(value.trim());
  if (!match) {
    return value;
  }
  const [, year, month, day] = match;
  if (month && day) {
    const weekday = WEEKDAYS[new Date(Date.UTC(Number(year), Number(month) - 1, Number(day))).getUTCDay()];
    return `${weekday}, ${LONG_MONTHS[Number(month) - 1]} ${Number(day)}, ${year}`;
  }
  if (month) {
    return `${LONG_MONTHS[Number(month) - 1]} ${year}`;
  }
  return year;
}

// "SEP 7 2005": the way the camcorder itself printed it, for the label tag.
export function stampLabel(value?: string): string {
  const match = /^((?:19|20)\d{2})-(\d{2})-(\d{2})/.exec(value ?? "");
  if (!match) {
    return "";
  }
  return `${LONG_MONTHS[Number(match[2]) - 1].slice(0, 3).toUpperCase()} ${Number(match[3])} ${match[1]}`;
}

function pct(value: number) {
  return `${Math.min(100, Math.max(0, value * 100)).toFixed(3)}%`;
}

// ------------------------------------------------------------------ when

function StampFrameView({ frame, range }: { frame: StampFrame; range: MomentRange | null }) {
  const where =
    !range || (frame.time_s >= range.start - 1 && frame.time_s <= range.end + 1)
      ? ""
      : frame.time_s < range.start
        ? range.start - frame.time_s < 120
          ? ", just before this moment"
          : ", earlier on this tape"
        : range.end - frame.time_s > -120
          ? ", just after this moment"
          : ", later on this tape";
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  return (
    <figure className="stamp-frame">
      <div className="stamp-pic">
        <img
          src={assetUrl(frame.image)}
          alt="The frame the date was read from"
          onLoad={(loadEvent) => setSize({ w: loadEvent.currentTarget.naturalWidth, h: loadEvent.currentTarget.naturalHeight })}
        />
        {size &&
          frame.boxes.map((box, index) => (
            <span
              key={`${box.text}-${index}`}
              className="ocr-box"
              style={
                {
                  left: pct(box.x / size.w),
                  top: pct(box.y / size.h),
                  width: pct(box.width / size.w),
                  height: pct(box.height / size.h),
                  "--i": index,
                } as CSSProperties
              }
            />
          ))}
      </div>
      <figcaption>
        Apple Vision read{" "}
        {[...frame.boxes]
          .sort((a, b) => (Math.abs(a.y - b.y) > 12 ? a.y - b.y : a.x - b.x))
          .map((box) => `“${box.text}”`)
          .join(" ")}{" "}
        at <code>{formatTime(frame.time_s)}</code>
        {where}
      </figcaption>
    </figure>
  );
}

export function WhenPane({ event, range }: { event: EventRecord; range: MomentRange | null }) {
  const date = momentDate(event);
  const mentioned = event.dates.filter((entry) => entry.excluded_as_event_date && (entry.label || entry.date_value));
  const [frames, setFrames] = useState<StampFrame[] | null>(null);
  const day = date?.precision === "day" ? date.date_value : undefined;
  const rangeKey = range ? `${range.videoId}:${range.start}:${range.end}:${day ?? ""}` : "";

  useEffect(() => {
    if (!range) {
      setFrames([]);
      return;
    }
    let live = true;
    setFrames(null);
    loadStamps(range.videoId, range.start, range.end, day)
      .then((next) => live && setFrames(next))
      .catch(() => live && setFrames([]));
    return () => {
      live = false;
    };
    // rangeKey captures the range's identity
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rangeKey]);

  const fits = date?.capture_window_check === "inside";
  let source = "Not dated yet";
  let basis = "Nothing in this moment says when it was filmed. The moments around it on the tape set its year.";
  if (date?.origin === "overlay_datestamp") {
    source = "Camcorder date stamp";
    basis = "Apple Vision read it off the date the camcorder burned into the picture.";
  } else if (date?.origin === "narrated_current") {
    source = "Seen or heard in the moment";
    basis = `The video model read or heard this date in the moment${fits ? ", and it fits this tape's other dates" : ""}.`;
  } else if (date) {
    source = "Found in the moment";
    basis = fits ? "It fits this tape's other dates." : "";
  }
  const frame = frames?.[0];

  return (
    <section className="ev-card ev-when" aria-label="When">
      <p className="ev-source">
        <CalendarDays size={13} />
        When · {source}
      </p>
      <p className="ev-big">{date?.date_value ? longDate(date.date_value) : "Undated"}</p>
      {basis && <p className="ev-basis">{basis}</p>}
      {frame && <StampFrameView frame={frame} range={range} />}
      {frames === null && <div className="stamp-skeleton" aria-hidden="true" />}
      {mentioned.length > 0 && (
        <div className="ev-struck" aria-label="Dates that don't count">
          {mentioned.slice(0, 3).map((entry) => (
            <span key={entry.id} title="Mentioned in the moment, not the day it was filmed">
              <s>{entry.label || entry.date_value}</s> mentioned, not filmed
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ where

const ROLE_LABELS: Record<string, string> = {
  explicit_location_anchor: "Named in the moment",
  visible_place: "Seen on screen",
  generic_scene_type: "What the scene looks like",
  administrative_context: "City or region",
  ambiguous_place_reference: "Mentioned",
  travel_plan: "A trip talked about",
};

function normalize(value?: string) {
  return (value ?? "").toLowerCase().replace(/\s*\([^)]*\)\s*$/, "").trim();
}

// The most telling short line behind a place: a sentence with a few words in
// it, not the place's own name or the moment's title echoed back.
function shortClue(role: PlaceRoleAsset | undefined, label: string, title: string): string {
  const name = normalize(label);
  const texts = (role?.evidence_texts ?? [])
    .map((text) => text.trim().replace(/^\d{1,2}:\d{2}\s*-\s*/, ""))
    .filter((text) => {
      const plain = normalize(text);
      return (
        text.length <= 120 &&
        text.split(/\s+/).length >= 3 &&
        plain !== name &&
        plain !== normalize(title) &&
        !(plain.length < name.length + 12 && (plain.includes(name) || name.includes(plain)))
      );
    });
  return texts.sort((a, b) => a.length - b.length)[0] ?? "";
}

export function WherePane({
  event,
  places,
  roles,
  continuity,
}: {
  event: EventRecord;
  places: PlaceRecord[];
  roles: PlaceRoleAsset[];
  continuity: ContinuityContextAsset[];
}) {
  const placesById = useMemo(() => new Map(places.map((place) => [place.id, place])), [places]);
  const eventRoles = roles.filter((role) => role.canonical_event_id === event.id);
  const rows = event.places.slice(0, 4).map((ref) => {
    const place = placesById.get(ref.id);
    const role = eventRoles.find((candidate) => normalize(candidate.label) === normalize(place?.label ?? ref.label));
    return { ref, place, role, label: placeDisplayLabel(place?.display_label ?? ref.label) };
  });
  // Same bar as the Places map: a geocode under 0.6 is a guess, not a pin.
  const pins: MiniMapPin[] = [];
  let unsure = 0;
  for (const row of rows) {
    const coords = row.place?.coordinates;
    if (coords && (row.place?.geocode?.confidence ?? 0) < 0.6) {
      unsure += 1;
    }
    if (coords && (row.place?.geocode?.confidence ?? 0) >= 0.6) {
      pins.push({
        lat: coords.lat,
        lng: coords.lng,
        label: row.label,
        approximate: Boolean(row.place?.geocode?.approximate) || row.role?.role === "administrative_context",
      });
    }
  }
  const carried = continuity
    .filter((row) => row.canonical_event_id === event.id)
    .map((row) => (Array.isArray(row.supporting_signals) ? row.supporting_signals[0] : row.supporting_signals) ?? "")
    .filter(Boolean)
    .slice(0, 2);

  return (
    <section className="ev-card ev-where" aria-label="Where">
      <p className="ev-source">
        <MapPin size={13} />
        Where
      </p>
      {rows.length ? (
        <ul className="ev-places">
          {rows.map((row) => {
            const clue = shortClue(row.role, row.place?.label ?? row.ref.label, event.title);
            return (
              <li key={row.ref.id}>
                <strong>{row.label}</strong>
                <span className="ev-role">{ROLE_LABELS[row.role?.role ?? ""] ?? "Linked to this moment"}</span>
                {clue && <q>{clue}</q>}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="ev-basis">No place yet. Nothing on screen or on the soundtrack names one.</p>
      )}
      {carried.map((note) => (
        <p key={note} className="ev-basis">
          {note}
        </p>
      ))}
      {pins.length > 0 && <MiniMap pins={pins} label={`Map of ${pins.map((pin) => pin.label).join(", ")}`} />}
      {pins.length === 0 && unsure > 0 && (
        <p className="ev-basis">Not on the map yet: the geocoder's best guess is too uncertain to pin.</p>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ voices

const CYRILLIC = /[А-Яа-яЁё]/g;
const LATIN = /[A-Za-z]/g;

// A transcript line's language, by script: these tapes mix Russian and
// English, often mid-sentence.
export function lineLanguage(text: string): "ru" | "en" | "mixed" | "" {
  const cyrillic = (text.match(CYRILLIC) ?? []).length;
  const latin = (text.match(LATIN) ?? []).length;
  if (!cyrillic && !latin) return "";
  if (cyrillic >= 3 && latin >= 3) return "mixed";
  return cyrillic > latin ? "ru" : "en";
}

// One pill per language heard in the line; a line that switches gets both.
export function LanguagePills({ language }: { language: ReturnType<typeof lineLanguage> }) {
  if (!language) return null;
  const pills = language === "mixed" ? ["ru", "en"] : [language];
  return (
    <>
      {pills.map((code) => (
        <span key={code} className={`lang-pill ${code}`}>
          {code.toUpperCase()}
        </span>
      ))}
    </>
  );
}

function voiceName(label: string | undefined, videoId: string, media: MediaRecord[]): string {
  const raw = label ?? "";
  if (/^AZ_SPEAKER_\d+$/.test(raw)) {
    return humanVoiceLabel(`${raw}@${videoId}`, media)?.replace(/ · Tape \d+$/, "") ?? raw;
  }
  return humanVoiceLabel(raw, media) ?? raw;
}

export function VoicesPane({
  range,
  segments,
  media,
  onPlayFrom,
}: {
  range: MomentRange | null;
  segments: SpeakerSegment[];
  media: MediaRecord[];
  onPlayFrom: PlayFrom;
}) {
  const [translations, setTranslations] = useState<Map<string, string>>(new Map());
  useEffect(() => {
    let live = true;
    loadJournalPosts()
      .then((posts) => {
        const map = new Map<string, string>();
        for (const post of posts) {
          for (const block of post.blocks ?? []) {
            if (block.type === "pullquote" && block.segment_id && block.translation) {
              map.set(block.segment_id, block.translation);
            }
          }
        }
        if (live) setTranslations(map);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  const lines = useMemo(() => {
    if (!range) return [];
    return segments
      .filter(
        (segment) =>
          segment.source_video_id === range.videoId &&
          typeof segment.start_s === "number" &&
          segment.start_s >= range.start - 0.5 &&
          segment.start_s <= range.end + 0.5 &&
          (segment.metadata?.transcript_text ?? "").trim().length > 0,
      )
      .sort((a, b) => (a.start_s ?? 0) - (b.start_s ?? 0));
  }, [range, segments]);

  const voices = useMemo(() => {
    const talk = new Map<string, number>();
    for (const line of lines) {
      const label = line.speaker_label ?? "";
      talk.set(label, (talk.get(label) ?? 0) + Math.max(0.3, (line.end_s ?? line.start_s ?? 0) - (line.start_s ?? 0)));
    }
    return [...talk.entries()].sort((a, b) => b[1] - a[1]).map(([label]) => label);
  }, [lines]);

  if (!range || !lines.length) {
    return null;
  }
  const lanes = voices.slice(0, 3);
  const laneOf = (label?: string) => {
    const index = lanes.indexOf(label ?? "");
    return index === -1 ? 3 : index;
  };
  const span = Math.max(1, range.end - range.start);
  const shown = lines.slice(0, 200);

  return (
    <section className="ev-card ev-voices" aria-label="Who's talking">
      <p className="ev-source">
        <AudioLines size={13} />
        Voices · {voices.length === 1 ? "1 voice" : `${voices.length} voices`} · {lines.length === 1 ? "1 line" : `${lines.length} lines`}
      </p>
      <div className="lanes" role="img" aria-label={`When each voice speaks across the moment: ${lanes.map((label) => voiceName(label, range.videoId, media)).join(", ")}`}>
        {[...lanes, ...(voices.length > 3 ? ["__other"] : [])].map((label, lane) => (
          <div key={label} className={`lane v${lane}`}>
            <span className="lane-name">{label === "__other" ? "Others" : voiceName(label, range.videoId, media)}</span>
            <span className="lane-track">
              {lines
                .filter((line) => (label === "__other" ? laneOf(line.speaker_label) === 3 : line.speaker_label === label))
                .map((line) => (
                  <i
                    key={line.id}
                    style={{
                      left: pct(((line.start_s ?? 0) - range.start) / span),
                      width: `max(2px, ${pct(((line.end_s ?? line.start_s ?? 0) - (line.start_s ?? 0)) / span)})`,
                    }}
                  />
                ))}
            </span>
          </div>
        ))}
      </div>
      <ol className="transcript">
        {shown.map((line) => {
          const text = (line.metadata?.transcript_text ?? "").trim();
          const language = lineLanguage(text);
          const translation = translations.get(line.id);
          return (
            <li key={line.id} className={`v${laneOf(line.speaker_label)}`}>
              <button
                className="line-play"
                onClick={() => onPlayFrom(range.videoId, Math.max(0, (line.start_s ?? 0) - 0.6), line.end_s)}
                aria-label={`Play from ${formatTime(line.start_s)}`}
              >
                <Play size={10} fill="currentColor" />
                <code>{formatTime(line.start_s)}</code>
              </button>
              <div className="line-body">
                <span className="line-who">{voiceName(line.speaker_label, range.videoId, media)}</span>
                <p lang={language === "ru" || language === "en" ? language : undefined}>
                  <LanguagePills language={language} />
                  {text}
                </p>
                {translation && <p className="line-gloss">{translation}</p>}
              </div>
            </li>
          );
        })}
      </ol>
      {lines.length > shown.length && <p className="ev-basis">And {lines.length - shown.length} more lines in this moment.</p>}
    </section>
  );
}
