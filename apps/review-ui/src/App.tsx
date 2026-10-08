import {
  AlertTriangle,
  AudioLines,
  BookImage,
  BookOpen,
  CalendarDays,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Film,
  GitMerge,
  HelpCircle,
  ImageIcon,
  Images,
  Info,
  Inbox,
  MoreHorizontal,
  ListFilter,
  MapPin,
  MapPinned,
  Pencil,
  Play,
  RefreshCw,
  ScanFace,
  Search,
  Send,
  Trash2,
  UserCheck,
  UserRound,
  Users,
  UsersRound,
  X,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactElement, ReactNode } from "react";
import { PlacesMapView } from "./PlacesMap";
import { lineLanguage, momentDate, momentRange, stampLabel, VoicesPane, WhenPane, WherePane } from "./moment";
import { MONTHS, formatDateValue, tapeDisplayLabel, humanizeToken, personDisplayName, humanVoiceLabel, DISJOINT_REGIONS, contextDisplayLabel, titleCasePlace, placeDisplayLabel, uniqueStrings, formatTime } from "./format";
import { AppIcon, Segmented, ShellContext, Toolbar } from "./shell";
import {
  applyReviewActions,
  applySuggestedReviewActions,
  assetUrl,
  loadJournalPosts,
  loadProject,
  queueReviewAction,
  reapplyReviewCorrections,
  removeReviewAction,
  searchProject,
  videoUrl,
} from "./api";
import { semanticSearchProject } from "./api";
import type {
  AlbumRecord,
  ContinuityContextAsset,
  DateRef,
  EventEntry,
  EventRecord,
  FaceObservation,
  JournalBlock,
  JournalEntity,
  JournalPost,
  MediaRecord,
  PersonRecord,
  PlaceContext,
  PlaceRoleAsset,
  PlaceLocationOption,
  PlaceRecord,
  ProjectBundle,
  ReviewAction,
  ReviewItem,
  SceneRecord,
  SearchResult,
  SemanticHit,
  SemanticResponse,
  SemanticSectionId,
  SourceRange,
  SpeakerIdentityCandidate,
  SpeakerSegment,
  SuggestedReviewAction,
  TaskType,
  VisualAsset,
} from "./types";

type ViewMode = "library" | "people" | "places" | "albums" | "journal" | "search";
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
  resolve_speaker: "Voices",
  confirm_place_context: "Place Links",
  confirm_relationship: "Relationships",
  resolve_place: "Places",
  resolve_person: "People",
  resolve_duplicate_person: "Same Person",
  review_event: "Moments",
  resolve_date: "Dates",
};

// Each review type keeps one glyph and one color everywhere it appears.
const taskIcons: Record<string, typeof Inbox> = {
  resolve_face_cluster: ScanFace,
  resolve_speaker: AudioLines,
  confirm_place_context: MapPinned,
  confirm_relationship: UsersRound,
  resolve_place: MapPin,
  resolve_person: UserRound,
  resolve_duplicate_person: GitMerge,
  review_event: Film,
  resolve_date: CalendarDays,
};

// The source list: colored glyphs, grouped the way Photos groups its sidebar.
const viewLabels: Array<{ id: ViewMode; label: string; icon: typeof Inbox; section: string }> = [
  { id: "library", label: "Library", icon: Images, section: "Archive" },
  { id: "people", label: "People", icon: UsersRound, section: "Archive" },
  { id: "places", label: "Places", icon: MapPinned, section: "Archive" },
  { id: "albums", label: "Albums", icon: BookImage, section: "Collections" },
  { id: "journal", label: "Journal", icon: BookOpen, section: "Collections" },
  { id: "search", label: "Search", icon: Search, section: "Collections" },
];

const initialView = ((): ViewMode => {
  const requested = new URLSearchParams(window.location.search).get("view");
  return (viewLabels.some((item) => item.id === requested) ? requested : "library") as ViewMode;
})();

const initialSearchText = new URLSearchParams(window.location.search).get("q") ?? "";

export function App() {
  const [bundle, setBundle] = useState<ProjectBundle | null>(null);
  const [view, setView] = useState<ViewMode>(initialView);
  const [reviewScope, setReviewScope] = useState<ReviewScope>("primary");
  const [taskFilter, setTaskFilter] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string>("");
  const [activeMoment, setActiveMoment] = useState<PlayerMoment | null>(null);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(() => new URLSearchParams(window.location.search).has("review"));
  const [openEvent, setOpenEvent] = useState<EventRecord | null>(null);
  const [openPerson, setOpenPerson] = useState<PersonRecord | null>(null);
  const [openPlace, setOpenPlace] = useState<PlaceRecord | null>(null);
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
    const personId = params.get("person");
    if (personId) {
      const match = bundle.data.people.find((person) => person.id === personId);
      if (match) {
        setOpenPerson(match);
      }
    }
    const placeId = params.get("place");
    if (placeId) {
      const match = bundle.data.places.find((place) => place.id === placeId);
      if (match) {
        setOpenPlace(match);
      }
    }
  }, [bundle]);

  const navigableEvents = useMemo(
    () => splitByRelatedness(bundle?.data.timeline.events ?? []).related,
    [bundle],
  );

  useEffect(() => {
    function onKeyDown(keyEvent: KeyboardEvent) {
      if ((keyEvent.metaKey || keyEvent.ctrlKey) && keyEvent.key.toLowerCase() === "k") {
        keyEvent.preventDefault();
        setSearchOpen((open) => !open);
        return;
      }
      const target = keyEvent.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable)
      ) {
        return;
      }
      if (keyEvent.key === "Escape") {
        if (searchOpen) setSearchOpen(false);
        else if (activeMoment) setActiveMoment(null);
        else if (openEvent) setOpenEvent(null);
        else if (openAlbum) setOpenAlbum(null);
        else if (openPerson) setOpenPerson(null);
        else if (openPlace) setOpenPlace(null);
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
  }, [activeMoment, openEvent, openAlbum, openPerson, reviewOpen, searchOpen, navigableEvents, bundle]);

  const primaryReviewItems = bundle?.data.review_queue ?? [];
  const reviewBacklog = bundle?.data.review_backlog ?? [];
  const reviewItems = useMemo(() => {
    const pool =
      reviewScope === "backlog"
        ? reviewBacklog
        : reviewScope === "all"
          ? [...primaryReviewItems, ...reviewBacklog]
          : primaryReviewItems;
    // Lead with the strongest guesses: quick confirmations build momentum,
    // and the archive learns fastest from decisions it was nearly sure of.
    return [...pool].sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0));
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

  if (!bundle) {
    return (
      <div className="boot">
        <AppIcon size={56} />
        <span>
          {!error && <RefreshCw className="spin" size={14} />}
          {error || "Opening the archive…"}
        </span>
      </div>
    );
  }

  const reviewBadge = primaryReviewItems.length;
  const sections = [...new Set(viewLabels.map((item) => item.section))];

  return (
    <ShellContext.Provider
      value={{ openSearch: () => setSearchOpen(true), openReview: () => setReviewOpen(true), reviewCount: reviewBadge }}
    >
    <div className="photos-shell">
      <aside className="sidebar">
        <header className="sidebar-brand" title={bundle.projectDir}>
          <AppIcon size={30} />
          <div className="brand-copy">
            <strong>TapeSplit</strong>
            <span>{archiveByline(bundle)}</span>
          </div>
        </header>

        <nav className="sidebar-nav" aria-label="Views">
          {sections.map((section) => (
            <div key={section} className="sidebar-section">
              <h2>{section}</h2>
              {viewLabels
                .filter((item) => item.section === section)
                .map((item) => {
                  const Icon = item.icon;
                  return (
                    <button
                      key={item.id}
                      className={`nav-${item.id} ${view === item.id ? "active" : ""}`}
                      aria-current={view === item.id ? "page" : undefined}
                      onClick={() => setView(item.id)}
                    >
                      <Icon size={17} strokeWidth={2} />
                      <span>{item.label}</span>
                    </button>
                  );
                })}
            </div>
          ))}
        </nav>

        <div className="sidebar-footer">
          <button className={`review-entry ${reviewOpen ? "active" : ""}`} onClick={() => setReviewOpen(true)}>
            <Inbox size={17} strokeWidth={2} />
            <span>Review</span>
            {reviewBadge > 0 && <em>{reviewBadge}</em>}
          </button>
          <div className="sidebar-status">
            <button className="sidebar-refresh" onClick={() => void refresh("Reloaded")} disabled={busy} title="Reload the archive">
              <RefreshCw size={13} className={busy ? "spin" : ""} />
            </button>
            <span>
              {busy
                ? "Working…"
                : pendingActions.length
                  ? `${pendingActions.length} ${pendingActions.length === 1 ? "correction" : "corrections"} queued`
                  : "Up to date"}
            </span>
          </div>
        </div>
      </aside>

      <main
        className="stage"
        onScroll={(scrollEvent) => {
          const scrolled = scrollEvent.currentTarget.scrollTop > 2;
          if ((scrollEvent.currentTarget.dataset.scrolled === "true") !== scrolled) {
            scrollEvent.currentTarget.dataset.scrolled = String(scrolled);
          }
        }}
      >
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
            scenes={bundle.data.timeline.scenes}
            media={bundle.data.media}
            albums={bundle.data.tracks.albums}
            summary={bundle.data.summary}
            onOpen={setOpenEvent}
            onOpenAlbum={setOpenAlbum}
          />
        )}
        {view === "people" && (
          <PeopleWall people={bundle.data.people} onSelect={setOpenPerson} onQueue={queueAction} />
        )}
        {view === "albums" && (
          <AlbumsView
            albums={bundle.data.tracks.albums}
            events={bundle.data.timeline.events}
            media={bundle.data.media}
            onPlay={setActiveMoment}
            onOpenAlbum={setOpenAlbum}
          />
        )}
        {view === "journal" && (
          <JournalView
            bundle={bundle}
            onOpenEvent={setOpenEvent}
            onOpenPerson={setOpenPerson}
            onPlay={setActiveMoment}
          />
        )}
        {view === "places" && (
          <PlacesMapView
            places={bundle.data.places}
            events={bundle.data.timeline.events}
            media={bundle.data.media}
            onPlay={(event) => playEvent(event, bundle.data.media, setActiveMoment)}
            openPlace={openPlace}
            onOpenPlace={setOpenPlace}
            helpers={{ placeDisplayLabel, humanizeToken }}
            renderList={() => (
              <PlacesView
                contexts={bundle.data.place_contexts}
                places={bundle.data.places}
                events={bundle.data.timeline.events}
                media={bundle.data.media}
                onPlay={setActiveMoment}
              />
            )}
          />
        )}
        {view === "search" && (
          <>
            <Toolbar title="Search" subtitle="People, places, words and what’s on screen" />
            <section className="search-view">
              <SemanticSearchPanel
                bundle={bundle}
                autoFocus
                onOpenEvent={setOpenEvent}
                onOpenPerson={setOpenPerson}
                onPlay={setActiveMoment}
              />
            </section>
          </>
        )}
      </main>

      {openEvent && (
        <EventSheet
          key={openEvent.id}
          event={openEvent}
          events={bundle.data.timeline.events}
          scenes={bundle.data.timeline.scenes}
          media={bundle.data.media}
          people={bundle.data.people}
          places={bundle.data.places}
          speakerSegments={bundle.data.assets.speaker_segments ?? []}
          placeRoles={bundle.data.assets.place_roles ?? []}
          continuity={bundle.data.assets.event_continuity_contexts ?? []}
          reviewItems={[...primaryReviewItems, ...reviewBacklog]}
          onClose={() => setOpenEvent(null)}
          onOpenEvent={setOpenEvent}
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
          people={bundle.data.people}
          events={bundle.data.timeline.events}
          media={bundle.data.media}
          onQueue={queueAction}
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
          <aside className="review-drawer" role="dialog" aria-label="Review" onClick={(event) => event.stopPropagation()}>
            <header className="drawer-head">
              <div>
                <h2>Review</h2>
                <p>
                  {filteredReviewItems.length === 1 ? "1 guess" : `${filteredReviewItems.length} guesses`} to confirm or correct
                </p>
              </div>
              <button className="sheet-close inline" onClick={() => setReviewOpen(false)} aria-label="Close review">
                <X size={15} />
              </button>
            </header>

            <div className="queue-tools">
              <label className="search-box">
                <Search size={14} />
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter" />
              </label>
              <label className="popup-button" title="Which queue">
                <Inbox size={14} />
                <select value={reviewScope} onChange={(event) => setReviewScope(event.target.value as ReviewScope)}>
                  <option value="primary">Primary ({primaryReviewItems.length})</option>
                  <option value="backlog">Backlog ({reviewBacklog.length})</option>
                  <option value="all">All ({primaryReviewItems.length + reviewBacklog.length})</option>
                </select>
                <ChevronDown size={13} className="popup-chevron" />
              </label>
              <label className="popup-button" title="Kind of guess">
                <ListFilter size={14} />
                <select value={taskFilter} onChange={(event) => setTaskFilter(event.target.value)}>
                  <option value="all">All kinds</option>
                  {Object.entries(taskCounts).map(([task, count]) => (
                    <option key={task} value={task}>
                      {taskLabel(task)} ({count})
                    </option>
                  ))}
                </select>
                <ChevronDown size={13} className="popup-chevron" />
              </label>
              <button className="command-button queue-command" disabled={busy || !suggestedActionCount} onClick={() => void applyBestGuesses()}>
                <CheckCircle2 size={15} />
                <span>Accept Best Guesses</span>
              </button>
            </div>

            <div className="drawer-columns">
              <ReviewQueue
                items={filteredReviewItems}
                selectedId={selectedItem?.id ?? ""}
                pendingActions={pendingActions}
                media={bundle.data.media}
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

      {searchOpen && (
        <div className="search-overlay" onClick={() => setSearchOpen(false)}>
          <div className="search-dialog" role="dialog" aria-label="Search" onClick={(clickEvent) => clickEvent.stopPropagation()}>
            <SemanticSearchPanel
              bundle={bundle}
              autoFocus
              onClose={() => setSearchOpen(false)}
              onOpenEvent={(event) => {
                setSearchOpen(false);
                setOpenEvent(event);
              }}
              onOpenPerson={(person) => {
                setSearchOpen(false);
                setOpenPerson(person);
              }}
              onPlay={(moment) => {
                setSearchOpen(false);
                setActiveMoment(moment);
              }}
            />
          </div>
        </div>
      )}
    </div>
    </ShellContext.Provider>
  );
}

