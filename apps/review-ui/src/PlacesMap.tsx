// Places map — Photos-style geographic view over the archive's geocoded
// places. Local-first by design: a bundled Natural Earth 110m vector base
// (world countries + US states) rendered as styled SVG, no tile service.
// See docs/DESIGN.md ("Places map: rendering rationale").
import React, { useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement } from "react";
import { feature } from "topojson-client";
import type { FeatureCollection, Geometry, Position } from "geojson";
import { ImageIcon, List, Map as MapIcon, MapPin, X } from "lucide-react";
import worldTopo from "world-atlas/countries-110m.json";
import usTopo from "us-atlas/states-10m.json";
import { assetUrl } from "./api";
import type { EventRecord, MediaRecord, PlaceRecord } from "./types";

const VIEW_W = 1000;
const VIEW_H = 560;
const LAT_CLAMP = 74; // poles excluded; keeps Alaska/Russia comfortably in frame
const CLUSTER_PX = 52;
const MIN_K = 1;
const MAX_K = 90;

type XY = { x: number; y: number };
type Camera = { k: number; cx: number; cy: number }; // zoom + center (map units)

function project(lat: number, lng: number): XY {
  const clamped = Math.max(-LAT_CLAMP, Math.min(LAT_CLAMP, lat));
  const x = ((lng + 180) / 360) * VIEW_W;
  const rad = (clamped * Math.PI) / 180;
  const merc = Math.log(Math.tan(Math.PI / 4 + rad / 2));
  const maxMerc = Math.log(Math.tan(Math.PI / 4 + (LAT_CLAMP * Math.PI) / 360 / (180 / Math.PI) / 2));
  void maxMerc;
  const mercMax = Math.log(Math.tan(Math.PI / 4 + (LAT_CLAMP * Math.PI) / 180 / 2));
  const y = (1 - merc / mercMax) * (VIEW_H / 2);
  return { x, y };
}

function ringPath(ring: Position[]): string {
  // Break the path when a segment wraps the antimeridian (a jump of more
  // than half the map width would otherwise draw a line across the world).
  let path = "";
  let previous: XY | null = null;
  for (const [lng, lat] of ring) {
    const point = project(lat as number, lng as number);
    if (!previous || Math.abs(point.x - previous.x) > VIEW_W / 2) {
      path += `M${point.x.toFixed(2)},${point.y.toFixed(2)}`;
    } else {
      path += `L${point.x.toFixed(2)},${point.y.toFixed(2)}`;
    }
    previous = point;
  }
  return path;
}

function ringMostlyClamped(ring: Position[]): boolean {
  const clamped = ring.filter(([, lat]) => Math.abs(lat as number) >= LAT_CLAMP - 0.5).length;
  return clamped / ring.length > 0.5;
}

function geometryPath(geometry: Geometry): string {
  if (geometry.type === "Polygon") {
    return geometry.coordinates.filter((ring) => !ringMostlyClamped(ring)).map(ringPath).join("");
  }
  if (geometry.type === "MultiPolygon") {
    return geometry.coordinates.map((polygon: Position[][]) => polygon.filter((ring) => !ringMostlyClamped(ring)).map(ringPath).join("")).join("");
  }
  return "";
}

function buildPaths(topo: unknown, objectName: string): string[] {
  const topology = topo as { objects: Record<string, never> };
  const collection = feature(
    topology as never,
    (topology.objects as Record<string, never>)[objectName],
  ) as unknown as FeatureCollection;
  return collection.features
    .map((item: { geometry: Geometry | null }) => (item.geometry ? geometryPath(item.geometry) : ""))
    .filter(Boolean);
}

interface MappedPlace {
  place: PlaceRecord;
  point: XY;
  cover?: string;
  approximate: boolean;
  count: number;
}

interface Cluster {
  key: string;
  places: MappedPlace[];
  point: XY;
  count: number;
}

interface Helpers {
  placeDisplayLabel: (raw?: string) => string;
  humanizeToken: (value: string) => string;
}

