import {
  AlertTriangle,
  Album,
  CalendarDays,
  Check,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Film,
  GitMerge,
  ImageIcon,
  Info,
  Inbox,
  LayoutGrid,
  ListFilter,
  MapPin,
  MapPinned,
  Pencil,
  Play,
  RefreshCw,
  Search,
  Send,
  Trash2,
  UserCheck,
  Users,
  X,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, ReactNode } from "react";
import {
  applyReviewActions,
  applySuggestedReviewActions,
  assetUrl,
  loadProject,
  queueReviewAction,
  reapplyReviewCorrections,
  removeReviewAction,
  searchProject,
  videoUrl,
} from "./api";
import type {
  AlbumRecord,
  DateRef,
  EventEntry,
  EventRecord,
  FaceObservation,
  MediaRecord,
  PersonRecord,
  PlaceContext,
  PlaceLocationOption,
  PlaceRecord,
  ProjectBundle,
  ReviewAction,
  ReviewItem,
  SceneRecord,
  SearchResult,
  SourceRange,
  SpeakerIdentityCandidate,
  SuggestedReviewAction,
  TaskType,
  VisualAsset,
} from "./types";

type ViewMode = "library" | "people" | "places" | "albums" | "search";
type ReviewScope = "primary" | "backlog" | "all";

type PlayerMoment = {
  videoId: string;
  videoLabel: string;
  startS: number;
  endS?: number;
  title: string;
};

const taskLabels: Record<string, string> = {
  resolve_face_cluster: "Faces",
  resolve_speaker: "Speakers",
  confirm_place_context: "Place Links",
  confirm_relationship: "Relationships",
  resolve_place: "Places",
  resolve_person: "People",
  review_event: "Events",
  resolve_date: "Dates",
};

const viewLabels: Array<{ id: ViewMode; label: string; icon: typeof Inbox }> = [
  { id: "library", label: "Library", icon: LayoutGrid },
  { id: "people", label: "People", icon: Users },
  { id: "places", label: "Places", icon: MapPinned },
  { id: "albums", label: "Albums", icon: Album },
  { id: "search", label: "Search", icon: Search },
];

const initialView = ((): ViewMode => {
  const requested = new URLSearchParams(window.location.search).get("view");
  return (viewLabels.some((item) => item.id === requested) ? requested : "library") as ViewMode;
})();