function LibraryView({
  events,
  scenes,
  media,
  albums,
  summary,
  onOpen,
  onOpenAlbum,
}: {
  events: EventRecord[];
  scenes: SceneRecord[];
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
  const footageByTape = useMemo(() => footageRanges(scenes), [scenes]);
  const totalHours = Math.round(media.reduce((acc, tape) => acc + (tape.duration_s ?? 0), 0) / 3600);

  return (
    <section className="library">
      <Toolbar
        title="Library"
        subtitle={`${media.length} tapes · ${totalHours} hours · ${events.length} moments · ${summary.people ?? 0} people`}
      >
        <Segmented
          label="Sort library"
          value={sort}
          onChange={setSort}
          options={[
            { id: "tape", label: "By Tape" },
            { id: "chrono", label: "Chronological" },
            { id: "place", label: "By Place" },
          ]}
        />
      </Toolbar>

      <div className="stage-body">
        {memories.length > 0 && (
          <section className="memories" aria-label="Memories">
            <h2 className="section-title">Memories</h2>
            <div className="memories-row">
              {(() => {
                const usedCovers = new Set<string>();
                return memories.map((album, index) => {
                  const cover = albumCoverPath(album, eventsById, usedCovers);
                  if (cover) {
                    usedCovers.add(cover);
                  }
                  return (
                    <button
                      key={album.id}
                      className="memory-card"
                      style={{ "--tilt": MEMORY_TILTS[index % MEMORY_TILTS.length], "--washi": MEMORY_WASHI[index % MEMORY_WASHI.length] } as CSSProperties}
                      onClick={() => onOpenAlbum(album)}
                    >
                      <span className="memory-washi" aria-hidden="true" />
                      <span className="memory-photo">
                        {cover ? <img src={assetUrl(cover)} alt="" loading="lazy" /> : <span className="card-fallback"><CalendarDays size={26} /></span>}
                        <span className="label-tag memory-tag">{memoryTagLabel(album)}</span>
                      </span>
                      <span className="memory-caption">
                        <strong>{album.title}</strong>
                        <small>{momentCount(album.events?.length ?? 0)}</small>
                      </span>
                    </button>
                  );
                });
              })()}
            </div>
          </section>
        )}

        {groups.map((group) => {
          const tape = sort === "tape" ? media.find((row) => row.id === group.key) : undefined;
          const span = tapeDateSpan(group.events);
          const count = group.events.length === 1 ? "1 moment" : `${group.events.length} moments`;
          return (
            <section key={group.key} className={`library-group sort-${sort}`}>
              <header className="group-head">
                {sort === "place" && <MapPin size={18} className="group-glyph" />}
                <h2>{group.label}</h2>
                <p>
                  {sort === "tape"
                    ? [span, count, group.sublabel].filter(Boolean).join(" · ")
                    : count}
                </p>
                {tape && (
                  <ul className="tape-legend" aria-label="Colors on the tape strip">
                    {KIND_LABELS.filter(([kind]) => group.events.some((event) => eventKind(event) === kind)).map(([kind, label]) => (
                      <li key={kind} className={`kind-${kind}`}>
                        {label}
                      </li>
                    ))}
                  </ul>
                )}
              </header>
              {tape && (
                <TapeStrip tape={tape} events={group.events} footage={footageByTape.get(tape.id) ?? []} onOpen={onOpen} />
              )}
              <div className="event-grid">
                {group.events.map((event) => (
                  <EventCard key={event.id} event={event} media={media} onOpen={onOpen} />
                ))}
              </div>
            </section>
          );
        })}

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
      </div>
    </section>
  );
}

// Content scenes merged into continuous stretches of footage per tape; the
// gaps between them are blank tape, static and blue screen.
function footageRanges(scenes: SceneRecord[]): Map<string, Array<[number, number]>> {
  const byTape = new Map<string, Array<[number, number]>>();
  const sorted = scenes
    .filter((scene) => scene.scene_type === "content" && typeof scene.start_s === "number" && typeof scene.end_s === "number")
    .sort((a, b) => a.source_video_id.localeCompare(b.source_video_id) || (a.start_s ?? 0) - (b.start_s ?? 0));
  for (const scene of sorted) {
    const ranges = byTape.get(scene.source_video_id) ?? byTape.set(scene.source_video_id, []).get(scene.source_video_id)!;
    const last = ranges[ranges.length - 1];
    if (last && (scene.start_s ?? 0) - last[1] < 2) {
      last[1] = Math.max(last[1], scene.end_s ?? 0);
    } else {
      ranges.push([scene.start_s ?? 0, scene.end_s ?? 0]);
    }
  }
  return byTape;
}

// The tape at a glance, iMovie-style: every moment as a colored segment,
// footage outside any moment in gray, blank stretches hatched.
function TapeStrip({
  tape,
  events,
  footage,
  onOpen,
  currentId,
}: {
  tape: MediaRecord;
  events: EventRecord[];
  footage: Array<[number, number]>;
  onOpen: (event: EventRecord) => void;
  currentId?: string;
}) {
  const duration = tape.duration_s ?? 0;
  if (duration <= 0) {
    return null;
  }
  const offset = tape.offset_s ?? 0;
  const pct = (seconds: number) => `${Math.min(100, Math.max(0, (seconds / duration) * 100)).toFixed(3)}%`;
  const current = currentId ? events.find((event) => event.id === currentId) : undefined;
  const currentStart = current ? (current.start_s ?? offset) - offset : 0;
  const currentEnd = current && typeof current.end_s === "number" ? current.end_s - offset : currentStart;
  return (
    <div
      className={current ? "tape-strip has-current" : "tape-strip"}
      role="group"
      aria-label={`${events.length} moments along ${formatTime(duration)} of tape`}
    >
      {footage.map(([start, end]) => (
        <span key={`f${start}`} className="tape-footage" style={{ left: pct(start), width: pct(end - start) }} />
      ))}
      {events.map((event) => {
        const start = (event.start_s ?? offset) - offset;
        const end = typeof event.end_s === "number" ? event.end_s - offset : start;
        return (
          <button
            key={event.id}
            className={`tape-moment kind-${eventKind(event)}${event.id === currentId ? " is-current" : ""}`}
            style={{ left: pct(start), width: `max(3px, ${pct(end - start)})` }}
            title={`${event.title} · ${formatTime(start)}`}
            aria-label={`${event.title}, at ${formatTime(start)}`}
            aria-current={event.id === currentId ? "true" : undefined}
            onClick={() => onOpen(event)}
          />
        );
      })}
      {current && (
        <span
          className="tape-bracket"
          aria-hidden="true"
          style={{ left: `calc(${pct(currentStart)} - 3px)`, width: `calc(max(3px, ${pct(currentEnd - currentStart)}) + 6px)` }}
        />
      )}
    </div>
  );
}

const KIND_LABELS: Array<[string, string]> = [
  ["travel", "Trips"],
  ["home", "Home"],
  ["school", "School"],
  ["celebration", "Celebrations"],
  ["other", "Other"],
];

// Event types collapse into a handful of colors that mean the same thing on
// every tape.
function eventKind(event: EventRecord): string {
  const type = (event.event_type ?? "").toLowerCase();
  if (type === "travel") return "travel";
  if (type === "home" || type === "conversation") return "home";
  if (type === "school") return "school";
  if (["holiday", "birthday", "religious", "cultural", "social"].includes(type)) return "celebration";
  return "other";
}

// "Mar – May 2006", "1998 – 2001": the span a tape's datable moments cover.
function tapeDateSpan(events: EventRecord[]): string {
  const stamps: Array<{ year: number; month: number | null }> = [];
  for (const event of events) {
    for (const date of event.dates ?? []) {
      if (!plausibleEventDate(date)) continue;
      const match = /((?:19|20)\d{2})(?:-(\d{2}))?/.exec(date.date_value ?? "");
      if (match) {
        stamps.push({ year: Number(match[1]), month: match[2] ? Number(match[2]) : null });
      }
    }
  }
  if (!stamps.length) {
    return "";
  }
  const key = (stamp: { year: number; month: number | null }) => stamp.year * 12 + (stamp.month ?? 1) - 1;
  stamps.sort((a, b) => key(a) - key(b));
  const first = stamps[0];
  const last = stamps[stamps.length - 1];
  if (first.year !== last.year) {
    return `${first.year} – ${last.year}`;
  }
  if (first.month && last.month && first.month !== last.month) {
    return `${MONTHS[first.month - 1]} – ${MONTHS[last.month - 1]} ${first.year}`;
  }
  return first.month ? `${MONTHS[first.month - 1]} ${first.year}` : String(first.year);
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
  // The year earns one appearance; a place that repeats it forfeits its copy.
  const placeSansYear = year && place.includes(year) ? place.replace(year, "").replace(/\s*·\s*$/, "").trim() : place;
  const subtitle = [year, placeSansYear].filter(Boolean).join(" · ");
  const length =
    typeof event.start_s === "number" && typeof event.end_s === "number" ? formatTime(event.end_s - event.start_s) : "";
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
      aria-label={[event.title, subtitle].filter(Boolean).join(", ")}
      onKeyDown={(keyEvent) => keyEvent.key === "Enter" && onOpen(event)}
      onMouseEnter={armPreview}
      onMouseLeave={disarmPreview}
    >
      <div className="event-thumb">
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
        {length && <span className="thumb-length">{length}</span>}
        {event.review_status === "needs_review" && <span className="review-dot" title="Has an unconfirmed guess" />}
      </div>
      <figcaption>
        <strong>{event.title}</strong>
        {subtitle && <span>{subtitle}</span>}
      </figcaption>
    </figure>
  );
}