export function PlacesMapView({
  places,
  events,
  media,
  onPlay,
  openPlace,
  onOpenPlace,
  helpers,
  renderList,
}: {
  places: PlaceRecord[];
  events: EventRecord[];
  media: MediaRecord[];
  onPlay: (event: EventRecord) => void;
  openPlace: PlaceRecord | null;
  onOpenPlace: (place: PlaceRecord | null) => void;
  helpers: Helpers;
  renderList: () => ReactElement;
}) {
  const [mode, setMode] = useState<"map" | "list">("map");
  const eventsById = useMemo(() => new Map(events.map((event) => [event.id, event])), [events]);

  const worldPaths = useMemo(() => buildPaths(worldTopo, "countries"), []);
  const statePaths = useMemo(() => buildPaths(usTopo, "states"), []);

  const mapped = useMemo<MappedPlace[]>(() => {
    return places
      .filter(
        (place) =>
          place.coordinates &&
          place.kind === "named_place_candidate" &&
          (place.geocode?.confidence ?? 0) >= 0.6,
      )
      .map((place) => {
        const point = project(place.coordinates!.lat, place.coordinates!.lng);
        const firstEvent = (place.appearances || [])
          .map((entry) => eventsById.get(entry.event_id))
          .find((event) => event?.thumbnail_path || event?.keyframe_path);
        return {
          place,
          point,
          cover: firstEvent?.thumbnail_path || firstEvent?.keyframe_path || undefined,
          approximate: Boolean(place.geocode?.approximate),
          count: place.appearance_count || (place.appearances || []).length || 1,
        };
      });
  }, [places, eventsById]);

  const unplaced = useMemo(
    () =>
      places.filter(
        (place) =>
          place.kind === "named_place_candidate" &&
          (!place.coordinates || (place.geocode?.confidence ?? 0) < 0.6),
      ),
    [places],
  );

  const rollups = useMemo(() => {
    const byCity = new Map<string, { label: string; lat: number; lng: number; count: number; uncertain: boolean }>();
    for (const item of mapped) {
      const geo = item.place.geocode;
      const label = [geo?.city || geo?.state, geo?.country === "United States" ? geo?.state : geo?.country]
        .filter(Boolean)
        .filter((value, index, list) => list.indexOf(value) === index)
        .join(", ");
      if (!label) continue;
      const solid = !item.approximate && !geo?.context_suspect;
      const current = byCity.get(label) || {
        label,
        lat: item.place.coordinates!.lat,
        lng: item.place.coordinates!.lng,
        count: 0,
        uncertain: true,
      };
      current.count += item.count;
      // One confidently-placed member makes the region itself confident.
      current.uncertain = current.uncertain && !solid;
      byCity.set(label, current);
    }
    // Confident regions lead; approximate ones sink below the divider so the
    // rail never presents a guess with the same face as a fact.
    return [...byCity.values()].sort(
      (a, b) => Number(a.uncertain) - Number(b.uncertain) || b.count - a.count,
    );
  }, [mapped]);

  // Camera: start framed on the full pin set.
  const initialCamera = useMemo<Camera>(() => {
    if (!mapped.length) return { k: 1.6, cx: VIEW_W / 2, cy: VIEW_H / 2 };
    const xs = mapped.map((item) => item.point.x);
    const ys = mapped.map((item) => item.point.y);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const spanX = Math.max(maxX - minX, 40);
    const spanY = Math.max(maxY - minY, 40);
    // Generous framing: pins are ~54px boxes, so the fitted view needs real
    // margin or the outermost pin (Alaska) sits half-clipped at the edge.
    const k = Math.min(Math.max(Math.min(VIEW_W / (spanX * 1.85), VIEW_H / (spanY * 1.85)), MIN_K), 6);
    return { k, cx: (minX + maxX) / 2, cy: (minY + maxY) / 2 };
  }, [mapped]);

  const [camera, setCamera] = useState<Camera>(initialCamera);
  useEffect(() => setCamera(initialCamera), [initialCamera]);
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  const animRef = useRef<number | null>(null);
  const dragRef = useRef<{ x: number; y: number; cx: number; cy: number } | null>(null);

  function flyTo(lat: number, lng: number, k: number) {
    const target = project(lat, lng);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      setCamera({ k, cx: target.x, cy: target.y });
      return;
    }
    const from = { ...camera };
    const start = performance.now();
    const duration = 600;
    if (animRef.current) cancelAnimationFrame(animRef.current);
    const step = (now: number) => {
      const t = Math.min((now - start) / duration, 1);
      const ease = 1 - Math.pow(1 - t, 3);
      setCamera({
        k: from.k + (k - from.k) * ease,
        cx: from.cx + (target.x - from.cx) * ease,
        cy: from.cy + (target.y - from.cy) * ease,
      });
      if (t < 1) animRef.current = requestAnimationFrame(step);
    };
    animRef.current = requestAnimationFrame(step);
  }

  // Screen-space transform for a map point at the current camera.
  function toScreen(point: XY, width: number, height: number): XY {
    return {
      x: (point.x - camera.cx) * camera.k + width / 2,
      y: (point.y - camera.cy) * camera.k + height / 2,
    };
  }

  const clusters = useMemo<Cluster[]>(() => {
    const cell = CLUSTER_PX / camera.k;
    const buckets = new Map<string, MappedPlace[]>();
    for (const item of mapped) {
      const key = `${Math.round(item.point.x / cell)}:${Math.round(item.point.y / cell)}`;
      const bucket = buckets.get(key) || [];
      bucket.push(item);
      buckets.set(key, bucket);
    }
    return [...buckets.entries()].map(([key, members]) => ({
      key,
      places: members,
      point: {
        x: members.reduce((sum, member) => sum + member.point.x, 0) / members.length,
        y: members.reduce((sum, member) => sum + member.point.y, 0) / members.length,
      },
      count: members.reduce((sum, member) => sum + member.count, 0),
    }));
  }, [mapped, camera.k]);

  function onWheel(wheel: React.WheelEvent) {
    wheel.preventDefault();
    const surface = surfaceRef.current;
    if (!surface) return;
    const rect = surface.getBoundingClientRect();
    const factor = Math.exp(-wheel.deltaY * 0.0016);
    const nextK = Math.min(Math.max(camera.k * factor, MIN_K), MAX_K);
    // zoom toward cursor: keep the map point under the cursor stationary
    const px = wheel.clientX - rect.left;
    const py = wheel.clientY - rect.top;
    const mapX = camera.cx + (px - rect.width / 2) / camera.k;
    const mapY = camera.cy + (py - rect.height / 2) / camera.k;
    setCamera({
      k: nextK,
      cx: mapX - (px - rect.width / 2) / nextK,
      cy: mapY - (py - rect.height / 2) / nextK,
    });
  }

  function onPointerDown(pointer: React.PointerEvent) {
    (pointer.target as HTMLElement).setPointerCapture?.(pointer.pointerId);
    dragRef.current = { x: pointer.clientX, y: pointer.clientY, cx: camera.cx, cy: camera.cy };
  }

  function onPointerMove(pointer: React.PointerEvent) {
    const drag = dragRef.current;
    if (!drag) return;
    setCamera((current) => ({
      ...current,
      cx: drag.cx - (pointer.clientX - drag.x) / current.k,
      cy: drag.cy - (pointer.clientY - drag.y) / current.k,
    }));
  }

  function onPointerUp() {
    dragRef.current = null;
  }

  const surfaceSize = surfaceRef.current?.getBoundingClientRect();
  const width = surfaceSize?.width ?? 960;
  const height = surfaceSize?.height ?? 560;

  return (
    <section className="places-map-view">
      <header className="places-map-head">
        <h1>Places</h1>
        <div className="segmented" role="tablist" aria-label="Places display mode">
          <button role="tab" aria-selected={mode === "map"} className={mode === "map" ? "active" : ""} onClick={() => setMode("map")}>
            <MapIcon size={13} /> Map
          </button>
          <button role="tab" aria-selected={mode === "list"} className={mode === "list" ? "active" : ""} onClick={() => setMode("list")}>
            <List size={13} /> List
          </button>
        </div>
      </header>

      {mode === "list" ? (
        renderList()
      ) : (
        <div className="places-map-body">
          <div
            ref={surfaceRef}
            className="map-surface"
            tabIndex={0}
            onWheel={onWheel}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onKeyDown={(key) => {
              const pan = 60 / camera.k;
              if (key.key === "ArrowLeft") setCamera((c) => ({ ...c, cx: c.cx - pan }));
              else if (key.key === "ArrowRight") setCamera((c) => ({ ...c, cx: c.cx + pan }));
              else if (key.key === "ArrowUp") setCamera((c) => ({ ...c, cy: c.cy - pan }));
              else if (key.key === "ArrowDown") setCamera((c) => ({ ...c, cy: c.cy + pan }));
              else if (key.key === "+" || key.key === "=") setCamera((c) => ({ ...c, k: Math.min(c.k * 1.4, MAX_K) }));
              else if (key.key === "-") setCamera((c) => ({ ...c, k: Math.max(c.k / 1.4, MIN_K) }));
              else return;
              key.preventDefault();
            }}
          >
            <svg
              className="map-base"
              viewBox={`${camera.cx - width / 2 / camera.k} ${camera.cy - height / 2 / camera.k} ${width / camera.k} ${height / camera.k}`}
              preserveAspectRatio="xMidYMid slice"
            >
              <g>
                {worldPaths.map((d, index) => (
                  <path key={`c${index}`} d={d} className="map-country" vectorEffect="non-scaling-stroke" />
                ))}
                {camera.k > 2.2 &&
                  statePaths.map((d, index) => (
                    <path key={`s${index}`} d={d} className="map-state" vectorEffect="non-scaling-stroke" />
                  ))}
              </g>
            </svg>

            <div className="map-pins">
              {clusters.map((cluster) => {
                const screen = toScreen(cluster.point, width, height);
                if (screen.x < -60 || screen.x > width + 60 || screen.y < -60 || screen.y > height + 60) return null;
                const primary = cluster.places[0];
                const single = cluster.places.length === 1;
                return (
                  <button
                    key={cluster.key}
                    className={`map-pin${single && primary.approximate ? " approximate" : ""}${single ? "" : " cluster"}`}
                    style={{ transform: `translate(${screen.x}px, ${screen.y}px)` }}
                    title={
                      single
                        ? helpers.placeDisplayLabel(primary.place.display_label)
                        : `${cluster.places.length} places · ${cluster.count} moments`
                    }
                    onClick={() => {
                      if (single) onOpenPlace(primary.place);
                      else flyTo(
                        primary.place.coordinates!.lat,
                        primary.place.coordinates!.lng,
                        Math.min(camera.k * 2.6, MAX_K),
                      );
                    }}
                  >
                    <span className="pin-frame">
                      {primary.cover ? <img src={assetUrl(primary.cover)} alt="" loading="lazy" /> : <MapPin size={14} />}
                    </span>
                    {(cluster.count > 1 || !single) && <em>{single ? cluster.count : cluster.places.length}</em>}
                  </button>
                );
              })}
            </div>
          </div>

          <aside className="map-rail">
            <h2>By region</h2>
            <div className="map-rollups">
              {rollups.map((rollup, index) => (
                <React.Fragment key={rollup.label}>
                  {rollup.uncertain && (index === 0 || !rollups[index - 1].uncertain) && (
                    <div className="rollup-divider">Approximate</div>
                  )}
                  <button
                    className={rollup.uncertain ? "uncertain" : ""}
                    title={rollup.uncertain ? "Location is a best guess until its context is confirmed" : undefined}
                    onClick={() => flyTo(rollup.lat, rollup.lng, 14)}
                  >
                    <span>{rollup.label}</span>
                    <em>{rollup.count === 1 ? "1 moment" : `${rollup.count} moments`}</em>
                  </button>
                </React.Fragment>
              ))}
            </div>
            {unplaced.length > 0 && (
              <details className="map-unplaced">
                <summary>Not confidently placed · {unplaced.length}</summary>
                <div>
                  {unplaced.map((place) => (
                    <button key={place.id} onClick={() => onOpenPlace(place)}>
                      {helpers.placeDisplayLabel(place.display_label)}
                    </button>
                  ))}
                </div>
              </details>
            )}
          </aside>
        </div>
      )}

      {openPlace && (
        <PlaceSheet
          place={openPlace}
          eventsById={eventsById}
          media={media}
          helpers={helpers}
          onClose={() => onOpenPlace(null)}
          onPlay={onPlay}
        />
      )}
    </section>
  );
}