export function App() {
  const [bundle, setBundle] = useState<ProjectBundle | null>(null);
  const [view, setView] = useState<ViewMode>(initialView);
  const [reviewScope, setReviewScope] = useState<ReviewScope>("primary");
  const [taskFilter, setTaskFilter] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchBusy, setSearchBusy] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [selectedId, setSelectedId] = useState<string>("");
  const [activeMoment, setActiveMoment] = useState<PlayerMoment | null>(null);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(() => new URLSearchParams(window.location.search).has("review"));
  const [openEvent, setOpenEvent] = useState<EventRecord | null>(null);
  const [openPerson, setOpenPerson] = useState<PersonRecord | null>(null);
  const [openAlbum, setOpenAlbum] = useState<AlbumRecord | null>(null);

  async function refresh(nextStatus = "") {
    setBusy(true);
    setError("");
    try {
      const next = await loadProject();
      setBundle(next);
      setStatus(nextStatus);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  const deepLinkedRef = useRef(false);
  useEffect(() => {
    if (!bundle || deepLinkedRef.current) {
      return;
    }
    deepLinkedRef.current = true;
    const params = new URLSearchParams(window.location.search);
    const eventId = params.get("event");
    if (eventId) {
      const match = bundle.data.timeline.events.find((event) => event.id === eventId);
      if (match) {
        setOpenEvent(match);
        if (params.has("play")) {
          playEvent(match, bundle.data.media, setActiveMoment);
        }
      }
    }
  }, [bundle]);

  const navigableEvents = useMemo(
    () => splitByRelatedness(bundle?.data.timeline.events ?? []).related,
    [bundle],
  );

  useEffect(() => {
    function onKeyDown(keyEvent: KeyboardEvent) {
      const target = keyEvent.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable)
      ) {
        return;
      }
      if (keyEvent.key === "Escape") {
        if (activeMoment) setActiveMoment(null);
        else if (openEvent) setOpenEvent(null);
        else if (openAlbum) setOpenAlbum(null);
        else if (openPerson) setOpenPerson(null);
        else if (reviewOpen) setReviewOpen(false);
        return;
      }
      if ((keyEvent.key === "ArrowLeft" || keyEvent.key === "ArrowRight") && openEvent) {
        const index = navigableEvents.findIndex((event) => event.id === openEvent.id);
        if (index === -1) {
          return;
        }
        const next = navigableEvents[index + (keyEvent.key === "ArrowRight" ? 1 : -1)];
        if (next) {
          keyEvent.preventDefault();
          setOpenEvent(next);
        }
        return;
      }
      if (keyEvent.key === " ") {
        const video = document.querySelector<HTMLVideoElement>(".miniplayer video");
        if (video) {
          keyEvent.preventDefault();
          if (video.paused) void video.play().catch(() => undefined);
          else video.pause();
        } else if (openEvent && bundle) {
          keyEvent.preventDefault();
          playEvent(openEvent, bundle.data.media, setActiveMoment);
        }
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [activeMoment, openEvent, openAlbum, openPerson, reviewOpen, navigableEvents, bundle]);

  const primaryReviewItems = bundle?.data.review_queue ?? [];
  const reviewBacklog = bundle?.data.review_backlog ?? [];
  const reviewItems = useMemo(() => {
    if (reviewScope === "backlog") {
      return reviewBacklog;
    }
    if (reviewScope === "all") {
      return [...primaryReviewItems, ...reviewBacklog];
    }
    return primaryReviewItems;
  }, [primaryReviewItems, reviewBacklog, reviewScope]);
  const pendingActions = bundle?.pendingActions ?? [];
  const taskCounts = useMemo(() => countBy(reviewItems, (item) => item.task_type), [reviewItems]);
  const suggestedActionCount = useMemo(() => countSuggestedActions(reviewItems), [reviewItems]);

  const filteredReviewItems = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return reviewItems.filter((item) => {
      if (taskFilter !== "all" && item.task_type !== taskFilter) {
        return false;
      }
      if (!normalizedQuery) {
        return true;
      }
      return `${item.title} ${item.task_type} ${item.events.map((event) => event.title).join(" ")}`
        .toLowerCase()
        .includes(normalizedQuery);
    });
  }, [query, reviewItems, taskFilter]);

  const selectedItem = filteredReviewItems.find((item) => item.id === selectedId) ?? filteredReviewItems[0] ?? null;

  useEffect(() => {
    if (!selectedItem) {
      setSelectedId("");
    } else if (selectedItem.id !== selectedId) {
      setSelectedId(selectedItem.id);
    }
  }, [selectedId, selectedItem]);

  async function queueAction(action: ReviewAction | ReviewAction[]) {
    setBusy(true);
    setError("");
    try {
      const next = await queueReviewAction(action);
      setBundle(next);
      setStatus(Array.isArray(action) ? `${action.length} actions queued` : `${action.action} queued`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function removeAction(id?: string) {
    setBusy(true);
    setError("");
    try {
      const next = await removeReviewAction(id);
      setBundle(next);
      setStatus(id ? "Action removed" : "Pending actions cleared");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function applyActions() {
    setBusy(true);
    setError("");
    try {
      const next = await applyReviewActions();
      setBundle(next);
      setStatus("Corrections applied and refreshed");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function applyBestGuesses() {
    setBusy(true);
    setError("");
    try {
      const next = await applySuggestedReviewActions(reviewScope);
      setBundle(next);
      setStatus(`Accepted best guesses for ${reviewScope} review`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function reapplyCorrections() {
    setBusy(true);
    setError("");
    try {
      const next = await reapplyReviewCorrections();
      setBundle(next);
      setStatus("Corrections replayed");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function runSearch(nextQuery = searchQuery) {
    const trimmed = nextQuery.trim();
    if (!trimmed) {
      setSearchResults([]);
      setSearchError("");
      return;
    }
    setSearchBusy(true);
    setSearchError("");
    try {
      const response = await searchProject(trimmed, 16);
      setSearchResults(response.results);
    } catch (err) {
      setSearchError(err instanceof Error ? err.message : String(err));
    } finally {
      setSearchBusy(false);
    }
  }

  if (!bundle) {
    return (
      <div className="boot">
        <RefreshCw className="spin" size={20} />
        <span>{error || "Loading project"}</span>
      </div>
    );
  }

  const reviewBadge = primaryReviewItems.length;

  return (
    <div className="photos-shell">
      <aside className="sidebar">
        <header className="sidebar-brand">
          <div className="brand-mark">
            <Film size={17} />
          </div>
          <div className="brand-copy">
            <strong>Tapes</strong>
            <span>{shortPath(bundle.projectDir)}</span>
          </div>
        </header>

        <nav className="sidebar-nav" aria-label="Views">
          {viewLabels.map((item) => {
            const Icon = item.icon;
            return (
              <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}>
                <Icon size={17} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <button className={`review-entry ${reviewOpen ? "active" : ""}`} onClick={() => setReviewOpen(true)}>
            <Inbox size={16} />
            <span>Review</span>
            {reviewBadge > 0 && <em>{reviewBadge}</em>}
          </button>
          <button className="sidebar-refresh" onClick={() => void refresh("Reloaded")} disabled={busy}>
            <RefreshCw size={14} className={busy ? "spin" : ""} />
            <span>Refresh</span>
          </button>
        </div>
      </aside>

      <main className="stage">
        {(status || error) && (
          <div className={`toast ${error ? "error" : ""}`}>
            {error ? <AlertTriangle size={14} /> : <CheckCircle2 size={14} />}
            <span>{error || status}</span>
            <button onClick={() => (error ? setError("") : setStatus(""))} aria-label="Dismiss">
              <X size={13} />
            </button>
          </div>
        )}

        {view === "library" && (
          <LibraryView
            events={bundle.data.timeline.events}
            media={bundle.data.media}
            albums={bundle.data.tracks.albums}
            summary={bundle.data.summary}
            onOpen={setOpenEvent}
            onOpenAlbum={setOpenAlbum}
          />
        )}
        {view === "people" && (
          <PeopleWall people={bundle.data.people} onSelect={setOpenPerson} />
        )}
        {view === "albums" && (
          <AlbumsView albums={bundle.data.tracks.albums} events={bundle.data.timeline.events} media={bundle.data.media} onPlay={setActiveMoment} />
        )}
        {view === "places" && (
          <PlacesView
            contexts={bundle.data.place_contexts}
            places={bundle.data.places}
            events={bundle.data.timeline.events}
            media={bundle.data.media}
            onPlay={setActiveMoment}
          />
        )}
        {view === "search" && (
          <SearchView
            query={searchQuery}
            results={searchResults}
            busy={searchBusy}
            error={searchError}
            media={bundle.data.media}
            onQueryChange={setSearchQuery}
            onSearch={runSearch}
            onPlay={setActiveMoment}
          />
        )}
      </main>

      {openEvent && (
        <EventSheet
          event={openEvent}
          scenes={bundle.data.timeline.scenes}
          media={bundle.data.media}
          people={bundle.data.people}
          reviewItems={[...primaryReviewItems, ...reviewBacklog]}
          onClose={() => setOpenEvent(null)}
          onPlay={setActiveMoment}
          onOpenReview={(itemId) => {
            setSelectedId(itemId);
            setReviewOpen(true);
          }}
        />
      )}

      {openPerson && (
        <PersonSheet
          person={openPerson}
          events={bundle.data.timeline.events}
          media={bundle.data.media}
          onClose={() => setOpenPerson(null)}
          onOpenEvent={(event) => {
            setOpenPerson(null);
            setOpenEvent(event);
          }}
          onPlay={setActiveMoment}
        />
      )}

      {openAlbum && (
        <AlbumSheet
          album={openAlbum}
          events={bundle.data.timeline.events}
          media={bundle.data.media}
          onClose={() => setOpenAlbum(null)}
          onOpenEvent={(event) => {
            setOpenAlbum(null);
            setOpenEvent(event);
          }}
        />
      )}

      {reviewOpen && (
        <div className="drawer-scrim" onClick={() => setReviewOpen(false)}>
          <aside className="review-drawer" onClick={(event) => event.stopPropagation()}>
            <header className="drawer-head">
              <div>
                <h2>Review</h2>
                <p>Confirm or correct the archive’s remaining guesses</p>
              </div>
              <button className="icon-button" onClick={() => setReviewOpen(false)} aria-label="Close review">
                <X size={16} />
              </button>
            </header>

            <div className="queue-tools">
              <label className="search-box">
                <Search size={15} />
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter review items" />
              </label>
              <div className="task-filter">
                <Inbox size={15} />
                <select value={reviewScope} onChange={(event) => setReviewScope(event.target.value as ReviewScope)}>
                  <option value="primary">Primary ({primaryReviewItems.length})</option>
                  <option value="backlog">Backlog ({reviewBacklog.length})</option>
                  <option value="all">All ({primaryReviewItems.length + reviewBacklog.length})</option>
                </select>
              </div>
              <div className="task-filter">
                <ListFilter size={15} />
                <select value={taskFilter} onChange={(event) => setTaskFilter(event.target.value)}>
                  <option value="all">All types</option>
                  {Object.entries(taskCounts).map(([task, count]) => (
                    <option key={task} value={task}>
                      {taskLabel(task)} ({count})
                    </option>
                  ))}
                </select>
              </div>
              <button className="command-button queue-command" disabled={busy || !suggestedActionCount} onClick={() => void applyBestGuesses()}>
                <CheckCircle2 size={16} />
                <span>Accept Best Guesses</span>
              </button>
            </div>

            <div className="drawer-columns">
              <ReviewQueue
                items={filteredReviewItems}
                selectedId={selectedItem?.id ?? ""}
                pendingActions={pendingActions}
                onSelect={(item) => setSelectedId(item.id)}
              />
              <div className="drawer-detail">
                {selectedItem ? (
                  <ReviewDetail
                    key={selectedItem.id}
                    item={selectedItem}
                    pending={itemHasPendingAction(selectedItem, pendingActions)}
                    events={bundle.data.timeline.events}
                    media={bundle.data.media}
                    people={bundle.data.people}
                    places={bundle.data.places}
                    faces={bundle.data.assets.faces}
                    visualAssets={bundle.data.assets.visual}
                    onPlay={setActiveMoment}
                    onQueue={queueAction}
                  />
                ) : (
                  <EmptyState icon={CheckCircle2} title="Nothing needs review" />
                )}
                <PendingActionsPanel actions={pendingActions} busy={busy} onApply={applyActions} onReapply={reapplyCorrections} onRemove={removeAction} />
              </div>
            </div>
          </aside>
        </div>
      )}

      {activeMoment && (
        <DraggableMiniplayer>
          <VideoPlayerPanel moment={activeMoment} onClear={() => setActiveMoment(null)} />
        </DraggableMiniplayer>
      )}
    </div>
  );
}

function LibraryView({
  events,
  media,
  albums,
  summary,
  onOpen,
  onOpenAlbum,
}: {
  events: EventRecord[];
  media: MediaRecord[];
  albums: AlbumRecord[];
  summary: Record<string, number>;
  onOpen: (event: EventRecord) => void;
  onOpenAlbum: (album: AlbumRecord) => void;
}) {
  const [sort, setSort] = useState<LibrarySort>(() => {
    const requested = new URLSearchParams(window.location.search).get("sort");
    return requested === "chrono" || requested === "place" ? requested : "tape";
  });
  const { related, unrelated } = useMemo(() => splitByRelatedness(events), [events]);
  const groups = useMemo(() => libraryGroups(related, media, sort), [related, media, sort]);
  const eventsById = useMemo(() => new Map(events.map((event) => [event.id, event])), [events]);
  const memories = useMemo(() => memoriesRowAlbums(albums), [albums]);
  const totalHours = Math.round(media.reduce((acc, tape) => acc + (tape.duration_s ?? 0), 0) / 3600);

  return (
    <section className="library">
      <header className="stage-hero">
        <div className="hero-row">
          <div>
            <h1>Library</h1>
            <p>
              {media.length} tapes · {totalHours} hours · {events.length} events · {summary.people ?? 0} people
            </p>
          </div>
          <div className="segmented" role="tablist" aria-label="Sort library">
            {(
              [
                ["tape", "By Tape"],
                ["chrono", "Chronological"],
                ["place", "By Place"],
              ] as Array<[LibrarySort, string]>
            ).map(([id, label]) => (
              <button key={id} className={sort === id ? "active" : ""} onClick={() => setSort(id)}>
                {label}
              </button>
            ))}
          </div>
        </div>
      </header>

      {memories.length > 0 && (
        <div className="memories-row" aria-label="Memories">
          {(() => {
            const usedCovers = new Set<string>();
            return memories.map((album) => {
              const cover = albumCoverPath(album, eventsById, usedCovers);
              if (cover) {
                usedCovers.add(cover);
              }
              return (
                <button key={album.id} className="memory-card" onClick={() => onOpenAlbum(album)}>
                  {cover ? <img src={assetUrl(cover)} alt="" loading="lazy" /> : <div className="card-fallback"><CalendarDays size={26} /></div>}
                  <span className="memory-shade">
                    <strong>{album.title}</strong>
                    <small>{memoryDateLabel(album)}</small>
                  </span>
                </button>
              );
            });
          })()}
        </div>
      )}

      {groups.map((group) => (
        <section key={group.key} className="library-group">
          <div className="group-head">
            <h2>{group.label}</h2>
            <span>
              {group.sublabel ? `${group.sublabel} · ` : ""}
              {group.events.length === 1 ? "1 event" : `${group.events.length} events`}
            </span>
          </div>
          <div className="event-grid">
            {group.events.map((event) => (
              <EventCard key={event.id} event={event} media={media} onOpen={onOpen} />
            ))}
          </div>
        </section>
      ))}

      {unrelated.length > 0 && (
        <details className="offcuts">
          <summary>
            <ChevronRight size={15} className="chevron" />
            Also on these tapes — TV broadcasts and other footage ({unrelated.length})
          </summary>
          <div className="event-grid dimmed">
            {unrelated.map((event) => (
              <EventCard key={event.id} event={event} media={media} onOpen={onOpen} />
            ))}
          </div>
        </details>
      )}
    </section>
  );
}

function EventCard({
  event,
  media,
  onOpen,
}: {
  event: EventRecord;
  media: MediaRecord[];
  onOpen: (event: EventRecord) => void;
}) {
  const image = event.thumbnail_path || event.keyframe_path;
  const year = eventYear(event);
  const place = placeDisplayLabel(event.places[0]?.label);
  const subtitle = [year, place, formatEventDuration(event)].filter(Boolean).join(" · ");
  const [previewing, setPreviewing] = useState(false);
  const hoverTimer = useRef<number | null>(null);
  const moment = useMemo(() => momentFromEvent(event, media), [event, media]);

  function armPreview() {
    if (!moment) return;
    hoverTimer.current = window.setTimeout(() => setPreviewing(true), 380);
  }

  function disarmPreview() {
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
    setPreviewing(false);
  }

  return (
    <figure
      className="event-card"
      onClick={() => onOpen(event)}
      role="button"
      tabIndex={0}
      onKeyDown={(keyEvent) => keyEvent.key === "Enter" && onOpen(event)}
      onMouseEnter={armPreview}
      onMouseLeave={disarmPreview}
    >
      {image ? (
        <img src={assetUrl(image)} alt="" loading="lazy" />
      ) : (
        <div className="card-fallback">
          <Film size={26} />
        </div>
      )}
      {previewing && moment && (
        <video
          className="card-preview"
          src={`${videoUrl(moment.videoId)}#t=${Math.max(0, moment.startS).toFixed(1)}`}
          muted
          autoPlay
          playsInline
          preload="none"
          onError={() => setPreviewing(false)}
        />
      )}
      <figcaption className="card-shade">
        <strong>{event.title}</strong>
        {subtitle && <span>{subtitle}</span>}
      </figcaption>
      {event.review_status === "needs_review" && <span className="review-dot" title="Has an unconfirmed guess" />}
    </figure>
  );
}

function EventSheet({
  event,
  scenes,
  media,
  people,
  reviewItems,
  onClose,
  onPlay,
  onOpenReview,
}: {
  event: EventRecord;
  scenes: SceneRecord[];
  media: MediaRecord[];
  people: PersonRecord[];
  reviewItems: ReviewItem[];
  onClose: () => void;
  onPlay: (moment: PlayerMoment) => void;
  onOpenReview: (itemId: string) => void;
}) {
  const hero = event.keyframe_path || event.thumbnail_path;
  const strip = useMemo(() => eventFilmstrip(event, scenes, media), [event, scenes, media]);
  const relatedItems = useMemo(
    () =>
      reviewItems.filter(
        (item) => item.related_event_ids?.includes(event.id) || item.events.some((entry) => entry.event_id === event.id),
      ),
    [event.id, reviewItems],
  );
  const summaryText = event.reconciliation?.reconciled_summary || event.summary;
  const recordedDate = event.dates.find((date) => date.date_value && plausibleEventDate(date))?.date_value;
  const mentionedDates = event.dates.filter((date) => !plausibleEventDate(date));
  const byline = [
    event.event_type ? humanizeToken(event.event_type) : null,
    eventYear(event),
    recordedDate,
  ]
    .filter(Boolean)
    .slice(0, 2)
    .join(" · ");

  return (
    <div className="sheet-scrim" onClick={onClose}>
      <article className="event-sheet" onClick={(clickEvent) => clickEvent.stopPropagation()}>
        <button className="sheet-close" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
        <div className="sheet-hero">
          {hero ? <img src={assetUrl(hero)} alt="" /> : <div className="card-fallback tall"><Film size={40} /></div>}
          <button className="hero-play" onClick={() => playEvent(event, media, onPlay)} aria-label="Play">
            <Play size={22} fill="currentColor" />
          </button>
        </div>
        <div className="sheet-body">
          <header>
            <h1>{event.title}</h1>
            {byline && <p className="byline">{byline}</p>}
            {mentionedDates.length > 0 && (
              <p className="historical-row">
                {mentionedDates.slice(0, 3).map((date) => (
                  <span key={date.id} className="historical-chip" title="Mentioned in narration — not the recording date">
                    mentioned: {date.label || date.date_value} · historical
                  </span>
                ))}
              </p>
            )}
          </header>

          {(event.people.length > 0 || event.places.length > 0) && (
            <div className="sheet-chips">
              {event.people.slice(0, 8).map((ref) => {
                const person = findPerson(people, ref.id, ref.label);
                const thumb = primaryPersonThumb(person);
                return (
                  <span key={ref.id} className="person-bubble">
                    {thumb ? <img src={assetUrl(thumb)} alt="" /> : <i>{personInitials(ref.label)}</i>}
                    {personDisplayName(ref.label)}
                  </span>
                );
              })}
              {event.places.slice(0, 4).map((ref) => (
                <span key={ref.id} className="place-pill">
                  <MapPin size={12} />
                  {placeDisplayLabel(ref.label)}
                </span>
              ))}
            </div>
          )}

          {summaryText && <p className="sheet-summary">{summaryText}</p>}

          {strip.length > 0 && (
            <div className="filmstrip" aria-label="Moments">
              {strip.map((frame) => (
                <button
                  key={frame.scene.id}
                  className="frame"
                  onClick={() =>
                    onPlay({
                      videoId: frame.scene.source_video_id,
                      videoLabel: frame.videoLabel,
                      startS: frame.scene.start_s ?? 0,
                      endS: frame.scene.end_s,
                      title: event.title,
                    })
                  }
                >
                  <img src={assetUrl(frame.scene.thumbnail_path ?? "")} alt="" loading="lazy" />
                  <span>{formatTime(frame.scene.start_s)}</span>
                </button>
              ))}
            </div>
          )}

          {relatedItems.length > 0 && (
            <button className="quiet-review" onClick={() => onOpenReview(relatedItems[0].id)}>
              <Info size={14} />
              {relatedItems.length === 1 ? "1 detail could use a confirmation" : `${relatedItems.length} details could use a confirmation`}
              <ChevronRight size={14} />
            </button>
          )}
        </div>
      </article>
    </div>
  );
}

function PeopleWall({ people, onSelect }: { people: PersonRecord[]; onSelect: (person: PersonRecord) => void }) {
  const { faces, faceless, roles } = useMemo(() => {
    const faces: PersonRecord[] = [];
    const faceless: PersonRecord[] = [];
    const roles: PersonRecord[] = [];
    for (const person of people) {
      if (person.kind === "role_candidate") {
        roles.push(person);
      } else if (primaryPersonThumb(person)) {
        faces.push(person);
      } else {
        faceless.push(person);
      }
    }
    const byCount = (a: PersonRecord, b: PersonRecord) => appearanceCount(b) - appearanceCount(a);
    faces.sort(byCount);
    faceless.sort(byCount);
    roles.sort(byCount);
    return { faces, faceless, roles };
  }, [people]);

  return (
    <section className="people-wall">
      <header className="stage-hero">
        <h1>People</h1>
        <p>{faces.length + faceless.length === 1 ? "1 person" : `${faces.length + faceless.length} people`} across the tapes</p>
      </header>
      <div className="avatar-grid">
        {faces.map((person) => (
          <button key={person.id} className="avatar-cell" onClick={() => onSelect(person)}>
            <PersonAvatar person={person} size="large" />
            <strong>{personDisplayName(person.label)}</strong>
            <span>{appearanceCount(person)} {appearanceCount(person) === 1 ? "event" : "events"}</span>
          </button>
        ))}
      </div>
      {faceless.length > 0 && (
        <details className="offcuts">
          <summary>
            <ChevronRight size={15} className="chevron" />
            Heard or mentioned, no face yet ({faceless.length})
          </summary>
          <div className="avatar-grid compact">
            {faceless.map((person) => (
              <button key={person.id} className="avatar-cell" onClick={() => onSelect(person)}>
                <PersonAvatar person={person} />
                <strong>{personDisplayName(person.label)}</strong>
                <span>{appearanceCount(person)} {appearanceCount(person) === 1 ? "event" : "events"}</span>
              </button>
            ))}
          </div>
        </details>
      )}
      {roles.length > 0 && (
        <details className="offcuts">
          <summary>
            <ChevronRight size={15} className="chevron" />
            Roles heard on tape, not yet matched to a person ({roles.length})
          </summary>
          <div className="avatar-grid compact">
            {roles.map((person) => (
              <button key={person.id} className="avatar-cell" onClick={() => onSelect(person)}>
                <PersonAvatar person={person} />
                <strong>{personDisplayName(person.label)}</strong>
                <span>{appearanceCount(person)} {appearanceCount(person) === 1 ? "event" : "events"}</span>
              </button>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}

function PersonAvatar({ person, size }: { person: PersonRecord; size?: "large" }) {
  const thumb = primaryPersonThumb(person);
  return (
    <span className={`avatar ${size ?? ""}`}>
      {thumb ? <img src={assetUrl(thumb)} alt="" loading="lazy" /> : <i>{personInitials(person.label)}</i>}
    </span>
  );
}

function PersonSheet({
  person,
  events,
  media,
  onClose,
  onOpenEvent,
  onPlay,
}: {
  person: PersonRecord;
  events: EventRecord[];
  media: MediaRecord[];
  onClose: () => void;
  onOpenEvent: (event: EventRecord) => void;
  onPlay: (moment: PlayerMoment) => void;
}) {
  const eventsById = useMemo(() => new Map(events.map((event) => [event.id, event])), [events]);
  const appearances = useMemo(() => {
    if (person.appearances?.length) {
      return person.appearances;
    }
    // Fall back to scanning the timeline for this person's label/aliases.
    const labels = new Set([person.label, ...(person.aliases ?? [])].map((value) => value.toLowerCase()));
    return events
      .filter((event) => event.people.some((ref) => ref.id === person.id || labels.has(ref.label.toLowerCase())))
      .map((event) => ({
        event_id: event.id,
        title: event.title,
        start_s: event.start_s,
        end_s: event.end_s,
        source_video_ids: event.source_video_ids,
      }));
  }, [events, person]);
  return (
    <div className="sheet-scrim" onClick={onClose}>
      <article className="event-sheet person-sheet" onClick={(clickEvent) => clickEvent.stopPropagation()}>
        <button className="sheet-close" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
        <div className="sheet-body">
          <header className="person-head">
            <PersonAvatar person={person} size="large" />
            <div>
              <h1>{personDisplayName(person.label)}</h1>
              <p className="byline">
                {personAliasByline(person) && `also known as ${personAliasByline(person)} · `}
                {appearances.length} {appearances.length === 1 ? "event" : "events"}
              </p>
            </div>
          </header>
          <div className="event-grid">
            {appearances.map((entry) => {
              const record = eventsById.get(entry.event_id);
              if (record) {
                return <EventCard key={entry.event_id} event={record} media={media} onOpen={onOpenEvent} />;
              }
              return (
                <button key={entry.event_id} className="mini-event" onClick={() => playEvent(entry, media, onPlay)}>
                  <Play size={13} />
                  <span>{entry.title}</span>
                </button>
              );
            })}
          </div>
        </div>
      </article>
    </div>
  );
}

function AlbumSheet({
  album,
  events,
  media,
  onClose,
  onOpenEvent,
}: {
  album: AlbumRecord;
  events: EventRecord[];
  media: MediaRecord[];
  onClose: () => void;
  onOpenEvent: (event: EventRecord) => void;
}) {
  const eventsById = useMemo(() => new Map(events.map((event) => [event.id, event])), [events]);
  const cover = albumCoverPath(album, eventsById);
  const albumEvents = (album.events ?? [])
    .map((entry) => eventsById.get(entry.event_id))
    .filter((event): event is EventRecord => Boolean(event));
  const byline = [
    album.date_label,
    placeDisplayLabel(album.place_label),
    (album.events?.length ?? 0) === 1 ? "1 event" : `${album.events?.length ?? 0} events`,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="sheet-scrim" onClick={onClose}>
      <article className="event-sheet" onClick={(clickEvent) => clickEvent.stopPropagation()}>
        <button className="sheet-close" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
        <div className="sheet-hero">
          {cover ? <img src={assetUrl(cover)} alt="" /> : <div className="card-fallback tall"><CalendarDays size={40} /></div>}
        </div>
        <div className="sheet-body">
          <header>
            <h1>{album.title}</h1>
            {byline && <p className="byline">{byline}</p>}
          </header>
          <div className="event-grid">
            {albumEvents.map((event) => (
              <EventCard key={event.id} event={event} media={media} onOpen={onOpenEvent} />
            ))}
          </div>
        </div>
      </article>
    </div>
  );
}

function splitByRelatedness(events: EventRecord[]) {
  const related: EventRecord[] = [];
  const unrelated: EventRecord[] = [];
  for (const event of events) {
    if ((event.relatedness ?? "").includes("unrelated")) {
      unrelated.push(event);
    } else {
      related.push(event);
    }
  }
  return { related, unrelated };
}

type LibrarySort = "tape" | "chrono" | "place";

type LibraryGroup = {
  key: string;
  label: string;
  sublabel?: string;
  events: EventRecord[];
};

function libraryGroups(events: EventRecord[], media: MediaRecord[], sort: LibrarySort = "tape"): LibraryGroup[] {
  const byTimeline = (a: EventRecord, b: EventRecord) => (a.start_s ?? 0) - (b.start_s ?? 0);

  if (sort === "chrono") {
    // Dates come from narration and can be historical; unknown years are
    // grouped honestly rather than guessed.
    const byYear = new Map<string, EventRecord[]>();
    for (const event of events) {
      const year = eventYear(event) || "Undated";
      (byYear.get(year) ?? byYear.set(year, []).get(year))!.push(event);
    }
    const keys = [...byYear.keys()].sort((a, b) => {
      if (a === "Undated") return 1;
      if (b === "Undated") return -1;
      return a.localeCompare(b);
    });
    return keys.map((key) => ({
      key,
      label: key === "Undated" ? "No date yet" : key,
      events: (byYear.get(key) ?? []).sort(byTimeline),
    }));
  }

  if (sort === "place") {
    const byPlace = new Map<string, EventRecord[]>();
    for (const event of events) {
      const place = placeDisplayLabel(event.places[0]?.label) || "No place identified";
      (byPlace.get(place) ?? byPlace.set(place, []).get(place))!.push(event);
    }
    const keys = [...byPlace.keys()].sort((a, b) => {
      if (a === "No place identified") return 1;
      if (b === "No place identified") return -1;
      return (byPlace.get(b)?.length ?? 0) - (byPlace.get(a)?.length ?? 0);
    });
    return keys.map((key) => ({
      key,
      label: key,
      events: (byPlace.get(key) ?? []).sort(byTimeline),
    }));
  }

  // Default: by tape — like film rolls — since home-video dates are
  // unreliable until reviewed; years show on cards when known.
  const order = new Map(media.map((tape, index) => [tape.id, index]));
  const byTape = new Map<string, EventRecord[]>();
  for (const event of events) {
    const tapeId = event.source_video_ids[0] ?? "unknown";
    (byTape.get(tapeId) ?? byTape.set(tapeId, []).get(tapeId))!.push(event);
  }
  const keys = [...byTape.keys()].sort((a, b) => (order.get(a) ?? 99) - (order.get(b) ?? 99));
  return keys.map((key) => {
    const tape = media.find((row) => row.id === key);
    const index = order.get(key);
    const stem = (tape?.filename ?? key).replace(/\.[a-z0-9]+$/i, "");
    return {
      key,
      label: typeof index === "number" ? `Tape ${index + 1}` : "Loose footage",
      sublabel: stem,
      events: (byTape.get(key) ?? []).sort(byTimeline),
    };
  });
}

// Camcorders don't predate ~1970; narrated years older than that (volcano
// eruptions, building cornerstones) are historical context, not event dates.
const EARLIEST_PLAUSIBLE_YEAR = 1970;
const LATEST_PLAUSIBLE_YEAR = new Date().getFullYear();

// A date can anchor an event only if the pipeline didn't exclude it and its
// year sits inside the camcorder era; anything else is narration history.
function plausibleEventDate(date: DateRef): boolean {
  if (date.excluded_as_event_date) {
    return false;
  }
  const match = /(19|20)\d{2}/.exec(date.date_value ?? date.label ?? "");
  if (!match) {
    return true;
  }
  const year = Number(match[0]);
  return year >= EARLIEST_PLAUSIBLE_YEAR && year <= LATEST_PLAUSIBLE_YEAR;
}

function eventYear(event: EventRecord): string {
  for (const date of event.dates ?? []) {
    if (!plausibleEventDate(date)) {
      continue;
    }
    const match = /(19|20)\d{2}/.exec(date.date_value ?? date.label ?? "");
    if (match) {
      return match[0];
    }
  }
  return "";
}

function eventFilmstrip(event: EventRecord, scenes: SceneRecord[], media: MediaRecord[]) {
  const frames: Array<{ scene: SceneRecord; videoLabel: string }> = [];
  for (const videoId of event.source_video_ids) {
    const tape = media.find((row) => row.id === videoId);
    const offset = tape?.offset_s ?? 0;
    const localStart = Math.max(0, (event.start_s ?? offset) - offset);
    const localEnd = typeof event.end_s === "number" ? event.end_s - offset : Number.POSITIVE_INFINITY;
    for (const scene of scenes) {
      if (
        scene.source_video_id === videoId &&
        scene.scene_type === "content" &&
        scene.thumbnail_path &&
        (scene.end_s ?? 0) >= localStart &&
        (scene.start_s ?? 0) <= localEnd
      ) {
        frames.push({ scene, videoLabel: tape?.filename || videoId });
      }
    }
  }
  frames.sort((a, b) => (a.scene.start_s ?? 0) - (b.scene.start_s ?? 0));
  if (frames.length > 28) {
    const step = frames.length / 28;
    return Array.from({ length: 28 }, (_, index) => frames[Math.floor(index * step)]);
  }
  return frames;
}

function formatEventDuration(event: EventRecord): string {
  if (typeof event.start_s !== "number" || typeof event.end_s !== "number") {
    return "";
  }
  const seconds = Math.max(0, event.end_s - event.start_s);
  if (seconds < 90) {
    return `${Math.round(seconds)}s`;
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m`;
  }
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

function appearanceCount(person: PersonRecord): number {
  return person.appearances?.length || person.appearance_count || person.canonical_event_ids?.length || 0;
}

function personInitials(label: string): string {
  return personDisplayName(label)
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? "")
    .join("");
}

function humanizeToken(value: string): string {
  const text = value.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// Pipeline labels hold every alias at once ("Filia / Filip / Филя"); a person
// deserves one confident name, with the rest demoted to a byline.
function personDisplayName(label: string): string {
  const parts = label
    .split("/")
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length <= 1) {
    return label.trim() || label;
  }
  const latin = parts.filter((part) => /^[ -ɏ]+$/.test(part));
  const pool = latin.length ? latin : parts;
  return pool.reduce((best, part) => (part.length < best.length ? part : best));
}

function personAliasList(label: string, extra: string[] = []): string[] {
  const display = personDisplayName(label);
  const seen = new Set([display.toLowerCase()]);
  const rest: string[] = [];
  for (const alias of [...label.split("/"), ...extra]) {
    const trimmed = alias.trim();
    if (!trimmed || seen.has(trimmed.toLowerCase())) {
      continue;
    }
    seen.add(trimmed.toLowerCase());
    rest.push(trimmed);
  }
  return rest;
}

function personAliasByline(person: PersonRecord): string {
  return personAliasList(person.label, person.aliases ?? []).slice(0, 6).join(", ");
}

// One confident name up front; the rest of the aliases stay a hover away.
function PersonNameBadge({ label, aliases = [] }: { label: string; aliases?: string[] }) {
  const display = personDisplayName(label);
  const rest = personAliasList(label, aliases);
  if (!rest.length) {
    return <>{display}</>;
  }
  return (
    <span className="alias-badge" title={`Also: ${rest.join(", ")}`}>
      {display}
      <span className="alias-more">+{rest.length}</span>
      <span className="alias-pop">{rest.join(" · ")}</span>
    </span>
  );
}

const PERSONISH_TASKS = new Set(["resolve_face_cluster", "resolve_speaker", "resolve_person"]);

// Review titles arrive as "Resolve face cluster: Filia / Filip / Филя"; keep
// the prefix, badge the alias soup.
function ReviewItemTitle({ item }: { item: ReviewItem }) {
  const title = item.title ?? "";
  if (!PERSONISH_TASKS.has(item.task_type) || !title.includes("/")) {
    return <>{title}</>;
  }
  const colon = title.indexOf(":");
  const tail = (colon >= 0 ? title.slice(colon + 1) : title).trim();
  if (!tail.includes("/")) {
    return <>{title}</>;
  }
  return (
    <>
      {colon >= 0 ? `${title.slice(0, colon + 1)} ` : null}
      <PersonNameBadge label={tail} />
    </>
  );
}

// Free-text labels like "Appears to be Filia / Filip / Филя" — badge the
// slash-run, leave the surrounding words alone. Spaced slashes only, so
// dates and paths never match.
function AliasAwareLabel({ text }: { text: string }) {
  const firstSlash = text.indexOf(" / ");
  if (firstSlash < 0) {
    return <>{text}</>;
  }
  const head = text.slice(0, firstSlash);
  let start = 0;
  for (const delimiter of [": ", " be ", " is ", " as ", "· ", "( "]) {
    const at = head.lastIndexOf(delimiter);
    if (at >= 0) {
      start = Math.max(start, at + delimiter.length);
    }
  }
  const rest = text.slice(start);
  const cut = rest.search(/\s+\(/);
  const run = (cut >= 0 ? rest.slice(0, cut) : rest).trim();
  if (!run.includes(" / ")) {
    return <>{text}</>;
  }
  return (
    <>
      {text.slice(0, start)}
      <PersonNameBadge label={run} />
      {cut >= 0 ? rest.slice(cut) : ""}
    </>
  );
}

// Regions that must never share one scope: two of these in a parenthetical
// means the pipeline is asserting a contradiction, so we suppress it.
const DISJOINT_REGIONS = [
  "oregon",
  "wisconsin",
  "idaho",
  "hawaii",
  "alaska",
  "california",
  "florida",
  "washington",
  "switzerland",
  "russia",
];

// Context groups named after tapes are honest about being unplaced; named
// contexts drop the trailing machinery word.
function contextDisplayLabel(raw: string): string {
  const label = raw.trim();
  if (/^video_\d+/i.test(label)) {
    const year = /(19|20)\d{2}(?:\s*[–-]\s*(?:19|20)\d{2})?/.exec(label)?.[0];
    return year ? `${year} · unplaced` : "Unplaced";
  }
  return label.replace(/\s*context$/i, "");
}

// Machine scopes ("video_000003 context") and contradictory compound scopes
// ("Hawaii, Moscow context") assert things the archive has not earned; only a
// scope that reads as one coherent place survives to the screen.
function placeDisplayLabel(raw?: string): string {
  if (!raw) {
    return "";
  }
  const trimmed = raw.trim();
  const match = /^(.*?)\s*\(([^()]*)\)$/.exec(trimmed);
  if (!match) {
    return trimmed;
  }
  const label = match[1].trim() || trimmed;
  let scope = match[2].trim();
  if (!scope || /video_\d+/i.test(scope)) {
    return label;
  }
  if (/^near\s/i.test(scope)) {
    return `${label} · ${scope}`;
  }
  scope = scope.replace(/\s*context$/i, "").trim();
  if (!scope) {
    return label;
  }
  const parts = scope.split(",").map((part) => part.trim()).filter(Boolean);
  const regionHits = new Set(
    parts.map((part) => part.toLowerCase()).filter((part) => DISJOINT_REGIONS.includes(part)),
  );
  const leadsWithRegion = DISJOINT_REGIONS.includes(parts[0]?.toLowerCase() ?? "");
  if (regionHits.size > 1 || parts.length > 2 || (leadsWithRegion && parts.length > 1)) {
    return label;
  }
  return `${label} · ${scope}`;
}

function ProjectStats({ summary, backlogCount }: { summary: Record<string, number>; backlogCount: number }) {
  const metrics = [
    ["Events", summary.events],
    ["People", summary.people],
    ["Places", summary.places],
    ["Faces", summary.face_clusters],
    ["Queue", summary.review_items],
    ["Backlog", summary.review_backlog_items ?? backlogCount],
  ];
  return (
    <div className="stats-grid">
      {metrics.map(([label, value]) => (
        <div key={label} className="stat">
          <span>{value ?? 0}</span>
          <small>{label}</small>
        </div>
      ))}
    </div>
  );
}

function ReviewQueue({
  items,
  selectedId,
  pendingActions,
  onSelect,
}: {
  items: ReviewItem[];
  selectedId: string;
  pendingActions: ReviewAction[];
  onSelect: (item: ReviewItem) => void;
}) {
  return (
    <div className="queue-list">
      {items.map((item) => {
        const pending = itemHasPendingAction(item, pendingActions);
        return (
          <button
            key={item.id}
            className={`queue-row ${selectedId === item.id ? "selected" : ""}`}
            onClick={() => onSelect(item)}
          >
            <span className={`task-dot ${item.task_type}`} />
            <span className="queue-text">
              <strong>
                <ReviewItemTitle item={item} />
              </strong>
              <small>
                {taskLabel(item.task_type)} · {formatConfidence(item.confidence)}
              </small>
            </span>
            {pending && <span className="pending-pill">Queued</span>}
          </button>
        );
      })}
    </div>
  );
}

function ReviewDetail({
  item,
  pending,
  events,
  media,
  people,
  places,
  faces = [],
  visualAssets = [],
  onPlay,
  onQueue,
}: {
  item: ReviewItem;
  pending: boolean;
  events: EventRecord[];
  media: MediaRecord[];
  people: PersonRecord[];
  places: PlaceRecord[];
  faces?: FaceObservation[];
  visualAssets?: VisualAsset[];
  onPlay: (moment: PlayerMoment) => void;
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
  const eventsById = useMemo(() => new Map(events.map((event) => [event.id, event])), [events]);
  const isFaceItem = item.task_type === "resolve_face_cluster";
  return (
    <section className="review-surface">
      {isFaceItem ? (
        <FaceContextGallery clusterId={item.source_id} faces={faces} visualAssets={visualAssets} media={media} onPlay={onPlay} />
      ) : (
        <div className="review-media">
          {item.thumbnail_path ? (
            <img src={assetUrl(item.thumbnail_path)} alt="" />
          ) : (
            <div className="media-placeholder">
              <ImageIcon size={28} />
            </div>
          )}
        </div>
      )}
      <div className="review-body">
        <div className="review-heading">
          <span className={`task-badge ${item.task_type}`}>{taskLabel(item.task_type)}</span>
          <span className="review-confidence">{formatConfidence(item.confidence)}</span>
          {pending && <span className="pending-pill">Queued</span>}
        </div>
        <h2>
          <ReviewItemTitle item={item} />
        </h2>
        <p>{item.prompt}</p>
        <SuggestedResolution item={item} onQueue={onQueue} />
        <ReviewSubjectPreview item={item} eventsById={eventsById} people={people} places={places} />
        <EvidenceList item={item} media={media} onPlay={onPlay} />
        <ReviewActionControls item={item} people={people} onQueue={onQueue} />
      </div>
    </section>
  );
}

function FaceContextGallery({
  clusterId,
  faces,
  visualAssets,
  media,
  onPlay,
}: {
  clusterId: string;
  faces: FaceObservation[];
  visualAssets: VisualAsset[];
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
}) {
  const tiles = useMemo(() => {
    const assetsById = new Map(visualAssets.map((asset) => [asset.id, asset]));
    const assetsBySubject = new Map<string, VisualAsset>();
    for (const asset of visualAssets) {
      if (!assetsBySubject.has(asset.subject_id)) {
        assetsBySubject.set(asset.subject_id, asset);
      }
    }
    return faces
      .filter((face) => face.face_cluster_id === clusterId && face.face_thumbnail_path)
      .slice(0, 18)
      .map((face) => {
        const asset = assetsById.get(face.source_subject_id ?? "") ?? assetsBySubject.get(face.source_subject_id ?? "");
        return { face, asset };
      });
  }, [clusterId, faces, visualAssets]);

  if (!tiles.length) {
    return null;
  }
  return (
    <div className="face-context-grid">
      {tiles.map(({ face, asset }) => {
        const scene = asset?.keyframe_path || asset?.thumbnail_path;
        const tape = media.find((row) => row.id === asset?.source_video_id);
        const playable = asset && typeof asset.time_s === "number";
        return (
          <button
            key={face.id}
            className="face-context-tile"
            title={playable ? "Play this moment" : undefined}
            onClick={() =>
              playable &&
              onPlay({
                videoId: asset.source_video_id,
                videoLabel: tape?.filename || asset.source_video_id,
                startS: Math.max(0, (asset.time_s ?? 0) - 2),
                title: "Face context",
              })
            }
          >
            {scene ? <img className="scene-frame" src={assetUrl(scene)} alt="" loading="lazy" /> : <div className="card-fallback" />}
            <img className="face-inset" src={assetUrl(face.face_thumbnail_path ?? "")} alt="" loading="lazy" />
          </button>
        );
      })}
    </div>
  );
}

function SuggestedResolution({ item, onQueue }: { item: ReviewItem; onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void> }) {
  const suggestion = item.suggested_action;
  if (!suggestion) {
    return null;
  }
  const actions = reviewActionsFromSuggestion(suggestion);
  return (
    <div className="suggested-resolution">
      <div>
        <small>Best Guess</small>
        <strong>
          <AliasAwareLabel text={suggestion.label} />
        </strong>
        {suggestion.rationale ? <span>{suggestion.rationale}</span> : null}
      </div>
      <CommandButton icon={CheckCircle2} label="Accept Guess" onClick={() => onQueue(actions.length === 1 ? actions[0] : actions)} />
    </div>
  );
}

function ReviewSubjectPreview({
  item,
  eventsById,
  people,
  places,
}: {
  item: ReviewItem;
  eventsById: Map<string, EventRecord>;
  people: PersonRecord[];
  places: PlaceRecord[];
}) {
  if (item.task_type === "confirm_relationship") {
    const candidate = item.candidate as {
      subject_entity_id?: string;
      subject_label?: string;
      object_entity_id?: string;
      object_label?: string;
      predicate?: string;
    };
    return (
      <div className="relation-preview">
        <PersonChip
          label={candidate.subject_label || "Unknown person"}
          person={findPerson(people, candidate.subject_entity_id, candidate.subject_label)}
        />
        <span className="relation-predicate">{predicateLabel(candidate.predicate)}</span>
        <PersonChip
          label={candidate.object_label || "Unknown person"}
          person={findPerson(people, candidate.object_entity_id, candidate.object_label)}
        />
      </div>
    );
  }

  if (item.task_type === "resolve_speaker") {
    const candidate = item.candidate as {
      speaker_label?: string;
      identity_candidates?: SpeakerIdentityCandidate[];
    };
    const top = candidate.identity_candidates?.[0];
    return (
      <div className="relation-preview">
        <div className="subject-chip">
          <div className="face-stack">
            <Users size={18} />
          </div>
          <div>
            <strong>{candidate.speaker_label || "Local speaker"}</strong>
            <small>speaker track</small>
          </div>
        </div>
        <span className="relation-predicate">appears to be</span>
        <PersonChip label={top?.person_label || "Unknown person"} person={findPerson(people, top?.person_group_id, top?.person_label)} />
      </div>
    );
  }

  if (item.task_type === "confirm_place_context") {
    const candidate = item.candidate as { source_place_id?: string; target_place_id?: string; predicate?: string };
    const source = places.find((place) => place.id === candidate.source_place_id);
    const target = places.find((place) => place.id === candidate.target_place_id);
    return (
      <div className="relation-preview">
        <PlaceChip place={source} eventsById={eventsById} label={source?.display_label || "Source place"} />
        <span className="relation-predicate">{predicateLabel(candidate.predicate)}</span>
        <PlaceChip place={target} eventsById={eventsById} label={target?.display_label || "Target place"} />
      </div>
    );
  }

  if (item.task_type === "resolve_place") {
    const candidate = item.candidate as { display_label?: string; label?: string };
    const place = places.find((row) => row.id === item.source_id);
    return <PlaceChip place={place} eventsById={eventsById} label={candidate.display_label || candidate.label || item.title} wide />;
  }

  return null;
}

function PersonChip({ label, person }: { label: string; person?: PersonRecord }) {
  const thumbs = personFaceThumbs(person);
  return (
    <div className="subject-chip">
      <div className="face-stack">
        {thumbs.length ? (
          thumbs.slice(0, 3).map((path) => <img key={path} src={assetUrl(path)} alt="" />)
        ) : (
          <Users size={18} />
        )}
      </div>
      <div>
        <strong>
          <PersonNameBadge label={person?.label || label} aliases={person?.aliases ?? []} />
        </strong>
        <small>{person ? "person candidate" : "unresolved role"}</small>
      </div>
    </div>
  );
}

function PlaceChip({
  place,
  eventsById,
  label,
  wide,
}: {
  place?: PlaceRecord;
  eventsById: Map<string, EventRecord>;
  label: string;
  wide?: boolean;
}) {
  const thumb = firstPlaceThumbnail(place, eventsById);
  return (
    <div className={`subject-chip ${wide ? "wide" : ""}`}>
      <div className="place-thumb">{thumb ? <img src={assetUrl(thumb)} alt="" /> : <MapPin size={18} />}</div>
      <div>
        <strong>{label}</strong>
        <small>{place ? `${place.place_type ?? "place"} · ${place.review_status}${place.evidence_basis?.source_label ? ` · ${place.evidence_basis.source_label}` : ""}` : "place context"}</small>
      </div>
    </div>
  );
}

function ReviewActionControls({
  item,
  people = [],
  onQueue,
}: {
  item: ReviewItem;
  people?: PersonRecord[];
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
  if (item.task_type === "resolve_face_cluster") {
    return <FaceClusterActions item={item} people={people} onQueue={onQueue} />;
  }
  if (item.task_type === "resolve_speaker") {
    return <SpeakerIdentityActions item={item} onQueue={onQueue} />;
  }
  if (item.task_type === "resolve_place") {
    return <PlaceActions item={item} onQueue={onQueue} />;
  }
  if (item.task_type === "confirm_place_context") {
    return (
      <ActionRow>
        <CommandButton icon={Check} label="Confirm Link" onClick={() => onQueue(baseAction(item, "confirm_place_context"))} />
        <CommandButton icon={X} label="Reject Link" tone="danger" onClick={() => onQueue(baseAction(item, "reject_place_context"))} />
      </ActionRow>
    );
  }
  if (item.task_type === "confirm_relationship") {
    return <RelationshipActions item={item} onQueue={onQueue} />;
  }
  if (item.task_type === "review_event") {
    return <EventActions item={item} onQueue={onQueue} />;
  }
  if (item.task_type === "resolve_date") {
    return <DateActions item={item} onQueue={onQueue} />;
  }
  if (item.task_type === "resolve_person") {
    return <PersonActions item={item} onQueue={onQueue} />;
  }
  return null;
}

function FaceClusterActions({
  item,
  people = [],
  onQueue,
}: {
  item: ReviewItem;
  people?: PersonRecord[];
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
  const candidates = identityCandidates(item);
  const [selectedId, setSelectedId] = useState(candidates[0]?.face_identity_candidate_id ?? "");
  const [customName, setCustomName] = useState("");
  const selected = candidates.find((candidate) => candidate.face_identity_candidate_id === selectedId);

  function nameCluster() {
    const label = customName.trim();
    if (!label) return;
    const match = findPerson(people, undefined, label);
    void onQueue({
      id: `label_${item.source_id}`,
      action: "label_face_cluster",
      target_id: item.source_id,
      target_type: "face_cluster",
      notes: match ? `Matched existing person ${match.label}` : "Named directly by reviewer",
      payload: match ? { label, person_group_id: match.id } : { label },
    });
    setCustomName("");
  }

  return (
    <div className="action-block">
      <form
        className="name-entry"
        onSubmit={(event) => {
          event.preventDefault();
          nameCluster();
        }}
      >
        <input
          value={customName}
          onChange={(event) => setCustomName(event.target.value)}
          placeholder="No match? Type who this is…"
        />
        <button type="submit" className="command-button secondary" disabled={!customName.trim()}>
          <UserCheck size={15} />
          <span>Name Person</span>
        </button>
      </form>
      <div className="candidate-list">
        {candidates.map((candidate) => (
          <label key={candidate.face_identity_candidate_id} className="candidate-option">
            <input
              type="radio"
              name={`identity-${item.id}`}
              checked={selectedId === candidate.face_identity_candidate_id}
              onChange={() => setSelectedId(candidate.face_identity_candidate_id)}
            />
            <span>
              <strong>
                <PersonNameBadge label={candidate.person_label} />
              </strong>
              <small>
                {formatConfidence(candidate.confidence)} · {candidate.candidate_ambiguity ?? "unknown"} ambiguity
                {candidate.direct_name_event_ids?.length ? " · named in event" : " · co-occurrence only"}
                {candidate.face_quality_status && candidate.face_quality_status !== "usable"
                  ? ` · ${qualityShortLabel(candidate.face_quality_status)}`
                  : ""}
              </small>
              <small>{candidate.supporting_event_titles.join(", ")}</small>
              {candidate.face_quality_notes?.length ? <small>{candidate.face_quality_notes.join(" ")}</small> : null}
            </span>
          </label>
        ))}
      </div>
      <ActionRow>
        <CommandButton
          icon={UserCheck}
          label="Confirm Identity"
          disabled={!selected}
          onClick={() =>
            selected &&
            onQueue({
              ...baseAction(item, "confirm_identity"),
              target_id: selected.face_identity_candidate_id,
              payload: {
                face_cluster_id: item.source_id,
                person_group_id: selected.person_group_id,
              },
            })
          }
        />
        <CommandButton
          icon={X}
          label="Reject Candidate"
          tone="danger"
          disabled={!selected}
          onClick={() =>
            selected &&
            onQueue({
              ...baseAction(item, "reject_identity"),
              target_id: selected.face_identity_candidate_id,
              payload: {
                face_cluster_id: item.source_id,
                person_group_id: selected.person_group_id,
              },
            })
          }
        />
        <CommandButton
          icon={XCircle}
          label="No Match"
          tone="danger"
          disabled={!candidates.length}
          onClick={() =>
            onQueue(
              candidates.map((candidate) => ({
                ...baseAction(item, "reject_identity"),
                id: `reject_${candidate.face_identity_candidate_id}`,
                target_id: candidate.face_identity_candidate_id,
                payload: {
                  face_cluster_id: item.source_id,
                  person_group_id: candidate.person_group_id,
                },
              })),
            )
          }
        />
      </ActionRow>
    </div>
  );
}

function SpeakerIdentityActions({ item, onQueue }: { item: ReviewItem; onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void> }) {
  const candidates = speakerIdentityCandidates(item);
  const [selectedId, setSelectedId] = useState(candidates[0]?.speaker_identity_candidate_id ?? candidates[0]?.id ?? "");
  const selected = candidates.find((candidate) => (candidate.speaker_identity_candidate_id ?? candidate.id) === selectedId);
  return (
    <div className="action-block">
      <div className="candidate-list">
        {candidates.map((candidate) => {
          const candidateId = candidate.speaker_identity_candidate_id ?? candidate.id ?? "";
          return (
            <label key={candidateId} className="candidate-option">
              <input
                type="radio"
                name={`speaker-identity-${item.id}`}
                checked={selectedId === candidateId}
                onChange={() => setSelectedId(candidateId)}
              />
              <span>
                <strong>
                <PersonNameBadge label={candidate.person_label} />
              </strong>
                <small>
                  {formatConfidence(candidate.confidence)}
                  {candidate.person_kind ? ` · ${candidate.person_kind}` : ""}
                  {candidate.signal_sources?.role_identity_bridge ? " · role bridge" : ""}
                  {candidate.signal_sources?.self_identification_phrase ? " · self ID" : ""}
                </small>
                {candidate.basis?.length ? <small>{candidate.basis.slice(0, 2).join(" ")}</small> : null}
              </span>
            </label>
          );
        })}
      </div>
      <ActionRow>
        <CommandButton
          icon={UserCheck}
          label="Confirm Speaker"
          disabled={!selected}
          onClick={() =>
            selected &&
            onQueue({
              ...baseAction(item, "confirm_speaker_identity"),
              target_id: selected.speaker_identity_candidate_id ?? selected.id ?? item.source_id,
              payload: {
                speaker_label: selected.speaker_label,
                person_group_id: selected.person_group_id,
              },
            })
          }
        />
        <CommandButton
          icon={X}
          label="Reject Candidate"
          tone="danger"
          disabled={!selected}
          onClick={() =>
            selected &&
            onQueue({
              ...baseAction(item, "reject_speaker_identity"),
              target_id: selected.speaker_identity_candidate_id ?? selected.id ?? item.source_id,
              payload: {
                speaker_label: selected.speaker_label,
                person_group_id: selected.person_group_id,
              },
            })
          }
        />
        <CommandButton
          icon={XCircle}
          label="No Match"
          tone="danger"
          disabled={!candidates.length}
          onClick={() =>
            onQueue(
              candidates.map((candidate) => ({
                ...baseAction(item, "reject_speaker_identity"),
                id: `reject_${candidate.speaker_identity_candidate_id ?? candidate.id}`,
                target_id: candidate.speaker_identity_candidate_id ?? candidate.id ?? item.source_id,
                payload: {
                  speaker_label: candidate.speaker_label,
                  person_group_id: candidate.person_group_id,
                },
              })),
            )
          }
        />
      </ActionRow>
    </div>
  );
}

function PlaceActions({ item, onQueue }: { item: ReviewItem; onQueue: (action: ReviewAction) => Promise<void> }) {
  const candidate = item.candidate as {
    label?: string;
    display_label?: string;
    context?: { label?: string };
    evidence_basis?: PlaceRecord["evidence_basis"];
    location_options?: PlaceLocationOption[];
  };
  const options = normalizedPlaceOptions(candidate);
  const [selectedOptionId, setSelectedOptionId] = useState(options[0]?.id ?? "other");
  const selectedOption = options.find((option) => option.id === selectedOptionId);
  const [label, setLabel] = useState(selectedOption?.label ?? candidate.label ?? "");
  const [scope, setScope] = useState(selectedOption?.scope_label ?? candidate.context?.label ?? "");

  useEffect(() => {
    const nextOption = options[0];
    setSelectedOptionId(nextOption?.id ?? "other");
    setLabel(nextOption?.label ?? candidate.label ?? "");
    setScope(nextOption?.scope_label ?? candidate.context?.label ?? "");
  }, [item.id]);

  function selectOption(option: PlaceLocationOption | { id: "other" }) {
    setSelectedOptionId(option.id);
    if ("label" in option) {
      setLabel(option.label);
      setScope(option.scope_label ?? "");
    }
  }

  return (
    <div className="action-block">
      <div className="candidate-list">
        {options.map((option) => (
          <label key={option.id} className="candidate-option">
            <input
              type="radio"
              name={`place-option-${item.id}`}
              checked={selectedOptionId === option.id}
              onChange={() => selectOption(option)}
            />
            <span>
              <strong>{option.display_label || option.label}</strong>
              <small>
                {option.source_label ?? "from context"} · {formatConfidence(option.confidence)}
                {option.selected ? " · selected" : ""}
              </small>
              {option.basis?.length ? <small title={option.basis.join(" · ")}>{option.basis.join(" · ")}</small> : null}
            </span>
            <span className="candidate-info-wrap" title={[option.source_label, ...(option.basis ?? [])].filter(Boolean).join(" · ")}>
              <Info className="candidate-info" size={15} aria-label="Evidence source" />
            </span>
          </label>
        ))}
        <label className="candidate-option">
          <input
            type="radio"
            name={`place-option-${item.id}`}
            checked={selectedOptionId === "other"}
            onChange={() => selectOption({ id: "other" })}
          />
          <span>
            <strong>Other</strong>
            <small>Enter a corrected label or scope below.</small>
          </span>
        </label>
      </div>
      {candidate.evidence_basis?.summary ? <p className="basis-copy">{candidate.evidence_basis.summary}</p> : null}
      <div className="field-grid">
        <label>
          <span>Label</span>
          <input value={label} onChange={(event) => setLabel(event.target.value)} />
        </label>
        <label>
          <span>Scope</span>
          <input value={scope} onChange={(event) => setScope(event.target.value)} />
        </label>
      </div>
      <ActionRow>
        <CommandButton
          icon={Check}
          label="Confirm Place"
          onClick={() =>
            onQueue({
              ...baseAction(item, "confirm_place"),
              label,
              scope_label: scope,
              selected_location_option_id: selectedOptionId,
              selected_location_option: selectedOption,
            })
          }
        />
        <CommandButton
          icon={Pencil}
          label="Rename"
          onClick={() =>
            onQueue({
              ...baseAction(item, "rename_place"),
              label,
              scope_label: scope,
              selected_location_option_id: selectedOptionId,
              selected_location_option: selectedOption,
            })
          }
        />
        <CommandButton icon={X} label="Not Location" tone="danger" onClick={() => onQueue(baseAction(item, "mark_not_location"))} />
      </ActionRow>
    </div>
  );
}

function PersonActions({ item, onQueue }: { item: ReviewItem; onQueue: (action: ReviewAction) => Promise<void> }) {
  const candidate = item.candidate as {
    label?: string;
    role_identity_options?: Array<{
      person_group_id: string;
      label?: string;
      confidence?: number;
      object_label?: string;
      relationship_candidate_id?: string;
      basis?: string[];
    }>;
  };
  const roleOptions = candidate.role_identity_options ?? [];
  const [selectedRoleOptionId, setSelectedRoleOptionId] = useState(roleOptions[0]?.person_group_id ?? "");
  const [label, setLabel] = useState(candidate.label ?? item.title.replace("Resolve person: ", ""));
  const selectedRoleOption = roleOptions.find((option) => option.person_group_id === selectedRoleOptionId);
  return (
    <div className="action-block">
      {roleOptions.length ? (
        <div className="candidate-list">
          {roleOptions.map((option) => (
            <label key={option.person_group_id} className="candidate-option">
              <input
                type="radio"
                name={`role-identity-${item.id}`}
                checked={selectedRoleOptionId === option.person_group_id}
                onChange={() => setSelectedRoleOptionId(option.person_group_id)}
              />
              <span>
                <strong>
                  <PersonNameBadge label={option.label ?? ""} />
                </strong>
                <small>
                  role identity · {formatConfidence(option.confidence)}
                  {option.object_label ? ` · via ${personDisplayName(option.object_label)}` : ""}
                </small>
                {option.basis?.length ? <small title={option.basis.join(" · ")}>{option.basis.join(" · ")}</small> : null}
              </span>
            </label>
          ))}
        </div>
      ) : null}
      <label className="single-field">
        <span>Name</span>
        <input value={label} onChange={(event) => setLabel(event.target.value)} />
      </label>
      <ActionRow>
        {selectedRoleOption ? (
          <CommandButton
            icon={GitMerge}
            label="Merge Role"
            onClick={() =>
              onQueue({
                ...baseAction(item, "merge_person"),
                merge_with_person_group_id: selectedRoleOption.person_group_id,
                role_identity_option: selectedRoleOption,
              })
            }
          />
        ) : null}
        <CommandButton icon={Check} label="Confirm" onClick={() => onQueue({ ...baseAction(item, "confirm_person"), label })} />
        <CommandButton icon={Pencil} label="Rename" onClick={() => onQueue({ ...baseAction(item, "rename_person"), label })} />
        <CommandButton icon={GitMerge} label="Role Only" onClick={() => onQueue(baseAction(item, "mark_role_only"))} />
      </ActionRow>
    </div>
  );
}

function RelationshipActions({ item, onQueue }: { item: ReviewItem; onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void> }) {
  const candidate = item.candidate as {
    predicate?: string;
    subject_entity_id?: string;
    subject_label?: string;
    object_entity_id?: string;
    object_label?: string;
    relationship_ids?: string[];
    relationship_count?: number;
  };
  const relationshipIds = relationshipIdsFromItem(item);
  const payload = {
    predicate: candidate.predicate,
    subject_entity_id: candidate.subject_entity_id,
    subject_label: candidate.subject_label,
    object_entity_id: candidate.object_entity_id,
    object_label: candidate.object_label,
    relationship_ids: relationshipIds,
  };
  const confirmActions = relationshipIds.map((id) => ({
    action: "confirm_relationship",
    target_id: id,
    target_type: "relationship_candidate",
    payload,
  }));
  const rejectActions = relationshipIds.map((id) => ({
    action: "reject_relationship",
    target_id: id,
    target_type: "relationship_candidate",
    payload: { relationship_ids: relationshipIds },
  }));
  const count = candidate.relationship_count ?? relationshipIds.length;
  return (
    <div className="action-block">
      {count > 1 ? <p className="basis-copy">This decision applies to {count} supporting relationship observations.</p> : null}
      <ActionRow>
        <CommandButton icon={Check} label={count > 1 ? "Confirm All" : "Confirm"} onClick={() => onQueue(confirmActions.length === 1 ? confirmActions[0] : confirmActions)} />
        <CommandButton
          icon={X}
          label={count > 1 ? "Reject All" : "Reject"}
          tone="danger"
          onClick={() => onQueue(rejectActions.length === 1 ? rejectActions[0] : rejectActions)}
        />
      </ActionRow>
    </div>
  );
}

function EventActions({ item, onQueue }: { item: ReviewItem; onQueue: (action: ReviewAction) => Promise<void> }) {
  const candidate = item.candidate as { title?: string };
  const [title, setTitle] = useState(candidate.title ?? item.title.replace("Review event: ", ""));
  return (
    <div className="action-block">
      <label className="single-field">
        <span>Title</span>
        <input value={title} onChange={(event) => setTitle(event.target.value)} />
      </label>
      <ActionRow>
        <CommandButton icon={Check} label="Confirm" onClick={() => onQueue({ ...baseAction(item, "confirm_event"), title })} />
        <CommandButton icon={Pencil} label="Rename" onClick={() => onQueue({ ...baseAction(item, "rename_event"), title })} />
        <CommandButton icon={X} label="Unrelated" tone="danger" onClick={() => onQueue(baseAction(item, "mark_unrelated"))} />
      </ActionRow>
    </div>
  );
}

function DateActions({ item, onQueue }: { item: ReviewItem; onQueue: (action: ReviewAction) => Promise<void> }) {
  const candidate = item.candidate as { date_value?: string; precision?: string; label?: string };
  const [dateValue, setDateValue] = useState(candidate.date_value ?? "");
  const [precision, setPrecision] = useState(candidate.precision ?? "day");
  return (
    <div className="action-block">
      <div className="field-grid">
        <label>
          <span>Date</span>
          <input value={dateValue} onChange={(event) => setDateValue(event.target.value)} />
        </label>
        <label>
          <span>Precision</span>
          <select value={precision} onChange={(event) => setPrecision(event.target.value)}>
            <option value="day">Day</option>
            <option value="month">Month</option>
            <option value="year">Year</option>
            <option value="range">Range</option>
            <option value="unknown">Unknown</option>
          </select>
        </label>
      </div>
      <ActionRow>
        <CommandButton
          icon={Check}
          label="Event Date"
          onClick={() => onQueue({ ...baseAction(item, "confirm_event_date"), date_value: dateValue, precision })}
        />
        <CommandButton
          icon={CalendarDays}
          label="Context Only"
          onClick={() => onQueue({ ...baseAction(item, "mark_historical_context"), date_value: dateValue, precision })}
        />
      </ActionRow>
    </div>
  );
}

function ActionRow({ children }: { children: React.ReactNode }) {
  return <div className="action-row">{children}</div>;
}

function CommandButton({
  icon: Icon,
  label,
  tone,
  disabled,
  onClick,
}: {
  icon: typeof Check;
  label: string;
  tone?: "danger";
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button className={`command-button ${tone ?? ""}`} disabled={disabled} onClick={onClick}>
      <Icon size={16} />
      <span>{label}</span>
    </button>
  );
}

function EvidenceList({
  item,
  media,
  onPlay,
}: {
  item: ReviewItem;
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
}) {
  return (
    <div className="evidence-list">
      {item.events.map((event) => (
        <div key={event.event_id} className="evidence-row">
          <Clock3 size={14} />
          <span>{event.title}</span>
          <small>{event.source_video_ids.join(", ")} · {formatTime(event.start_s)}</small>
          <MomentButton event={event} media={media} onPlay={onPlay} />
        </div>
      ))}
    </div>
  );
}

function MomentButton({
  event,
  media,
  onPlay,
}: {
  event: EventEntry | EventRecord;
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
}) {
  const moment = momentFromEvent(event, media);
  if (!moment) {
    return null;
  }
  return (
    <button className="moment-button" onClick={() => onPlay(moment)} title={`Play ${moment.videoLabel} at ${formatTime(moment.startS)}`}>
      <Clock3 size={13} />
      <span>{formatTime(moment.startS)}</span>
    </button>
  );
}

const PLAYER_POS_KEY = "tapesplit.miniplayer.pos";

function readSavedPlayerPos(): { x: number; y: number } | null {
  try {
    const raw = localStorage.getItem(PLAYER_POS_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as { x?: number; y?: number };
    if (typeof parsed.x !== "number" || typeof parsed.y !== "number") {
      return null;
    }
    return clampPlayerPos(parsed as { x: number; y: number }, null);
  } catch {
    return null;
  }
}

function clampPlayerPos(pos: { x: number; y: number }, el: HTMLElement | null) {
  const width = el?.offsetWidth ?? 430;
  const height = el?.offsetHeight ?? 340;
  return {
    x: Math.min(Math.max(8, pos.x), Math.max(8, window.innerWidth - width - 8)),
    y: Math.min(Math.max(8, pos.y), Math.max(8, window.innerHeight - height - 8)),
  };
}

// The player floats bottom-right by default; grab its header to put it
// anywhere, double-click the header to send it home. Position survives
// reloads via localStorage; drags move via transform and bake to left/top
// on release.
function DraggableMiniplayer({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const dragState = useRef<{ pointerId: number; startX: number; startY: number; origin: { x: number; y: number } } | null>(null);
  const [pos, setPos] = useState(readSavedPlayerPos);
  const [dragging, setDragging] = useState(false);

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement;
    if (!target.closest(".panel-heading") || target.closest("button, a, input, video")) {
      return;
    }
    const el = ref.current;
    if (!el) {
      return;
    }
    const rect = el.getBoundingClientRect();
    const origin = { x: rect.left, y: rect.top };
    setPos(origin);
    dragState.current = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, origin };
    el.setPointerCapture(event.pointerId);
    setDragging(true);
    event.preventDefault();
  }

  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragState.current;
    const el = ref.current;
    if (!drag || !el || event.pointerId !== drag.pointerId) {
      return;
    }
    const next = clampPlayerPos(
      { x: drag.origin.x + event.clientX - drag.startX, y: drag.origin.y + event.clientY - drag.startY },
      el,
    );
    el.style.transform = `translate(${next.x - drag.origin.x}px, ${next.y - drag.origin.y}px)`;
  }

  function endDrag(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragState.current;
    if (!drag || event.pointerId !== drag.pointerId) {
      return;
    }
    dragState.current = null;
    setDragging(false);
    const el = ref.current;
    if (!el) {
      return;
    }
    const rect = el.getBoundingClientRect();
    el.style.transform = "";
    const next = clampPlayerPos({ x: rect.left, y: rect.top }, el);
    setPos(next);
    try {
      localStorage.setItem(PLAYER_POS_KEY, JSON.stringify(next));
    } catch {
      // storage unavailable; position just won't persist
    }
  }

  return (
    <div
      ref={ref}
      className={`miniplayer${dragging ? " dragging" : ""}`}
      style={pos ? { left: pos.x, top: pos.y, right: "auto", bottom: "auto" } : undefined}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={(event) => {
        if ((event.target as HTMLElement).closest(".panel-heading")) {
          setPos(null);
          try {
            localStorage.removeItem(PLAYER_POS_KEY);
          } catch {
            // storage unavailable
          }
        }
      }}
    >
      {children}
    </div>
  );
}

function VideoPlayerPanel({ moment, onClear }: { moment: PlayerMoment | null; onClear: () => void }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !moment) {
      return;
    }
    const seek = () => {
      video.currentTime = moment.startS;
      void video.play().catch(() => undefined);
    };
    if (video.readyState >= 1) {
      seek();
    } else {
      video.addEventListener("loadedmetadata", seek, { once: true });
      return () => video.removeEventListener("loadedmetadata", seek);
    }
  }, [moment]);

  return (
    <section className="side-panel player-panel">
      <div className="panel-heading">
        <h2>Player</h2>
        {moment && (
          <button className="text-button" onClick={onClear} title="Clear player">
            <X size={14} />
          </button>
        )}
      </div>
      {moment ? (
        <>
          <video key={moment.videoId} ref={videoRef} controls preload="metadata" src={videoUrl(moment.videoId)} />
          <div className="player-meta">
            <strong>{moment.title}</strong>
            <small>
              {moment.videoLabel} · {formatTime(moment.startS)}
              {typeof moment.endS === "number" ? `-${formatTime(moment.endS)}` : ""}
            </small>
          </div>
        </>
      ) : (
        <p className="empty-copy">Select any timestamp to preview the source tape.</p>
      )}
    </section>
  );
}

function PendingActionsPanel({
  actions,
  busy,
  onApply,
  onReapply,
  onRemove,
}: {
  actions: ReviewAction[];
  busy: boolean;
  onApply: () => Promise<void>;
  onReapply: () => Promise<void>;
  onRemove: (id?: string) => Promise<void>;
}) {
  return (
    <section className="side-panel">
      <div className="panel-heading">
        <h2>Pending</h2>
        <span>{actions.length}</span>
      </div>
      <div className="pending-actions">
        {actions.slice(0, 12).map((action, index) => (
          <div key={String(action.id ?? index)} className="pending-action">
            <span>{String(action.action)}</span>
            <small>{String(action.target_id)}</small>
            <button className="text-button" onClick={() => onRemove(String(action.id))} title="Remove action">
              <Trash2 size={14} />
            </button>
          </div>
        ))}
        {!actions.length && <p className="empty-copy">No pending actions.</p>}
      </div>
      <div className="pending-footer">
        <button className="command-button" disabled={!actions.length || busy} onClick={() => void onApply()}>
          <Send size={16} />
          <span>Apply</span>
        </button>
        <button className="command-button secondary" disabled={!actions.length || busy} onClick={() => void onRemove()}>
          <Trash2 size={16} />
          <span>Clear</span>
        </button>
        <button className="command-button secondary" disabled={busy} onClick={() => void onReapply()} title="Replay corrections and refresh review data">
          <RefreshCw size={16} />
          <span>Reapply</span>
        </button>
      </div>
    </section>
  );
}

function ContextPanel({
  item,
  events,
  media,
  onPlay,
}: {
  item: ReviewItem | null;
  events: EventRecord[];
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
}) {
  const related = item?.related_event_ids?.map((id) => events.find((event) => event.id === id)).filter(Boolean) as EventRecord[] | undefined;
  return (
    <section className="side-panel context-panel">
      <div className="panel-heading">
        <h2>Context</h2>
      </div>
      {!item && <p className="empty-copy">No item selected.</p>}
      {item && (
        <>
          <dl className="metadata-list">
            <div>
              <dt>Source</dt>
              <dd>{item.source_id}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{item.review_status}</dd>
            </div>
            <div>
              <dt>Priority</dt>
              <dd>{item.priority}</dd>
            </div>
          </dl>
          <div className="mini-events">
            {(related ?? []).slice(0, 6).map((event) => (
              <div key={event.id} className="mini-event">
                {event.thumbnail_path ? <img src={assetUrl(event.thumbnail_path)} alt="" /> : <span />}
                <div>
                  <strong>{event.title}</strong>
                  <small>{event.event_type ?? "event"} · {formatTime(event.start_s)}</small>
                </div>
                <MomentButton event={event} media={media} onPlay={onPlay} />
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function TimelineView({
  events,
  media,
  pendingActions,
  onPlay,
  onQueue,
}: {
  events: EventRecord[];
  media: MediaRecord[];
  pendingActions: ReviewAction[];
  onPlay: (moment: PlayerMoment) => void;
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
  return (
    <section className="timeline-view">
      {events.map((event) => {
        const pending = eventHasPendingAction(event, pendingActions);
        const displaySummary = event.reconciliation?.reconciled_summary || event.summary;
        return (
          <article key={event.id} className={`timeline-row ${event.reconciliation ? "has-reconciliation" : ""}`}>
            <div className="timeline-thumb">
              {event.thumbnail_path ? <img src={assetUrl(event.thumbnail_path)} alt="" /> : <ImageIcon size={22} />}
            </div>
            <div className="timeline-copy">
              <div className="row-heading">
                <div className="event-title-stack">
                  <h2>{event.title}</h2>
                  {event.original_title && normalizeLabel(event.original_title) !== normalizeLabel(event.title) ? (
                    <small>original model title: {event.original_title}</small>
                  ) : null}
                </div>
                <div className="row-actions">
                  {pending && <span className="pending-pill">Queued</span>}
                  <MomentButton event={event} media={media} onPlay={onPlay} />
                </div>
              </div>
              <p>{displaySummary}</p>
              <div className="token-row">
                <Token>{event.event_type ?? "event"}</Token>
                <Token>{event.review_status}</Token>
                {event.reconciliation?.reconciliation_status ? <Token>{reconciliationLabel(event.reconciliation.reconciliation_status)}</Token> : null}
                {event.people.slice(0, 4).map((person) => (
                  <Token key={person.id}>{personDisplayName(person.label)}</Token>
                ))}
                {event.places.slice(0, 3).map((place) => (
                  <Token key={place.id}>{placeDisplayLabel(place.label)}</Token>
                ))}
              </div>
              <EventIntelligencePanel event={event} media={media} pending={pending} onPlay={onPlay} onQueue={onQueue} />
            </div>
          </article>
        );
      })}
    </section>
  );
}

function EventIntelligencePanel({
  event,
  media,
  pending,
  onPlay,
  onQueue,
}: {
  event: EventRecord;
  media: MediaRecord[];
  pending: boolean;
  onPlay: (moment: PlayerMoment) => void;
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
  const reconciliation = event.reconciliation;
  const alignment = event.alignment;
  if (!reconciliation && !alignment) {
    return null;
  }

  const originalTitle = reconciliation?.original_title || event.original_title || "";
  const titleChanged = Boolean(originalTitle && normalizeLabel(originalTitle) !== normalizeLabel(event.title));
  const selectedPlaces = uniqueStrings(reconciliation?.selected_place_labels ?? []);
  const rejectedPlaces = uniqueStrings(reconciliation?.rejected_place_labels ?? []);
  const anchors = alignment?.transcript_context_anchors ?? [];
  const statusTitle = [...(reconciliation?.signals ?? []), ...(alignment?.signals ?? [])].join(" · ");

  return (
    <div className="event-intelligence">
      <div className="event-intelligence-head">
        <div className="intelligence-badges">
          {reconciliation?.reconciliation_status ? (
            <span className={`status-badge ${reconciliation.reconciliation_status}`}>
              {reconciliation.reconciliation_status === "corrected" ? "Appears to be corrected" : reconciliationLabel(reconciliation.reconciliation_status)}
            </span>
          ) : null}
          {alignment?.timing_status ? <span className="status-badge neutral">{predicateLabel(alignment.timing_status)}</span> : null}
          {typeof reconciliation?.confidence === "number" ? <span className="status-badge neutral">{formatConfidence(reconciliation.confidence)}</span> : null}
        </div>
        {statusTitle ? (
          <span className="evidence-source" title={statusTitle}>
            <Info size={14} />
            context
          </span>
        ) : null}
      </div>

      {titleChanged ? (
        <div className="title-rewrite">
          <span>Model title</span>
          <strong>{originalTitle}</strong>
        </div>
      ) : null}

      {selectedPlaces.length || rejectedPlaces.length ? (
        <div className="place-decision-grid">
          {selectedPlaces.length ? (
            <div className="place-decision selected">
              <MapPin size={14} />
              <span>Selected filming context</span>
              <strong>{selectedPlaces.join(", ")}</strong>
            </div>
          ) : null}
          {rejectedPlaces.length ? (
            <div className="place-decision rejected">
              <XCircle size={14} />
              <span>Mentioned or off-window</span>
              <strong>{rejectedPlaces.join(", ")}</strong>
            </div>
          ) : null}
        </div>
      ) : null}

      {anchors.length ? (
        <div className="context-anchor-grid">
          {anchors.slice(0, 4).map((anchor, index) => (
            <div key={`${anchor.label ?? "anchor"}-${index}`} className="context-anchor" title={anchor.text || ""}>
              <strong>{anchor.label || "Context anchor"}</strong>
              <small>
                {predicateLabel(anchor.role)} · {formatConfidence(anchor.confidence)}
                {typeof anchor.distance_to_event_s === "number" ? ` · ${formatSignedSeconds(anchor.distance_to_event_s)}` : ""}
              </small>
              {anchor.text ? <span>{anchor.text}</span> : null}
            </div>
          ))}
        </div>
      ) : null}

      <div className="range-groups">
        <SourceRangeGroup
          label="Event window"
          ranges={reconciliation?.selected_source_ranges ?? []}
          media={media}
          onPlay={onPlay}
          title={event.title}
        />
        <SourceRangeGroup
          label="Relocated evidence"
          ranges={reconciliation?.relocated_evidence_ranges ?? alignment?.suggested_source_ranges ?? []}
          media={media}
          onPlay={onPlay}
          title={`${event.title} evidence`}
        />
      </div>

      {reconciliation?.warnings?.length || alignment?.warnings?.length ? (
        <div className="warning-strip">
          <AlertTriangle size={14} />
          <span>{[...(reconciliation?.warnings ?? []), ...(alignment?.warnings ?? [])].slice(0, 2).join(" ")}</span>
        </div>
      ) : null}

      <ActionRow>
        <CommandButton
          icon={Check}
          label="Accept Guess"
          disabled={pending}
          onClick={() => onQueue(confirmEventAction(event, event.title, event.reconciliation?.reconciled_summary))}
        />
        {titleChanged ? (
          <CommandButton
            icon={Pencil}
            label="Keep Original"
            disabled={pending}
            onClick={() => onQueue(confirmEventAction(event, originalTitle, event.summary))}
          />
        ) : null}
        <CommandButton
          icon={X}
          label="Unrelated"
          tone="danger"
          disabled={pending}
          onClick={() => onQueue({ action: "mark_unrelated", target_id: event.id, target_type: "event" })}
        />
      </ActionRow>
    </div>
  );
}

function SourceRangeGroup({
  label,
  ranges,
  media,
  onPlay,
  title,
}: {
  label: string;
  ranges: SourceRange[];
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
  title: string;
}) {
  if (!ranges.length) {
    return null;
  }
  return (
    <div className="range-group">
      <span>{label}</span>
      <div>
        {ranges.slice(0, 3).map((range, index) => (
          <RangeMomentButton key={`${range.source_video_id}-${range.start_s ?? 0}-${index}`} range={range} media={media} onPlay={onPlay} title={title} />
        ))}
      </div>
    </div>
  );
}

function RangeMomentButton({
  range,
  media,
  onPlay,
  title,
}: {
  range: SourceRange;
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
  title: string;
}) {
  const moment = momentFromSourceRange(range, media, title);
  if (!moment) {
    return null;
  }
  const rangeLabel =
    typeof moment.endS === "number" && moment.endS !== moment.startS
      ? `${formatTime(moment.startS)}-${formatTime(moment.endS)}`
      : formatTime(moment.startS);
  const source = [moment.videoLabel, range.basis ? predicateLabel(range.basis) : "", formatConfidence(range.confidence)]
    .filter((value) => value && value !== "n/a")
    .join(" · ");
  return (
    <button className="moment-button range-button" onClick={() => onPlay(moment)} title={source}>
      <Clock3 size={13} />
      <span>{rangeLabel}</span>
    </button>
  );
}

function SearchView({
  query,
  results,
  busy,
  error,
  media,
  onQueryChange,
  onSearch,
  onPlay,
}: {
  query: string;
  results: SearchResult[];
  busy: boolean;
  error: string;
  media: MediaRecord[];
  onQueryChange: (value: string) => void;
  onSearch: (query?: string) => Promise<void>;
  onPlay: (moment: PlayerMoment) => void;
}) {
  return (
    <section className="search-view">
      <form
        className="search-command"
        onSubmit={(event) => {
          event.preventDefault();
          void onSearch(query);
        }}
      >
        <Search size={18} />
        <input value={query} onChange={(event) => onQueryChange(event.target.value)} placeholder="Search tape" />
        <button className="command-button" disabled={busy || !query.trim()}>
          <Search size={16} />
          <span>{busy ? "Searching" : "Search"}</span>
        </button>
      </form>
      {error ? (
        <div className="status error">
          <AlertTriangle size={14} />
          {error}
        </div>
      ) : null}
      <div className="search-results">
        {results.map((result) => (
          <SearchResultRow key={`${result.record_type}:${result.source_id}`} result={result} media={media} onPlay={onPlay} />
        ))}
        {!results.length && !busy ? <EmptyState icon={Search} title="No search results" /> : null}
      </div>
    </section>
  );
}

function SearchResultRow({
  result,
  media,
  onPlay,
}: {
  result: SearchResult;
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
}) {
  const sourceRange: SourceRange | null = result.source_video_id
    ? {
        source_video_id: result.source_video_id,
        start_s: result.start_s,
        end_s: result.end_s,
      }
    : null;
  return (
    <article className="search-result-row">
      <div className="search-result-main">
        <div className="row-heading">
          <h2>{result.title}</h2>
          {sourceRange ? <RangeMomentButton range={sourceRange} media={media} onPlay={onPlay} title={result.title} /> : null}
        </div>
        {result.snippet ? <p>{result.snippet}</p> : null}
        <div className="token-row">
          <Token>{predicateLabel(result.record_type)}</Token>
          <Token>{formatScore(result.score)}</Token>
          {result.time_label ? <Token>{result.time_label}</Token> : null}
          {result.source_video_id ? <Token>{result.source_video_id}</Token> : null}
        </div>
      </div>
    </article>
  );
}

function AlbumsView({
  albums,
  events,
  media,
  onPlay,
}: {
  albums: AlbumRecord[];
  events: EventRecord[];
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
}) {
  const eventsById = useMemo(() => new Map(events.map((event) => [event.id, event])), [events]);
  const visibleAlbums = dedupeAlbumsForDisplay(albums.filter((album) => album.events?.length || album.thumbnail_path));
  return (
    <section className="albums-view">
      {visibleAlbums.map((album) => (
        <article key={album.id} className="album-row">
          <div className="album-cover">
            {album.thumbnail_path ? <img src={assetUrl(album.thumbnail_path)} alt="" /> : <AlbumCoverFallback album={album} eventsById={eventsById} />}
          </div>
          <div className="album-body">
            <div className="row-heading">
              <div className="event-title-stack">
                <h2>{album.title}</h2>
                <small>{[album.date_label, placeDisplayLabel(album.place_label)].filter(Boolean).join(" · ") || humanizeToken(album.album_type ?? "album")}</small>
              </div>
              <span>{(album.events?.length ?? 0) === 1 ? "1 event" : `${album.events?.length ?? 0} events`}</span>
            </div>
            <div className="token-row">
              <Token>{humanizeToken(album.album_type ?? "album")}</Token>
              {(album.people_labels ?? []).slice(0, 5).map((label) => (
                <Token key={label}>{personDisplayName(label)}</Token>
              ))}
            </div>
            <div className="album-events">
              {(album.events ?? []).slice(0, 8).map((event) => {
                const fullEvent = eventsById.get(event.event_id);
                const moment = momentFromEvent(event, media);
                return (
                  <button key={`${album.id}-${event.event_id}`} className="album-event-tile" onClick={() => playEvent(event, media, onPlay)}>
                    {fullEvent?.thumbnail_path ? <img src={assetUrl(fullEvent.thumbnail_path)} alt="" /> : <ImageIcon size={18} />}
                    <span>{event.title}</span>
                    <small>{moment ? formatTime(moment.startS) : ""}</small>
                  </button>
                );
              })}
            </div>
          </div>
        </article>
      ))}
      {!visibleAlbums.length ? <EmptyState icon={CalendarDays} title="No albums" /> : null}
    </section>
  );
}

// The richest dated day-albums are ready-made Memories; surface a handful
// as the library's hero strip.
function memoriesRowAlbums(albums: AlbumRecord[]): AlbumRecord[] {
  return dedupeAlbumsForDisplay(albums.filter((album) => album.date_label && (album.events?.length ?? 0) >= 2))
    .sort((a, b) => (b.events?.length ?? 0) - (a.events?.length ?? 0))
    .slice(0, 6);
}

function albumCoverPath(album: AlbumRecord, eventsById: Map<string, EventRecord>, avoid?: Set<string>): string {
  const candidates = [
    album.thumbnail_path ?? "",
    ...(album.events ?? []).map((entry) => eventsById.get(entry.event_id)?.thumbnail_path ?? ""),
  ].filter(Boolean);
  return candidates.find((path) => !avoid?.has(path)) ?? candidates[0] ?? "";
}

// Day-albums carry every constituent date ("MAY 10 2002, MAY 11 2002, ...");
// a Memory reads as a span. Dates themselves may contain commas ("March 22,
// 2006"), so split only at commas that start a new month token.
const MONTH_BOUNDARY = /,\s*(?=(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d)/i;

function memoryDateLabel(album: AlbumRecord): string {
  const label = album.date_label ?? "";
  const parts = label.split(MONTH_BOUNDARY).map((part) => part.trim()).filter(Boolean);
  if (parts.length <= 1) {
    return label;
  }
  return `${parts[0]} – ${parts[parts.length - 1]}`;
}

function dedupeAlbumsForDisplay(albums: AlbumRecord[]) {
  const buckets = new Map<string, AlbumRecord>();
  for (const album of albums) {
    const eventIds = (album.events ?? []).map((event) => event.event_id).sort();
    const key = eventIds.length ? `${album.album_type ?? "album"}:${eventIds.join("|")}` : album.id;
    const current = buckets.get(key);
    if (!current || albumDisplayRank(album) > albumDisplayRank(current)) {
      buckets.set(key, album);
    }
  }
  return [...buckets.values()];
}

function albumDisplayRank(album: AlbumRecord) {
  let rank = 0;
  if (album.thumbnail_path) rank += 10;
  if (album.review_status === "unreviewed") rank += 4;
  if (album.date_label && !album.date_label.includes(",")) rank += 2;
  rank += Math.min(album.events?.length ?? 0, 5) * 0.1;
  return rank;
}

function AlbumCoverFallback({ album, eventsById }: { album: AlbumRecord; eventsById: Map<string, EventRecord> }) {
  const event = (album.events ?? []).map((entry) => eventsById.get(entry.event_id)).find((entry) => entry?.thumbnail_path);
  if (event?.thumbnail_path) {
    return <img src={assetUrl(event.thumbnail_path)} alt="" />;
  }
  return <CalendarDays size={24} />;
}

function PlacesView({
  contexts,
  places,
  events,
  media,
  onPlay,
}: {
  contexts: PlaceContext[];
  places: PlaceRecord[];
  events: EventRecord[];
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
}) {
  const eventsById = useMemo(() => new Map(events.map((event) => [event.id, event])), [events]);
  return (
    <section className="entity-view">
      <div className="context-grid">
        {contexts.map((context) => (
          <article key={context.id} className="context-block">
            <div className="row-heading">
              <h2>{contextDisplayLabel(context.label)}</h2>
              <span>{context.place_count === 1 ? "1 place" : `${context.place_count} places`}</span>
            </div>
            <div className="moment-strip">
              {context.events.slice(0, 5).map((event) => {
                const fullEvent = eventsById.get(event.event_id);
                return (
                  <button key={event.event_id} className="moment-tile" onClick={() => playEvent(event, media, onPlay)}>
                    {fullEvent?.thumbnail_path ? <img src={assetUrl(fullEvent.thumbnail_path)} alt="" /> : <ImageIcon size={18} />}
                    <span>{event.title}</span>
                  </button>
                );
              })}
            </div>
            <div className="place-list">
              {context.places.map((place) => (
                <div key={place.id} className="place-row">
                  <PlaceThumb place={places.find((row) => row.id === place.id)} eventsById={eventsById} />
                  <span>{placeDisplayLabel(place.display_label)}</span>
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>
      <div className="flat-list">
        {places.map((place) => (
          <div key={place.id} className="flat-row">
            <PlaceThumb place={place} eventsById={eventsById} />
            <span>{placeDisplayLabel(place.display_label)}</span>
            {place.place_type && <small>{humanizeToken(place.place_type)}</small>}
          </div>
        ))}
      </div>
    </section>
  );
}

function PeopleView({
  people,
  media,
  onPlay,
}: {
  people: PersonRecord[];
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
}) {
  return (
    <section className="people-grid">
      {people.map((person) => (
        <article key={person.id} className="person-row">
          <div className="person-avatar">
            {primaryPersonThumb(person) ? <img src={assetUrl(primaryPersonThumb(person))} alt="" /> : <Users size={18} />}
          </div>
          <div>
            <h2>{person.label}</h2>
            <small>{person.kind} · {person.review_status}</small>
            <FaceThumbStrip person={person} />
            <div className="token-row">
              {(person.aliases ?? []).slice(0, 4).map((alias) => <Token key={alias}>{alias}</Token>)}
              {(person.candidate_face_clusters ?? []).slice(0, 2).map((cluster) => (
                <Token key={cluster.face_cluster_id}>{faceClusterTokenLabel(cluster)}</Token>
              ))}
            </div>
            <div className="person-moments">
              {(person.appearances ?? []).slice(0, 3).map((event) => (
                <MomentButton key={event.event_id} event={event} media={media} onPlay={onPlay} />
              ))}
            </div>
          </div>
        </article>
      ))}
    </section>
  );
}

function FaceThumbStrip({ person }: { person: PersonRecord }) {
  const clusters = person.candidate_face_clusters ?? [];
  const thumbs = personFaceThumbs(person);
  if (!thumbs.length) {
    return null;
  }
  return (
    <div className="face-thumb-strip">
      {thumbs.slice(0, 5).map((path) => {
        const cluster = clusters.find((item) => item.thumbnail_path === path);
        const title = cluster
          ? `Candidate face, not confirmed. ${qualityShortLabel(cluster.quality_status)}${cluster.review_only ? ". Review-only crop." : ""}`
          : "Candidate face, not confirmed";
        return <img key={path} src={assetUrl(path)} alt="" title={title} />;
      })}
      <small>candidate faces</small>
    </div>
  );
}

function PlaceThumb({ place, eventsById }: { place?: PlaceRecord; eventsById: Map<string, EventRecord> }) {
  const thumb = firstPlaceThumbnail(place, eventsById);
  return <span className="inline-place-thumb">{thumb ? <img src={assetUrl(thumb)} alt="" /> : <MapPin size={15} />}</span>;
}

function EmptyState({ icon: Icon, title }: { icon: typeof Inbox; title: string }) {
  return (
    <div className="empty-state">
      <Icon size={28} />
      <h2>{title}</h2>
    </div>
  );
}

function Token({ children }: { children: React.ReactNode }) {
  return <span className="token">{children}</span>;
}

function baseAction(item: ReviewItem, action: string): ReviewAction {
  return {
    action,
    target_id: item.source_id,
    target_type: item.source_record_type,
  };
}

function reviewActionsFromSuggestion(suggestion: SuggestedReviewAction): ReviewAction[] {
  const payload = suggestion.payload ?? {};
  const relationshipIds = Array.isArray(payload.relationship_ids) ? payload.relationship_ids.map(String).filter(Boolean) : [];
  if (suggestion.action === "confirm_relationship" && relationshipIds.length > 1) {
    return relationshipIds.map((relationshipId) => ({
      action: suggestion.action,
      target_id: relationshipId,
      target_type: suggestion.target_type,
      payload,
      notes: suggestion.rationale ?? "",
    }));
  }
  return [
    {
      action: suggestion.action,
      target_id: suggestion.target_id,
      target_type: suggestion.target_type,
      payload,
      notes: suggestion.rationale ?? "",
    },
  ];
}

function countSuggestedActions(items: ReviewItem[]) {
  return items.reduce((count, item) => count + (item.suggested_action ? reviewActionsFromSuggestion(item.suggested_action).length : 0), 0);
}

function relationshipIdsFromItem(item: ReviewItem) {
  const candidate = item.candidate as { relationship_ids?: unknown };
  const ids = Array.isArray(candidate.relationship_ids) ? candidate.relationship_ids.map(String).filter(Boolean) : [];
  return ids.length ? ids : [item.source_id];
}

function identityCandidates(item: ReviewItem) {
  const candidate = item.candidate as {
    identity_candidates?: Array<{
      face_identity_candidate_id: string;
      person_group_id: string;
      person_label: string;
      confidence?: number;
      supporting_event_titles: string[];
      direct_name_event_ids?: string[];
      direct_name_strength?: number;
      candidate_ambiguity?: string;
      average_event_people_count?: number;
      face_quality_status?: string;
      face_quality_notes?: string[];
      basis?: string[];
    }>;
  };
  return candidate.identity_candidates ?? [];
}

function speakerIdentityCandidates(item: ReviewItem): SpeakerIdentityCandidate[] {
  const candidate = item.candidate as {
    identity_candidates?: SpeakerIdentityCandidate[];
  };
  return candidate.identity_candidates ?? [];
}

function normalizedPlaceOptions(candidate: {
  label?: string;
  display_label?: string;
  context?: { label?: string };
  location_options?: PlaceLocationOption[];
}): PlaceLocationOption[] {
  if (candidate.location_options?.length) {
    return candidate.location_options;
  }
  const label = candidate.label ?? candidate.display_label ?? "";
  return [
    {
      id: "selected",
      label,
      display_label: candidate.display_label ?? label,
      scope_label: candidate.context?.label ?? "",
      source_label: "from context",
      selected: true,
    },
  ];
}

function itemHasPendingAction(item: ReviewItem, actions: ReviewAction[]) {
  const targetIds = new Set([item.source_id]);
  if (item.task_type === "resolve_face_cluster") {
    identityCandidates(item).forEach((candidate) => targetIds.add(candidate.face_identity_candidate_id));
  }
  if (item.task_type === "resolve_speaker") {
    speakerIdentityCandidates(item).forEach((candidate) => {
      targetIds.add(candidate.speaker_identity_candidate_id ?? candidate.id ?? "");
    });
  }
  if (item.task_type === "confirm_relationship") {
    relationshipIdsFromItem(item).forEach((relationshipId) => targetIds.add(relationshipId));
  }
  return actions.some((action) => targetIds.has(String(action.target_id)));
}

function eventHasPendingAction(event: EventRecord, actions: ReviewAction[]) {
  return actions.some((action) => String(action.target_id) === event.id);
}

function confirmEventAction(event: EventRecord, title: string, summary?: string): ReviewAction {
  return {
    action: "confirm_event",
    target_id: event.id,
    target_type: "event",
    payload: {
      title,
      summary,
      event_type: event.event_type,
      relatedness: event.relatedness,
    },
  };
}

function playEvent(event: EventEntry | EventRecord, media: MediaRecord[], onPlay: (moment: PlayerMoment) => void) {
  const moment = momentFromEvent(event, media);
  if (moment) {
    onPlay(moment);
  }
}

function momentFromEvent(event: EventEntry | EventRecord, media: MediaRecord[]): PlayerMoment | null {
  const videoId = event.source_video_ids[0];
  if (!videoId) {
    return null;
  }
  const tape = media.find((row) => row.id === videoId);
  const offset = tape?.offset_s ?? 0;
  const start = Math.max(0, (event.start_s ?? offset) - offset);
  const end = typeof event.end_s === "number" ? Math.max(start, event.end_s - offset) : undefined;
  return {
    videoId,
    videoLabel: tape?.filename || videoId,
    startS: start,
    endS: end,
    title: event.title,
  };
}

function momentFromSourceRange(range: SourceRange, media: MediaRecord[], title: string): PlayerMoment | null {
  const videoId = range.source_video_id;
  if (!videoId) {
    return null;
  }
  const tape = media.find((row) => row.id === videoId);
  const start = normalizeSourceSeconds(range.start_s ?? 0, tape);
  const rawEnd = typeof range.end_s === "number" ? normalizeSourceSeconds(range.end_s, tape) : undefined;
  const end = typeof rawEnd === "number" ? Math.max(start, rawEnd) : undefined;
  return {
    videoId,
    videoLabel: tape?.filename || videoId,
    startS: start,
    endS: end,
    title,
  };
}

function normalizeSourceSeconds(value: number, media?: MediaRecord) {
  const offset = media?.offset_s ?? 0;
  const duration = media?.duration_s ?? 0;
  if (offset > 0 && duration > 0 && value > duration + 5) {
    return Math.max(0, value - offset);
  }
  return Math.max(0, value);
}

function findPerson(people: PersonRecord[], id?: string, label?: string) {
  const byId = id ? people.find((person) => person.id === id) : undefined;
  if (byId) {
    return byId;
  }
  const normalized = normalizeLabel(label);
  if (!normalized) {
    return undefined;
  }
  return people.find((person) => {
    const labels = [person.label, ...(person.aliases ?? [])].map(normalizeLabel);
    return labels.includes(normalized);
  });
}

function primaryPersonThumb(person?: PersonRecord) {
  return personFaceThumbs(person)[0] || "";
}

function personFaceThumbs(person?: PersonRecord) {
  if (!person) {
    return [];
  }
  return uniqueStrings([
    person.thumbnail_path || "",
    ...((person.candidate_face_clusters ?? []).map((cluster) => cluster.thumbnail_path || "")),
  ]);
}

function faceClusterTokenLabel(cluster: NonNullable<PersonRecord["candidate_face_clusters"]>[number]) {
  const count = cluster.face_count ?? 0;
  const quality = cluster.quality_status && cluster.quality_status !== "usable" ? `${qualityShortLabel(cluster.quality_status)} ` : "";
  return `${count} ${quality}${count === 1 ? "face" : "faces"}`;
}

function qualityShortLabel(value?: string) {
  if (!value) {
    return "unknown quality";
  }
  return value.replaceAll("_", " ");
}

function firstPlaceThumbnail(place: PlaceRecord | undefined, eventsById: Map<string, EventRecord>) {
  if (!place) {
    return "";
  }
  for (const appearance of place.appearances ?? []) {
    const event = eventsById.get(appearance.event_id);
    if (event?.thumbnail_path) {
      return event.thumbnail_path;
    }
  }
  return "";
}

function uniqueStrings(values: string[]) {
  return values.filter((value, index) => value && values.indexOf(value) === index);
}

function normalizeLabel(value?: string) {
  return String(value || "").toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

function predicateLabel(value?: string) {
  return String(value || "related").replace(/_/g, " ");
}

function reconciliationLabel(value?: string) {
  const label = predicateLabel(value);
  if (value === "needs_evidence") {
    return "needs evidence";
  }
  return label;
}

function countBy<T>(rows: T[], getKey: (row: T) => string) {
  return rows.reduce<Record<string, number>>((acc, row) => {
    const key = getKey(row);
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
}

function taskLabel(task: string) {
  return taskLabels[task] ?? task.replace(/_/g, " ");
}

function formatConfidence(value?: number) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "n/a";
}

function formatScore(value?: number) {
  return typeof value === "number" ? `score ${value.toFixed(2)}` : "score n/a";
}

function formatTime(value?: number) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "0:00";
  const total = Math.max(0, Math.floor(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function formatSignedSeconds(value: number) {
  const rounded = Math.round(value);
  if (rounded === 0) {
    return "at event";
  }
  return `${rounded > 0 ? "+" : ""}${rounded}s`;
}

function shortPath(path: string) {
  const parts = path.split("/");
  return parts.slice(-2).join("/");
}