function EventSheet({
  event,
  events,
  scenes,
  media,
  people,
  places,
  speakerSegments,
  placeRoles,
  continuity,
  reviewItems,
  onClose,
  onOpenEvent,
  onPlay,
  onOpenReview,
}: {
  event: EventRecord;
  events: EventRecord[];
  scenes: SceneRecord[];
  media: MediaRecord[];
  people: PersonRecord[];
  places: PlaceRecord[];
  speakerSegments: SpeakerSegment[];
  placeRoles: PlaceRoleAsset[];
  continuity: ContinuityContextAsset[];
  reviewItems: ReviewItem[];
  onClose: () => void;
  onOpenEvent: (event: EventRecord) => void;
  onPlay: (moment: PlayerMoment) => void;
  onOpenReview: (itemId: string) => void;
}) {
  const hero = event.keyframe_path || event.thumbnail_path;
  const strip = useMemo(() => eventFilmstrip(event, scenes, media), [event, scenes, media]);
  const range = useMemo(() => momentRange(event, media), [event, media]);
  const relatedItems = useMemo(
    () =>
      reviewItems.filter(
        (item) => item.related_event_ids?.includes(event.id) || item.events.some((entry) => entry.event_id === event.id),
      ),
    [event.id, reviewItems],
  );
  const summaryText = event.reconciliation?.reconciled_summary || event.summary;
  const filedDate = momentDate(event);
  const recordedDate = event.dates.find((date) => date.date_value && plausibleEventDate(date))?.date_value;
  const when = recordedDate ? formatDateValue(recordedDate) : eventYear(event);
  const tapeMoment = momentFromEvent(event, media);
  const placeLabels = uniqueStrings(event.places.slice(0, 4).map((ref) => placeDisplayLabel(ref.label)));
  const tape = media.find((row) => row.id === event.source_video_ids[0]);
  const tapeEvents = useMemo(
    () => events.filter((entry) => entry.source_video_ids[0] === tape?.id),
    [events, tape?.id],
  );
  const footage = useMemo(() => (tape ? footageRanges(scenes.filter((scene) => scene.source_video_id === tape.id)).get(tape.id) ?? [] : []), [scenes, tape]);
  // People in the moment get bubbles; "Grandma" said to the camera is a mention, not a person in frame.
  const resolvedPeople = event.people.slice(0, 10).map((ref) => ({ ref, person: findPerson(people, ref.id, ref.label) }));
  const present = resolvedPeople.filter(({ person }) => person?.kind !== "role_candidate");
  const mentioned = resolvedPeople.filter(({ person }) => person?.kind === "role_candidate");
  const stamp = stampLabel(filedDate?.precision === "day" ? filedDate.date_value : undefined);
  const playFrom = (videoId: string, startS: number, endS?: number) =>
    onPlay({ videoId, videoLabel: tapeDisplayLabel(videoId, media), startS, endS, title: event.title });

  return (
    <div className="sheet-scrim" onClick={onClose}>
      <article className="event-sheet" role="dialog" aria-label={event.title} onClick={(clickEvent) => clickEvent.stopPropagation()}>
        <button className="sheet-close" onClick={onClose} aria-label="Close">
          <X size={15} />
        </button>
        <div className="sheet-hero">
          {hero ? <img src={assetUrl(hero)} alt="" /> : <div className="card-fallback tall"><Film size={40} /></div>}
          <button className="hero-play" onClick={() => playEvent(event, media, onPlay)} aria-label="Play">
            <Play size={22} fill="currentColor" />
          </button>
          {stamp && (
            <span className="label-tag hero-tag" title="The date this moment is filed under">
              {stamp}
            </span>
          )}
        </div>
        <div className="sheet-body">
          <header>
            {event.event_type && <p className="sheet-kicker">{humanizeToken(event.event_type)}</p>}
            <h1>{event.title}</h1>
            <div className="fact-row">
              {when && (
                <span className="fact when" title="When it was filmed">
                  <CalendarDays size={13} />
                  {when}
                </span>
              )}
              {placeLabels.map((label) => (
                <span key={label} className="fact where" title="Where it was filmed">
                  <MapPin size={13} />
                  {label}
                </span>
              ))}
              {tapeMoment && (
                <span className="fact tape" title="Where it sits on the tape">
                  <Film size={13} />
                  {tapeMoment.videoLabel}
                  <code>{formatTime(tapeMoment.startS)}</code>
                </span>
              )}
            </div>
          </header>

          {(present.length > 0 || mentioned.length > 0) && (
            <div className="sheet-chips" aria-label="Who">
              {present.map(({ ref, person }, index) => {
                const thumb = primaryPersonThumb(person);
                return (
                  <span key={`${ref.id}-${index}`} className="person-bubble">
                    {thumb ? <img src={assetUrl(thumb)} alt="" /> : <i>{personInitials(ref.label)}</i>}
                    {personDisplayName(ref.label)}
                  </span>
                );
              })}
              {mentioned.map(({ ref }, index) => (
                <span key={`m-${ref.id}-${index}`} className="mention-chip" title="Spoken to or about, not seen in this moment">
                  {personDisplayName(ref.label).replace(/\s*\(.*\)\s*$/, "")}
                  <small>mentioned</small>
                </span>
              ))}
            </div>
          )}

          {summaryText && <p className="sheet-summary">{summaryText}</p>}

          {tape && (tape.duration_s ?? 0) > 0 && (
            <section className="sheet-tape" aria-label="Where this moment sits on its tape">
              <div className="sheet-tape-head">
                <span className="label-tag">{tapeDisplayLabel(tape.id, media)}</span>
                <span>
                  {tape.filename} · {tapeMoment ? `${formatTime(tapeMoment.startS)} – ${formatTime(tapeMoment.endS)}` : ""} of{" "}
                  {formatTime(tape.duration_s)}
                </span>
              </div>
              <TapeStrip tape={tape} events={tapeEvents} footage={footage} onOpen={onOpenEvent} currentId={event.id} />
            </section>
          )}

          <section className="evidence" aria-label="How TapeSplit knows">
            <h2 className="evidence-title">How TapeSplit knows</h2>
            <div className="evidence-grid">
              <WhenPane event={event} range={range} />
              <WherePane event={event} places={places} roles={placeRoles} continuity={continuity} />
            </div>
            <VoicesPane range={range} segments={speakerSegments} media={media} onPlayFrom={playFrom} />
          </section>

          {strip.length >= 3 && (
            <section className="sheet-section">
              <h3>Scenes</h3>
              <div className="filmstrip" aria-label="Scenes">
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
            </section>
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

function PeopleWall({
  people,
  onSelect,
  onQueue,
}: {
  people: PersonRecord[];
  onSelect: (person: PersonRecord) => void;
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
  const [dismissedPairs, setDismissedPairs] = useState<Set<string>>(() => loadDismissedPairs());

  function dismissPair(key: string) {
    setDismissedPairs((current) => {
      const next = new Set(current);
      next.add(key);
      window.localStorage.setItem(DISMISSED_PAIRS_KEY, JSON.stringify([...next]));
      return next;
    });
  }

  const duplicatePairs = useMemo(() => {
    const named = people.filter(
      (person) => person.kind !== "role_candidate" && !/^face cluster/i.test(person.label),
    );
    const pairs: { a: PersonRecord; b: PersonRecord; score: number }[] = [];
    for (let i = 0; i < named.length; i += 1) {
      for (let j = i + 1; j < named.length; j += 1) {
        const score = samePersonScore(named[i], named[j]);
        if (score >= 2 && !dismissedPairs.has(pairKey(named[i], named[j]))) {
          pairs.push({ a: named[i], b: named[j], score });
        }
      }
    }
    return pairs.sort((x, y) => y.score - x.score).slice(0, 3);
  }, [dismissedPairs, people]);

  function mergePair(a: PersonRecord, b: PersonRecord) {
    // Merge the thinner record into the richer one.
    const [source, destination] = appearanceCount(a) <= appearanceCount(b) ? [a, b] : [b, a];
    void onQueue({
      id: `merge_${source.id}_${destination.id}`,
      action: "merge_person",
      target_id: source.id,
      target_type: "people_group",
      notes: "Confirmed as the same person from the People wall",
      payload: { merge_with_person_group_id: destination.id },
    });
    dismissPair(pairKey(a, b));
  }

  const { faces, faceless, roles } = useMemo(() => {
    const faces: PersonRecord[] = [];
    const faceless: PersonRecord[] = [];
    const roles: PersonRecord[] = [];
    for (const person of people) {
      if (person.kind === "role_candidate") {
        roles.push(person);
      } else if (primaryPersonThumb(person) || appearanceCount(person) >= 2) {
        // Substantial people stay on the wall even without a confident face —
        // an honest monogram beats both a wrong crop and being hidden away.
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
      <Toolbar
        title="People"
        subtitle={`${faces.length + faceless.length === 1 ? "1 person" : `${faces.length + faceless.length} people`} across the tapes`}
      />
      <div className="stage-body">
        {duplicatePairs.length > 0 && (
          <div className="same-person-row">
            {duplicatePairs.map(({ a, b }) => (
              <div key={pairKey(a, b)} className="same-person-card">
                <div className="same-person-faces">
                  <PersonAvatar person={a} />
                  <PersonAvatar person={b} />
                </div>
                <div className="same-person-text">
                  <strong>Same person?</strong>
                  <small>
                    {personDisplayName(a.label)} · {personDisplayName(b.label)}
                  </small>
                </div>
                <div className="same-person-actions">
                  <button className="command-button small" onClick={() => mergePair(a, b)}>
                    <GitMerge size={13} />
                    <span>Merge</span>
                  </button>
                  <button className="command-button secondary small" onClick={() => dismissPair(pairKey(a, b))}>
                    <span>Not the Same</span>
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="avatar-grid">
          {faces.map((person) => (
            <button key={person.id} className="avatar-cell" onClick={() => onSelect(person)}>
              <PersonAvatar person={person} size="large" />
              <strong>{personDisplayName(person.label)}</strong>
              <span>{momentCount(appearanceCount(person))}</span>
            </button>
          ))}
        </div>
        {faceless.length > 0 && (
          <details className="offcuts">
            <summary>
              <ChevronRight size={15} className="chevron" />
              More people ({faceless.length})
            </summary>
            <div className="avatar-grid compact">
              {faceless.map((person) => (
                <button key={person.id} className="avatar-cell" onClick={() => onSelect(person)}>
                  <PersonAvatar person={person} />
                  <strong>{personDisplayName(person.label)}</strong>
                  <span>{momentCount(appearanceCount(person))}</span>
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
                  <span>{momentCount(appearanceCount(person))}</span>
                </button>
              ))}
            </div>
          </details>
        )}
      </div>
    </section>
  );
}

function PersonAvatar({ person, size }: { person: PersonRecord; size?: "large" }) {
  const thumbs = personFaceThumbs(person);
  const [thumbIndex, setThumbIndex] = useState(0);
  useEffect(() => setThumbIndex(0), [person.id]);
  const thumb = thumbs[thumbIndex] ?? "";
  return (
    <span className={`avatar ${size ?? ""}`}>
      {thumb ? (
        <img
          src={assetUrl(thumb)}
          alt=""
          loading="lazy"
          onError={() => setThumbIndex((index) => index + 1)}
        />
      ) : (
        <i>{personInitials(person.label)}</i>
      )}
    </span>
  );
}

function PersonSheet({
  person,
  people,
  events,
  media,
  onClose,
  onOpenEvent,
  onPlay,
  onQueue,
}: {
  person: PersonRecord;
  people: PersonRecord[];
  events: EventRecord[];
  media: MediaRecord[];
  onClose: () => void;
  onOpenEvent: (event: EventRecord) => void;
  onPlay: (moment: PlayerMoment) => void;
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
  const [merging, setMerging] = useState(false);
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
                {momentCount(appearances.length)}
              </p>
              <div className="merge-affordance">
                {merging ? (
                  <PersonPicker
                    people={people}
                    placeholder="Merge into…"
                    excludeIds={[person.id]}
                    onPick={(destination) => {
                      void onQueue({
                        id: `merge_${person.id}_${destination.id}`,
                        action: "merge_person",
                        target_id: person.id,
                        target_type: "people_group",
                        notes: `Merged from person sheet into ${personDisplayName(destination.label)}`,
                        payload: { merge_with_person_group_id: destination.id },
                      });
                      setMerging(false);
                    }}
                  />
                ) : (
                  <button className="command-button secondary" onClick={() => setMerging(true)}>
                    <GitMerge size={14} />
                    <span>Merge into…</span>
                  </button>
                )}
              </div>
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
    momentCount(album.events?.length ?? 0),
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
        frames.push({ scene, videoLabel: tapeDisplayLabel(videoId, media) });
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

function momentCount(count: number): string {
  return count === 1 ? "1 moment" : `${count} moments`;
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

// Small typeahead over the people wall, for merges and reassignment.
function PersonPicker({
  people,
  placeholder,
  excludeIds = [],
  onPick,
}: {
  people: PersonRecord[];
  placeholder: string;
  excludeIds?: string[];
  onPick: (person: PersonRecord) => void;
}) {
  const [query, setQuery] = useState("");
  const matches = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) {
      return [];
    }
    return people
      .filter((person) => person.kind !== "role_candidate" && !excludeIds.includes(person.id))
      .filter((person) =>
        [person.label, ...(person.aliases ?? [])].some((alias) => alias.toLowerCase().includes(normalized)),
      )
      .slice(0, 6);
  }, [excludeIds, people, query]);
  return (
    <div className="person-picker" onClick={(clickEvent) => clickEvent.stopPropagation()}>
      <input autoFocus value={query} onChange={(changeEvent) => setQuery(changeEvent.target.value)} placeholder={placeholder} />
      {matches.length > 0 && (
        <div className="person-picker-list">
          {matches.map((person) => (
            <button key={person.id} type="button" onClick={() => onPick(person)}>
              <PersonAvatar person={person} />
              <span>{personDisplayName(person.label)}</span>
              <small>{momentCount(appearanceCount(person))}</small>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Common Russian diminutive families: enough to pair the archive's nicknames
// without pretending to be a name database.
const DIMINUTIVE_FAMILIES: string[][] = [
  ["lena", "len", "elena", "лена", "елена"],
  ["filya", "filia", "filip", "filipp", "philip", "phillip", "филип", "филя"],
  ["andryusha", "andrey", "andrei", "andrew", "андрей", "андрюша"],
  ["sasha", "alexander", "alexandra", "aleksandr", "саша"],
  ["misha", "mikhail", "michael", "миша"],
  ["dima", "dmitri", "dmitriy", "dmitry", "дима"],
  ["katya", "ekaterina", "katherine", "катя"],
  ["natasha", "natalia", "natalya", "наташа"],
  ["tanya", "tatiana", "tatyana", "таня"],
  ["grisha", "grigory", "grigori", "гриша"],
];

function personNameTokens(person: PersonRecord): Set<string> {
  const tokens = new Set<string>();
  for (const raw of [person.label, ...(person.aliases ?? [])]) {
    for (const part of raw.split("/")) {
      const token = part.trim().toLowerCase();
      if (token.length >= 3 && !token.includes("(")) {
        tokens.add(token);
      }
    }
  }
  return tokens;
}

function samePersonScore(a: PersonRecord, b: PersonRecord): number {
  const tokensA = personNameTokens(a);
  const tokensB = personNameTokens(b);
  if (!tokensA.size || !tokensB.size) {
    return 0;
  }
  let score = 0;
  for (const token of tokensA) {
    if (tokensB.has(token)) {
      score += 2;
    }
  }
  for (const family of DIMINUTIVE_FAMILIES) {
    const inA = family.some((name) => tokensA.has(name));
    const inB = family.some((name) => tokensB.has(name));
    if (inA && inB) {
      score += 2;
    }
  }
  return score;
}

const DISMISSED_PAIRS_KEY = "tapesplit.samePersonDismissed.v1";

function loadDismissedPairs(): Set<string> {
  try {
    return new Set(JSON.parse(window.localStorage.getItem(DISMISSED_PAIRS_KEY) ?? "[]") as string[]);
  } catch {
    return new Set();
  }
}

function pairKey(a: PersonRecord, b: PersonRecord): string {
  return [a.id, b.id].sort().join("::");
}

const PERSONISH_TASKS = new Set(["resolve_face_cluster", "resolve_speaker", "resolve_person"]);

// The same voices inside free text: rationales and evidence notes.
function humanizeVoiceIds(text: string | undefined, media: MediaRecord[]): string {
  return (text ?? "").replace(/AZ_SPEAKER_\d+@video_\d+|AZ_P\d+_[A-Z]\b/g, (raw) => humanVoiceLabel(raw, media) ?? raw);
}

// Review titles arrive as "Resolve face cluster: Filia / Filip / Филя"; keep
// the prefix, badge the alias soup.
function ReviewItemTitle({ item, media, compact }: { item: ReviewItem; media: MediaRecord[]; compact?: boolean }) {
  const title = (item.title ?? "").replace(/\s->\s/g, " → ");
  const colonAt = title.indexOf(":");
  // In the queue the glyph already names the kind, so "Resolve speaker:" goes.
  const lead = colonAt >= 0 && !compact ? `${title.slice(0, colonAt + 1)} ` : "";
  const subject = colonAt >= 0 ? title.slice(colonAt + 1).trim() : title;
  if (item.task_type === "resolve_speaker") {
    const voice = humanVoiceLabel(subject, media);
    if (voice) {
      return (
        <span title={subject}>
          {lead}
          {voice}
        </span>
      );
    }
  }
  if (String(item.task_type) === "resolve_duplicate_person" && title.includes(" + ")) {
    const [prefix, rest] = title.includes("?") ? [title.slice(0, title.indexOf("?") + 1), title.slice(title.indexOf("?") + 1)] : ["", title];
    const sides = rest.split(" + ").map((side) => side.trim()).filter(Boolean);
    if (sides.length === 2) {
      if (compact) {
        return <span title={title}>{`${prefix} ${personDisplayName(sides[0])} & ${personDisplayName(sides[1])}`.trim()}</span>;
      }
      return (
        <>
          {prefix ? `${prefix} ` : null}
          <PersonNameBadge label={sides[0]} />
          {" & "}
          <PersonNameBadge label={sides[1]} />
        </>
      );
    }
  }
  if (item.task_type === "confirm_relationship" && subject.includes(" / ")) {
    return (
      <>
        {lead}
        <AliasAwareLabel text={subject} />
      </>
    );
  }
  if (!PERSONISH_TASKS.has(item.task_type) || !subject.includes("/")) {
    return (
      <>
        {lead}
        {subject}
      </>
    );
  }
  return (
    <>
      {lead}
      <PersonNameBadge label={subject} />
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
  for (const delimiter of [": ", " be ", " is ", " as ", "· ", "( ", "→ "]) {
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

function TaskGlyph({ task }: { task: string }) {
  const Icon = taskIcons[task] ?? HelpCircle;
  return (
    <span className={`task-glyph ${task}`} aria-hidden="true">
      <Icon size={13} strokeWidth={2.2} />
    </span>
  );
}

function ReviewQueue({
  items,
  selectedId,
  pendingActions,
  media,
  onSelect,
}: {
  items: ReviewItem[];
  selectedId: string;
  pendingActions: ReviewAction[];
  media: MediaRecord[];
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
            aria-current={selectedId === item.id ? "true" : undefined}
            onClick={() => onSelect(item)}
          >
            <TaskGlyph task={item.task_type} />
            <span className="queue-text">
              <strong>
                <ReviewItemTitle item={item} media={media} compact />
              </strong>
              <small title={typeof item.confidence === "number" ? `${Math.round(item.confidence * 100)}% confidence` : undefined}>
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
  const [excludedFaces, setExcludedFaces] = useState<Set<string>>(new Set());
  useEffect(() => {
    setExcludedFaces(new Set());
  }, [item.id]);

  function toggleFace(faceId: string) {
    setExcludedFaces((current) => {
      const next = new Set(current);
      if (next.has(faceId)) {
        next.delete(faceId);
      } else {
        next.add(faceId);
      }
      return next;
    });
  }

  return (
    <section className="review-surface">
      {isFaceItem ? (
        <FaceContextGallery
          clusterId={item.source_id}
          faces={faces}
          visualAssets={visualAssets}
          media={media}
          onPlay={onPlay}
          excluded={excludedFaces}
          onToggle={toggleFace}
          people={people}
          onQueue={onQueue}
        />
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
          <span className={`task-badge ${item.task_type}`}>
            <TaskGlyph task={item.task_type} />
            {taskLabel(item.task_type)}
          </span>
          <span
            className="review-confidence"
            title={typeof item.confidence === "number" ? `${Math.round(item.confidence * 100)}% confidence` : undefined}
          >
            {formatConfidence(item.confidence)}
          </span>
          {pending && <span className="pending-pill">Queued</span>}
        </div>
        <h2>
          <ReviewItemTitle item={item} media={media} />
        </h2>
        <p>{item.prompt}</p>
        <SuggestedResolution item={item} media={media} onQueue={onQueue} />
        <ReviewSubjectPreview item={item} eventsById={eventsById} people={people} places={places} media={media} />
        <EvidenceList item={item} media={media} onPlay={onPlay} />
        <ReviewActionControls item={item} people={people} media={media} onQueue={onQueue} excludedFaceIds={[...excludedFaces]} />
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
  excluded,
  onToggle,
  people = [],
  onQueue,
}: {
  clusterId: string;
  faces: FaceObservation[];
  visualAssets: VisualAsset[];
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
  excluded?: Set<string>;
  onToggle?: (faceId: string) => void;
  people?: PersonRecord[];
  onQueue?: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
  const [menuFor, setMenuFor] = useState("");
  const [reassignFor, setReassignFor] = useState("");
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
  const selectable = Boolean(onToggle && excluded);

  function playFace(asset: VisualAsset | undefined) {
    if (!asset || typeof asset.time_s !== "number") {
      return;
    }
    const tape = media.find((row) => row.id === asset.source_video_id);
    onPlay({
      videoId: asset.source_video_id,
      videoLabel: tape?.filename || asset.source_video_id,
      startS: Math.max(0, (asset.time_s ?? 0) - 2),
      title: "Face context",
    });
  }

  return (
    <div className="face-context-grid">
      {tiles.map(({ face, asset }) => {
        const scene = asset?.keyframe_path || asset?.thumbnail_path;
        const playable = asset && typeof asset.time_s === "number";
        const isExcluded = excluded?.has(face.id) ?? false;
        return (
          <div key={face.id} className={`face-context-tile ${isExcluded ? "excluded" : ""}`}>
            <button
              className="tile-main"
              title={selectable ? (isExcluded ? "Include this face" : "Exclude this face") : playable ? "Play this moment" : undefined}
              onClick={() => (selectable ? onToggle?.(face.id) : playFace(asset))}
            >
              {scene ? <img className="scene-frame" src={assetUrl(scene)} alt="" loading="lazy" /> : <div className="card-fallback" />}
              <img className="face-inset" src={assetUrl(face.face_thumbnail_path ?? "")} alt="" loading="lazy" />
              {isExcluded && (
                <span className="excluded-badge">
                  <X size={12} />
                </span>
              )}
            </button>
            {playable && selectable && (
              <button className="tile-play" title="Play this moment" onClick={() => playFace(asset)}>
                <Play size={12} />
              </button>
            )}
            {onQueue && (
              <button
                className="tile-menu-button"
                title="More"
                onClick={() => {
                  setReassignFor("");
                  setMenuFor(menuFor === face.id ? "" : face.id);
                }}
              >
                <MoreHorizontal size={13} />
              </button>
            )}
            {menuFor === face.id && onQueue && (
              <div className="tile-menu">
                {reassignFor === face.id ? (
                  <PersonPicker
                    people={people}
                    placeholder="Who is this?"
                    onPick={(person) => {
                      void onQueue({
                        id: `reassign_${face.id}`,
                        action: "reassign_face_observations",
                        target_id: clusterId,
                        target_type: "face_cluster",
                        notes: `Reviewer says this face is ${personDisplayName(person.label)}`,
                        payload: {
                          face_observation_ids: [face.id],
                          destination_person_group_id: person.id,
                        },
                      });
                      setMenuFor("");
                      setReassignFor("");
                    }}
                  />
                ) : (
                  <>
                    <button
                      onClick={() => {
                        setReassignFor(face.id);
                      }}
                    >
                      <UserCheck size={13} />
                      <span>This is someone else…</span>
                    </button>
                    <button
                      onClick={() => {
                        void onQueue({
                          id: `unknown_${face.id}`,
                          action: "mark_face_unknown",
                          target_id: clusterId,
                          target_type: "face_cluster",
                          notes: "Reviewer does not recognize this face",
                          payload: { face_observation_ids: [face.id] },
                        });
                        setMenuFor("");
                      }}
                    >
                      <HelpCircle size={13} />
                      <span>Don't know</span>
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function SuggestedResolution({
  item,
  media,
  onQueue,
}: {
  item: ReviewItem;
  media: MediaRecord[];
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
  const suggestion = item.suggested_action;
  if (!suggestion) {
    return null;
  }
  const actions = reviewActionsFromSuggestion(suggestion);
  return (
    <div className="suggested-resolution">
      <div>
        <small>Best Guess</small>
        <strong title={suggestion.label}>
          <AliasAwareLabel text={humanizeVoiceIds(suggestion.label, media)} />
        </strong>
        {suggestion.rationale ? <span>{humanizeVoiceIds(suggestion.rationale, media)}</span> : null}
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
  media,
}: {
  item: ReviewItem;
  eventsById: Map<string, EventRecord>;
  people: PersonRecord[];
  places: PlaceRecord[];
  media: MediaRecord[];
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
    const voice = humanVoiceLabel(candidate.speaker_label, media);
    return (
      <div className="relation-preview">
        <div className="subject-chip voice" title={candidate.speaker_label}>
          <div className="face-stack">
            <AudioLines size={18} />
          </div>
          <div>
            <strong>{voice || candidate.speaker_label || "Local speaker"}</strong>
            <small>voice heard on tape</small>
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
  media,
  onQueue,
  excludedFaceIds = [],
}: {
  item: ReviewItem;
  people?: PersonRecord[];
  media: MediaRecord[];
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
  excludedFaceIds?: string[];
}) {
  if (item.task_type === "resolve_face_cluster") {
    return <FaceClusterActions item={item} people={people} onQueue={onQueue} excludedFaceIds={excludedFaceIds} />;
  }
  if (item.task_type === "resolve_speaker") {
    return <SpeakerIdentityActions item={item} media={media} onQueue={onQueue} />;
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
  excludedFaceIds = [],
}: {
  item: ReviewItem;
  people?: PersonRecord[];
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
  excludedFaceIds?: string[];
}) {
  const candidates = identityCandidates(item);
  const [selectedId, setSelectedId] = useState(candidates[0]?.face_identity_candidate_id ?? "");
  const [customName, setCustomName] = useState("");
  const selected = candidates.find((candidate) => candidate.face_identity_candidate_id === selectedId);

  // Any excluded tiles ride along with the decisive action as one submit.
  function withDetach(action: ReviewAction): ReviewAction | ReviewAction[] {
    if (!excludedFaceIds.length) {
      return action;
    }
    return [
      action,
      {
        id: `detach_${item.source_id}`,
        action: "detach_faces_from_cluster",
        target_id: item.source_id,
        target_type: "face_cluster",
        notes: "Reviewer excluded these faces while resolving the cluster",
        payload: { face_observation_ids: excludedFaceIds },
      },
    ];
  }

  function nameCluster() {
    const label = customName.trim();
    if (!label) return;
    const match = findPerson(people, undefined, label);
    void onQueue(
      withDetach({
        id: `label_${item.source_id}`,
        action: "label_face_cluster",
        target_id: item.source_id,
        target_type: "face_cluster",
        notes: match ? `Matched existing person ${match.label}` : "Named directly by reviewer",
        payload: match ? { label, person_group_id: match.id } : { label },
      }),
    );
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
              <small title={typeof candidate.confidence === "number" ? `${Math.round(candidate.confidence * 100)}% confidence` : undefined}>
                {formatConfidence(candidate.confidence)}
                {candidate.candidate_ambiguity === "high" ? " · crowded scenes" : ""}
                {candidate.direct_name_event_ids?.length ? " · named on tape" : " · seen at the same events"}
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
      {excludedFaceIds.length > 0 && (
        <p className="exclusion-note">
          {excludedFaceIds.length} {excludedFaceIds.length === 1 ? "face" : "faces"} excluded — they leave this
          cluster when you confirm or name it.
        </p>
      )}
      <ActionRow>
        <CommandButton
          icon={UserCheck}
          label="Confirm Identity"
          disabled={!selected}
          onClick={() =>
            selected &&
            onQueue(
              withDetach({
                ...baseAction(item, "confirm_identity"),
                target_id: selected.face_identity_candidate_id,
                payload: {
                  face_cluster_id: item.source_id,
                  person_group_id: selected.person_group_id,
                },
              }),
            )
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

function SpeakerIdentityActions({
  item,
  media,
  onQueue,
}: {
  item: ReviewItem;
  media: MediaRecord[];
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
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
                {candidate.basis?.length ? <small>{humanizeVoiceIds(candidate.basis.slice(0, 2).join(" "), media)}</small> : null}
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
      {item.events.length > 0 && <h3 className="evidence-heading">Seen in</h3>}
      {item.events.map((event, index) => (
        <div key={`${event.event_id}-${index}`} className="evidence-row">
          <Film size={14} className="evidence-glyph" />
          <span className="evidence-title">{event.title}</span>
          <small>{uniqueStrings(event.source_video_ids.map((id) => tapeDisplayLabel(id, media))).join(", ")}</small>
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
      <Play size={10} fill="currentColor" />
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
      <Play size={10} fill="currentColor" />
      <span>{rangeLabel}</span>
    </button>
  );
}

const semanticSectionLabels: Record<SemanticSectionId, string> = {
  people: "People",
  places: "Places",
  moments: "Moments",
  spoken: "Spoken",
  seen: "Seen",
};

function SemanticSearchPanel({
  bundle,
  autoFocus,
  onClose,
  onOpenEvent,
  onOpenPerson,
  onPlay,
}: {
  bundle: ProjectBundle;
  autoFocus?: boolean;
  onClose?: () => void;
  onOpenEvent: (event: EventRecord) => void;
  onOpenPerson: (person: PersonRecord) => void;
  onPlay: (moment: PlayerMoment) => void;
}) {
  const [text, setText] = useState(initialSearchText);
  const [response, setResponse] = useState<SemanticResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const requestRef = useRef(0);

  const media = bundle.data.media;
  const events = bundle.data.timeline.events;
  const people = bundle.data.people;
  const eventsById = useMemo(() => new Map(events.map((event) => [event.id, event])), [events]);
  const peopleById = useMemo(() => new Map(people.map((person) => [person.id, person])), [people]);

  const suggestions = useMemo(() => {
    const needle = text.trim().toLowerCase();
    if (!needle || needle.length < 2) {
      return [];
    }
    const pool: string[] = [];
    for (const person of people) {
      pool.push(personDisplayName(person.label));
    }
    for (const place of bundle.data.places) {
      pool.push(placeDisplayLabel(place.display_label || place.label));
    }
    for (const event of events) {
      const year = eventYear(event);
      if (year) pool.push(year);
    }
    const unique = [...new Set(pool)];
    return unique
      .filter((token) => token && token.toLowerCase() !== needle && token.toLowerCase().includes(needle))
      .slice(0, 6);
  }, [text, people, events, bundle.data.places]);

  useEffect(() => {
    if (autoFocus) {
      inputRef.current?.focus();
    }
  }, [autoFocus]);

  useEffect(() => {
    const trimmed = text.trim();
    if (!trimmed) {
      setResponse(null);
      setNotice("");
      return;
    }
    const requestId = ++requestRef.current;
    setBusy(true);
    const timer = setTimeout(async () => {
      try {
        const result = await semanticSearchProject(trimmed, 8);
        if (requestRef.current !== requestId) return;
        setResponse(result);
        setNotice(result.semantic ? "" : "Semantic index not built yet — showing keyword matches. Run: tapesplit search embed <project>");
      } catch (err) {
        if (requestRef.current !== requestId) return;
        // Sidecar unavailable: fall back to the legacy keyword endpoint.
        try {
          const legacy = await searchProject(trimmed, 16);
          if (requestRef.current !== requestId) return;
          setResponse(legacyToSections(trimmed, legacy.results));
          setNotice("Semantic search unavailable — showing keyword matches.");
        } catch (fallbackErr) {
          if (requestRef.current !== requestId) return;
          setResponse(null);
          setNotice(fallbackErr instanceof Error ? fallbackErr.message : String(fallbackErr));
        }
      } finally {
        if (requestRef.current === requestId) setBusy(false);
      }
    }, 250);
    return () => clearTimeout(timer);
  }, [text]);

  const sections = response?.sections ?? {};
  const hasHits = (Object.keys(sections) as SemanticSectionId[]).some((key) => (sections[key] ?? []).length > 0);
  const searching = busy && !response;
  const emptyResult = !busy && response !== null && !hasHits && text.trim().length > 0;

  return (
    <div className="semantic-search">
      <div className="search-command">
        <Search size={18} />
        <input
          ref={inputRef}
          value={text}
          onChange={(changeEvent) => setText(changeEvent.target.value)}
          onKeyDown={(keyEvent) => {
            if (keyEvent.key === "Escape") {
              keyEvent.stopPropagation();
              if (text) setText("");
              else onClose?.();
            }
          }}
          placeholder="Search people, places, moments, words, or what's on screen"
          aria-label="Semantic search"
        />
        {busy ? <RefreshCw size={15} className="spin" /> : <kbd>⌘K</kbd>}
      </div>

      {suggestions.length > 0 && (
        <div className="suggestion-tokens">
          {suggestions.map((token) => (
            <button key={token} onClick={() => setText(token)}>
              {token}
            </button>
          ))}
        </div>
      )}

      {notice ? (
        <div className="search-notice">
          <Info size={13} />
          <span>{notice}</span>
        </div>
      ) : null}

      {searching && (
        <div className="search-state">
          <RefreshCw size={16} className="spin" />
          <span>Searching the archive…</span>
        </div>
      )}
      {emptyResult && (
        <div className="search-state">
          <Search size={16} />
          <span>
            Nothing matched “{text.trim()}” — try a person, a place, a year, or what’s on screen.
          </span>
        </div>
      )}

      <div className="semantic-sections">
        {(Object.keys(semanticSectionLabels) as SemanticSectionId[]).map((sectionId) => {
          const hits = (sections[sectionId] ?? []).filter((hit) => sectionId !== "seen" || hit.thumbnail_path);
          if (!hits.length) return null;
          return (
            <div key={sectionId} className="section-block">
              <h3>{semanticSectionLabels[sectionId]}</h3>
              {sectionId === "people" && (
                <div className="people-hit-row">
                  {hits.map((hit) => {
                    const person = peopleById.get(hit.source_id);
                    const name = personDisplayName(hit.title || person?.label || "");
                    return (
                      <button key={hit.source_id} className="people-hit" onClick={() => person && onOpenPerson(person)} disabled={!person}>
                        <span className="people-hit-avatar">
                          {person?.thumbnail_path ? <img src={assetUrl(person.thumbnail_path)} alt="" /> : <i>{personInitials(name)}</i>}
                        </span>
                        <span>{name}</span>
                        {person?.canonical_event_ids?.length ? <em>{person.canonical_event_ids.length}</em> : null}
                      </button>
                    );
                  })}
                </div>
              )}
              {sectionId === "places" && (
                <div className="token-row">
                  {hits.map((hit) => (
                    <button key={hit.source_id} className="place-hit" onClick={() => setText(placeDisplayLabel(hit.title))}>
                      <MapPin size={13} />
                      <span>{placeDisplayLabel(hit.title)}</span>
                    </button>
                  ))}
                </div>
              )}
              {sectionId === "moments" && (
                <div className="moment-hit-grid">
                  {hits
                    // A moment the library can't open isn't a result — graph
                    // rows and stale ids stay out of the grid entirely.
                    .filter((hit) => eventsById.has(hit.source_id))
                    .map((hit) => {
                      const event = eventsById.get(hit.source_id)!;
                      const thumb = event.thumbnail_path;
                      return (
                        <button key={`${hit.record_type}:${hit.source_id}`} className="moment-hit" onClick={() => onOpenEvent(event)}>
                          <span className="moment-hit-thumb">{thumb ? <img src={assetUrl(thumb)} alt="" loading="lazy" /> : <ImageIcon size={18} />}</span>
                          <span className="moment-hit-copy">
                            <strong>{hit.title || event.title}</strong>
                            {cleanSnippet(hit.snippet) ? <small>{cleanSnippet(hit.snippet)}</small> : null}
                          </span>
                        </button>
                      );
                    })}
                </div>
              )}
              {sectionId === "spoken" && (
                <div className="spoken-hits">
                  {hits.map((hit) => (
                    <SpokenHitRow key={hit.source_id} hit={hit} media={media} onPlay={onPlay} />
                  ))}
                </div>
              )}
              {sectionId === "seen" && (
                <div className="seen-grid">
                  {hits.map((hit) => (
                    <SeenHitTile key={`${hit.record_type}:${hit.source_id}`} hit={hit} media={media} onPlay={onPlay} />
                  ))}
                </div>
              )}
            </div>
          );
        })}
        {text.trim() && !busy && !hasHits ? <EmptyState icon={Search} title="No matches in the archive" /> : null}
      </div>
    </div>
  );
}

function SpokenHitRow({ hit, media, onPlay }: { hit: SemanticHit; media: MediaRecord[]; onPlay: (moment: PlayerMoment) => void }) {
  const moment = momentFromHit(hit, media);
  const text = hit.snippet || hit.title;
  const language = lineLanguage(text);
  return (
    <button className="spoken-hit" onClick={() => moment && onPlay(moment)} disabled={!moment}>
      <span className="spoken-quote" lang={language || undefined}>
        {language && <span className={`lang-pill ${language}`}>{language.toUpperCase()}</span>}
        {text}
      </span>
      {moment ? (
        <small>
          <Play size={9} fill="currentColor" />
          {moment.videoLabel} · {formatTime(moment.startS)}
        </small>
      ) : null}
    </button>
  );
}

function SeenHitTile({ hit, media, onPlay }: { hit: SemanticHit; media: MediaRecord[]; onPlay: (moment: PlayerMoment) => void }) {
  const moment = momentFromHit(hit, media);
  if (!hit.thumbnail_path) {
    return null;
  }
  return (
    <button className="seen-tile" onClick={() => moment && onPlay(moment)} disabled={!moment} title={moment ? `${moment.videoLabel} · ${formatTime(moment.startS)}` : ""}>
      <img src={assetUrl(hit.thumbnail_path)} alt="" loading="lazy" />
      {moment ? <small>{formatTime(moment.startS)}</small> : null}
    </button>
  );
}

function momentFromHit(hit: SemanticHit, media: MediaRecord[]): PlayerMoment | null {
  if (!hit.source_video_id) {
    return null;
  }
  const anchor = typeof hit.time_s === "number" ? hit.time_s : typeof hit.start_s === "number" ? hit.start_s : null;
  if (anchor === null) {
    return null;
  }
  return {
    videoId: hit.source_video_id,
    videoLabel: tapeDisplayLabel(hit.source_video_id, media),
    startS: Math.max(0, anchor - 2),
    endS: typeof hit.end_s === "number" ? hit.end_s : undefined,
    title: hit.title || hit.snippet || "Search match",
  };
}

function legacyToSections(query: string, results: SearchResult[]): SemanticResponse {
  const spoken: SemanticHit[] = [];
  const moments: SemanticHit[] = [];
  for (const result of results) {
    const hit: SemanticHit = {
      kind: "document",
      score: result.score ?? 0,
      record_type: result.record_type,
      source_id: result.source_id,
      source_video_id: result.source_video_id ?? null,
      start_s: result.start_s ?? null,
      end_s: result.end_s ?? null,
      title: result.title,
      snippet: result.snippet ?? "",
    };
    if (result.record_type === "transcript") spoken.push(hit);
    else if (result.record_type === "event" || result.record_type === "album") moments.push(hit);
  }
  return { query, semantic: false, sections: { spoken, moments } };
}

function AlbumsView({
  albums,
  events,
  media,
  onPlay,
  onOpenAlbum,
}: {
  albums: AlbumRecord[];
  events: EventRecord[];
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
  onOpenAlbum: (album: AlbumRecord) => void;
}) {
  const eventsById = useMemo(() => new Map(events.map((event) => [event.id, event])), [events]);
  const visibleAlbums = dedupeAlbumsForDisplay(albums.filter((album) => album.events?.length || album.thumbnail_path));
  return (
    <section className="albums-page">
      <Toolbar title="Albums" subtitle={visibleAlbums.length === 1 ? "1 album" : `${visibleAlbums.length} albums, gathered from the tapes`} />
    <div className="stage-body albums-view">
      {visibleAlbums.map((album) => {
        const cover = albumCoverPath(album, eventsById) || album.thumbnail_path;
        // A bare-date title with the same date underneath says it twice.
        const subtitleParts = [album.date_label, placeDisplayLabel(album.place_label)].filter(Boolean) as string[];
        const subtitle =
          subtitleParts.filter((part) => !album.title.toLowerCase().includes(part.toLowerCase())).join(" · ") ||
          (subtitleParts.length ? "" : humanizeToken(album.album_type ?? "album"));
        const tiles = (album.events ?? []).slice(0, 8);
        const showTiles = tiles.length > 1;
        const names = uniqueStrings((album.people_labels ?? []).map((label) => personDisplayName(label))).slice(0, 5);
        return (
          <article key={album.id} className="album-row">
            <button className="album-cover" onClick={() => onOpenAlbum(album)} aria-label={`Open ${album.title}`}>
              {cover ? <img src={assetUrl(cover)} alt="" /> : <AlbumCoverFallback album={album} eventsById={eventsById} />}
            </button>
            <div className="album-body">
              <div className="row-heading">
                <div className="event-title-stack">
                  <h2>
                    <button className="album-title" onClick={() => onOpenAlbum(album)}>
                      {album.title}
                    </button>
                  </h2>
                  {subtitle && <small>{subtitle}</small>}
                </div>
                <span>{momentCount(album.events?.length ?? 0)}</span>
              </div>
              <div className="token-row">
                <Token>{humanizeToken(album.album_type ?? "album")}</Token>
                {names.map((name) => (
                  <Token key={name}>{name}</Token>
                ))}
              </div>
              {showTiles && (
                <div className="album-events">
                  {tiles.map((event) => {
                    const fullEvent = eventsById.get(event.event_id);
                    return (
                      <button key={`${album.id}-${event.event_id}`} className="album-event-tile" onClick={() => playEvent(event, media, onPlay)}>
                        {fullEvent?.thumbnail_path ? <img src={assetUrl(fullEvent.thumbnail_path)} alt="" /> : <ImageIcon size={18} />}
                        <span>{event.title}</span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </article>
        );
      })}
      {!visibleAlbums.length ? <EmptyState icon={CalendarDays} title="No albums" /> : null}
    </div>
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

// A memory's cover should show its people, not the scenery the camera warmed
// up on — rank candidate frames by how many people their event carries.
function albumCoverPath(album: AlbumRecord, eventsById: Map<string, EventRecord>, avoid?: Set<string>): string {
  const ranked = (album.events ?? [])
    .map((entry) => eventsById.get(entry.event_id))
    .filter((event): event is EventRecord => Boolean(event?.thumbnail_path))
    .sort(
      (a, b) =>
        (b.people?.length ?? 0) - (a.people?.length ?? 0) ||
        ((b.end_s ?? 0) - (b.start_s ?? 0)) - ((a.end_s ?? 0) - (a.start_s ?? 0)),
    );
  const candidates = [
    ...ranked.map((event) => event.thumbnail_path ?? ""),
    album.thumbnail_path ?? "",
  ].filter(Boolean);
  return candidates.find((path) => !avoid?.has(path)) ?? candidates[0] ?? "";
}

// Day-albums carry every constituent date ("MAY 10 2002, MAY 11 2002, ...");
// a Memory reads as a span. Dates themselves may contain commas ("March 22,
// 2006"), so split only at commas that start a new month token.
const MONTH_BOUNDARY = /,\s*(?=(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d)/i;

// Memories hang like snapshots on a board: a little tilt, a strip of tape.
const MEMORY_TILTS = ["-1.4deg", "1deg", "-0.7deg", "1.3deg", "-1.1deg", "0.8deg"];
const MEMORY_WASHI = ["var(--washi-1)", "var(--washi-2)", "var(--washi-3)", "var(--washi-4)"];

// The memory's span the way a camcorder stamp prints it: "MAR 22–25 2006".
function memoryTagLabel(album: AlbumRecord): string {
  const parts = (album.date_label ?? "").split(MONTH_BOUNDARY).map((part) => part.trim()).filter(Boolean);
  const parse = (text: string) => {
    const match = /(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+((?:19|20)\d{2})/i.exec(text);
    if (!match) return null;
    const month = MONTHS.findIndex((name) => name.toLowerCase() === match[1].toLowerCase());
    return { year: Number(match[3]), month, day: Number(match[2]) };
  };
  const dates = parts.map(parse).filter((date): date is { year: number; month: number; day: number } => Boolean(date));
  if (!dates.length) {
    return memoryDateLabel(album).toUpperCase();
  }
  dates.sort((a, b) => a.year - b.year || a.month - b.month || a.day - b.day);
  const first = dates[0];
  const last = dates[dates.length - 1];
  const mon = (date: { month: number }) => MONTHS[date.month].toUpperCase();
  if (first.year === last.year && first.month === last.month) {
    return first.day === last.day ? `${mon(first)} ${first.day} ${first.year}` : `${mon(first)} ${first.day}–${last.day} ${first.year}`;
  }
  if (first.year === last.year) {
    return `${mon(first)} ${first.day} – ${mon(last)} ${last.day} ${first.year}`;
  }
  return `${mon(first)} ${first.year} – ${mon(last)} ${last.year}`;
}

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
    videoLabel: tapeDisplayLabel(videoId, media),
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
    videoLabel: tapeDisplayLabel(videoId, media),
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
  if (person.avatar_candidates) {
    // New exports carry ranked, attribution-gated crops. An empty list means no
    // crop met the bar — the monogram is the honest render, so no legacy fallback.
    return uniqueStrings(person.avatar_candidates.map((candidate) => candidate.path || ""));
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

// Percentages read like exam grades; the queue speaks in plain terms and
// keeps the exact number a hover away.
function formatConfidence(value?: number) {
  if (typeof value !== "number") {
    return "needs a look";
  }
  if (value >= 0.75) return "strong guess";
  if (value >= 0.45) return "possible match";
  return "needs a look";
}

function formatScore(value?: number) {
  return typeof value === "number" ? `score ${value.toFixed(2)}` : "score n/a";
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

// Index snippets carry pipeline vocabulary (record types, video ids); a
// search result speaks the user's language or stays quiet.
function cleanSnippet(snippet?: string): string {
  if (!snippet) {
    return "";
  }
  return snippet
    .replace(/\b[a-z]+(?:_[a-z0-9]+)+\b/g, " ")
    .replace(/\bvideo_\d+\b/gi, " ")
    .replace(/\s{2,}/g, " ")
    .replace(/^\W+|\W+$/g, "")
    .trim();
}

// The sidebar introduces the archive, not its directory; the path lives in
// the tooltip for anyone who needs it.
function archiveByline(bundle: ProjectBundle): string {
  const media = bundle.data.media ?? [];
  const seconds = media.reduce((sum, row) => sum + (row.duration_s ?? 0), 0);
  const hours = Math.round(seconds / 3600);
  if (!media.length) {
    return shortPath(bundle.projectDir);
  }
  return `${media.length} tapes · ${hours} hours`;
}

function JournalView({
  bundle,
  onOpenEvent,
  onOpenPerson,
  onPlay,
}: {
  bundle: ProjectBundle;
  onOpenEvent: (event: EventRecord) => void;
  onOpenPerson: (person: PersonRecord) => void;
  onPlay: (moment: PlayerMoment) => void;
}) {
  const [posts, setPosts] = useState<JournalPost[] | null>(null);
  const [error, setError] = useState("");
  const [openPostId, setOpenPostId] = useState<string>(
    () => new URLSearchParams(window.location.search).get("post") ?? "",
  );

  useEffect(() => {
    loadJournalPosts()
      .then(setPosts)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  const eventsById = useMemo(() => {
    const map = new Map<string, EventRecord>();
    for (const event of bundle.data.timeline.events) {
      map.set(event.id, event);
    }
    return map;
  }, [bundle]);

  if (error || posts === null || !posts.length) {
    return (
      <section className="journal-page">
        <Toolbar title="Journal" subtitle="Days from the tapes, written down" />
        <div className="stage-body journal-view">
          <p className="empty-note">
            {error ? (
              error
            ) : posts === null ? (
              "Opening the journal…"
            ) : (
              <>
                No entries yet — run <code>tapesplit journal generate</code> to draft the first posts from the tapes.
              </>
            )}
          </p>
        </div>
      </section>
    );
  }

  const openPost = posts.find((post) => post.id === openPostId) ?? null;
  if (openPost) {
    return (
      <JournalPostArticle
        post={openPost}
        bundle={bundle}
        eventsById={eventsById}
        onBack={() => setOpenPostId("")}
        onOpenEvent={onOpenEvent}
        onOpenPerson={onOpenPerson}
        onPlay={onPlay}
      />
    );
  }

  return (
    <section className="journal-page">
      <Toolbar
        title="Journal"
        subtitle={`${posts.length === 1 ? "1 entry" : `${posts.length} entries`} · every line traceable to a moment`}
      />
      <div className="stage-body journal-view">
        <div className="journal-feed">
          {posts.map((post) => {
            const hero = post.hero_event_id ? eventsById.get(post.hero_event_id) : undefined;
            const cover = hero?.thumbnail_path || hero?.keyframe_path;
            return (
              <article
                key={post.id}
                className="journal-card"
                role="button"
                tabIndex={0}
                onClick={() => setOpenPostId(post.id)}
                onKeyDown={(keyEvent) => keyEvent.key === "Enter" && setOpenPostId(post.id)}
              >
                {cover ? <img src={assetUrl(cover)} alt="" loading="lazy" /> : <div className="journal-card-blank" />}
                <div className="journal-card-body">
                  <span className="journal-kicker">{post.kicker}</span>
                  <h2 className="journal-display">{post.title}</h2>
                  <p className="journal-card-dek">{post.dek}</p>
                  <span className="journal-byline">
                    {post.date_label}
                    {post.read_minutes ? ` · ${post.read_minutes} min read` : ""}
                  </span>
                </div>
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function JournalPostArticle({
  post,
  bundle,
  eventsById,
  onBack,
  onOpenEvent,
  onOpenPerson,
  onPlay,
}: {
  post: JournalPost;
  bundle: ProjectBundle;
  eventsById: Map<string, EventRecord>;
  onBack: () => void;
  onOpenEvent: (event: EventRecord) => void;
  onOpenPerson: (person: PersonRecord) => void;
  onPlay: (moment: PlayerMoment) => void;
}) {
  const hero = post.hero_event_id ? eventsById.get(post.hero_event_id) : undefined;
  const heroCover = hero?.keyframe_path || hero?.thumbnail_path;
  const media = bundle.data.media;

  function renderEntities(text: string, entities: JournalEntity[] | undefined): ReactNode {
    if (!entities?.length) {
      return text;
    }
    let nodes: Array<string | ReactElement> = [text];
    for (const entity of entities) {
      if (!entity.span_text) continue;
      nodes = nodes.flatMap((node) => {
        if (typeof node !== "string" || !node.includes(entity.span_text)) {
          return [node];
        }
        const [before, ...rest] = node.split(entity.span_text);
        const after = rest.join(entity.span_text);
        return [before, <EntityLink key={`${entity.id}-${before.length}`} entity={entity} />, after];
      });
    }
    return nodes;
  }

  function EntityLink({ entity }: { entity: JournalEntity }) {
    if (entity.kind === "person") {
      const person = bundle.data.people.find((row) => row.id === entity.id);
      if (person) {
        return (
          <button className="entity-link" title="Open person" onClick={() => onOpenPerson(person)}>
            {entity.span_text}
          </button>
        );
      }
    }
    if (entity.kind === "event") {
      const event = eventsById.get(entity.id);
      if (event) {
        return (
          <button className="entity-link" title="Open moment" onClick={() => onOpenEvent(event)}>
            {entity.span_text}
          </button>
        );
      }
    }
    return <span className="entity-mention" title={entity.kind}>{entity.span_text}</span>;
  }

  return (
    <section className="journal-page reading">
      <Toolbar title={post.title} subtitle={post.date_label} onBack={onBack} backLabel="Journal" />
      <div className="stage-body">
        <article className="journal-article">
          <header>
            <span className="journal-kicker">{post.kicker}</span>
            <h1 className="journal-display journal-title">{post.title}</h1>
            {post.dek ? <p className="journal-dek">{post.dek}</p> : null}
            <p className="journal-byline">
              {post.date_label}
              {post.read_minutes ? ` · ${post.read_minutes} min read` : ""}
              {post.generated ? " · drafted from the tapes" : ""}
            </p>
          </header>
          {heroCover ? (
            <figure className="journal-hero">
              <img src={assetUrl(heroCover)} alt="" />
            </figure>
          ) : null}
          <div className="journal-body">
            {post.blocks.map((block, index) => (
              <JournalBlockView key={index} block={block} />
            ))}
          </div>
        </article>
      </div>
    </section>
  );

  function JournalBlockView({ block }: { block: JournalBlock }) {
    if (block.type === "heading") {
      return <h3 className="journal-display journal-heading">{block.text}</h3>;
    }
    if (block.type === "paragraph") {
      return (
        <p className="journal-paragraph">
          {renderEntities(block.text ?? "", block.entities)}
          {block.citations?.length ? (
            <span
              className="journal-footnote"
              title={`Grounded in ${block.citations.length === 1 ? "1 source" : `${block.citations.length} sources`} from the tapes`}
              aria-label="This paragraph is grounded in the tapes"
            >
              {Array.from({ length: Math.min(block.citations.length, 3) }).map((_, dot) => (
                <i key={dot} />
              ))}
            </span>
          ) : null}
        </p>
      );
    }
    if (block.type === "pullquote") {
      const tape = media.find((row) => row.id === block.source_video_id);
      const canPlay = Boolean(block.source_video_id && typeof block.start_s === "number");
      return (
        <blockquote className="journal-pullquote">
          <p className="journal-display">«{block.text}»</p>
          {block.translation ? <p className="journal-translation">{block.translation}</p> : null}
          <footer>
            {block.speaker ? <span className="journal-speaker">{block.speaker}</span> : null}
            {canPlay ? (
              <button
                className="journal-play"
                title="Play this moment"
                onClick={() =>
                  onPlay({
                    videoId: block.source_video_id!,
                    videoLabel: tape?.filename || block.source_video_id!,
                    startS: Math.max(0, (block.start_s ?? 0) - 1),
                    endS: typeof block.end_s === "number" ? block.end_s + 1 : undefined,
                    title: block.text ?? "Quote",
                  })
                }
              >
                <Play size={12} /> Play
              </button>
            ) : null}
          </footer>
        </blockquote>
      );
    }
    if (block.type === "clip") {
      const event = block.event_id ? eventsById.get(block.event_id) : undefined;
      if (!event) return null;
      const cover = event.thumbnail_path || event.keyframe_path;
      const moment = momentFromEvent(event, media);
      return (
        <figure className="journal-clip">
          {cover ? (
            <button className="journal-clip-frame" title="Open moment" onClick={() => onOpenEvent(event)}>
              <img src={assetUrl(cover)} alt="" loading="lazy" />
            </button>
          ) : null}
          <figcaption>
            <span>{block.text || event.title}</span>
            {moment ? (
              <button className="journal-play" title="Play clip" onClick={() => onPlay(moment)}>
                <Play size={12} /> Play
              </button>
            ) : null}
          </figcaption>
        </figure>
      );
    }
    return null;
  }
}