function PlaceSheet({
  place,
  eventsById,
  media,
  helpers,
  onClose,
  onPlay,
}: {
  place: PlaceRecord;
  eventsById: Map<string, EventRecord>;
  media: MediaRecord[];
  helpers: Helpers;
  onClose: () => void;
  onPlay: (event: EventRecord) => void;
}) {
  void media;
  const placeEvents = (place.appearances || [])
    .map((entry) => eventsById.get(entry.event_id))
    .filter((event): event is EventRecord => Boolean(event));
  const hero = placeEvents.find((event) => event.thumbnail_path || event.keyframe_path);
  const geo = place.geocode;
  const subtitle = [
    geo?.formatted_address,
    geo?.approximate ? "approximate location" : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="sheet-scrim" onClick={onClose}>
      <article className="sheet place-sheet" onClick={(click) => click.stopPropagation()}>
        <button className="sheet-close" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
        {hero && (
          <div className="sheet-hero">
            <img src={assetUrl(hero.thumbnail_path || hero.keyframe_path)} alt="" />
          </div>
        )}
        <div className="sheet-body">
          <header>
            <h1>{helpers.placeDisplayLabel(place.display_label)}</h1>
            {subtitle && <p className="place-subtitle">{subtitle}</p>}
            {place.place_type && <p className="place-kind">{helpers.humanizeToken(place.place_type)}</p>}
          </header>
          <div className="place-sheet-events">
            {placeEvents.map((event) => (
              <button key={event.id} className="place-event" onClick={() => onPlay(event)}>
                {event.thumbnail_path || event.keyframe_path ? (
                  <img src={assetUrl(event.thumbnail_path || event.keyframe_path)} alt="" loading="lazy" />
                ) : (
                  <span className="card-fallback">
                    <ImageIcon size={18} />
                  </span>
                )}
                <span className="place-event-title">{event.title}</span>
              </button>
            ))}
            {!placeEvents.length && <p className="place-empty">No linked moments yet.</p>}
          </div>
        </div>
      </article>
    </div>
  );
}
