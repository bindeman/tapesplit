import {
  AlertTriangle,
  CalendarDays,
  Check,
  CheckCircle2,
  Clock3,
  GitMerge,
  ImageIcon,
  Info,
  Inbox,
  ListFilter,
  MapPin,
  MapPinned,
  Pencil,
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
import { applyReviewActions, assetUrl, loadProject, queueReviewAction, removeReviewAction, videoUrl } from "./api";
import type {
  AlbumRecord,
  EventEntry,
  EventRecord,
  MediaRecord,
  PersonRecord,
  PlaceContext,
  PlaceLocationOption,
  PlaceRecord,
  ProjectBundle,
  ReviewAction,
  ReviewItem,
  TaskType,
} from "./types";

type ViewMode = "review" | "timeline" | "places" | "people";

type PlayerMoment = {
  videoId: string;
  videoLabel: string;
  startS: number;
  endS?: number;
  title: string;
};

const taskLabels: Record<string, string> = {
  resolve_face_cluster: "Faces",
  confirm_place_context: "Place Links",
  confirm_relationship: "Relationships",
  resolve_place: "Places",
  resolve_person: "People",
  review_event: "Events",
  resolve_date: "Dates",
};

const viewLabels: Array<{ id: ViewMode; label: string; icon: typeof Inbox }> = [
  { id: "review", label: "Review", icon: Inbox },
  { id: "timeline", label: "Timeline", icon: Clock3 },
  { id: "places", label: "Places", icon: MapPinned },
  { id: "people", label: "People", icon: Users },
];

export function App() {
  const [bundle, setBundle] = useState<ProjectBundle | null>(null);
  const [view, setView] = useState<ViewMode>("review");
  const [taskFilter, setTaskFilter] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string>("");
  const [activeMoment, setActiveMoment] = useState<PlayerMoment | null>(null);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

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

  const reviewItems = bundle?.data.review_queue ?? [];
  const reviewBacklog = bundle?.data.review_backlog ?? [];
  const pendingActions = bundle?.pendingActions ?? [];
  const taskCounts = useMemo(() => countBy(reviewItems, (item) => item.task_type), [reviewItems]);

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
      setStatus("Corrections applied");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
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

  return (
    <div className="app-shell">
      <aside className="left-rail">
        <header className="brand">
          <div>
            <strong>TapeSplit</strong>
            <span>{shortPath(bundle.projectDir)}</span>
          </div>
          <button className="icon-button" onClick={() => void refresh("Reloaded")} disabled={busy} title="Reload project">
            <RefreshCw size={16} className={busy ? "spin" : ""} />
          </button>
        </header>

        <nav className="mode-tabs" aria-label="Views">
          {viewLabels.map((item) => {
            const Icon = item.icon;
            return (
              <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}>
                <Icon size={16} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <ProjectStats summary={bundle.data.summary} backlogCount={reviewBacklog.length} />

        <div className="queue-tools">
          <label className="search-box">
            <Search size={15} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter review items" />
          </label>
          <div className="task-filter">
            <ListFilter size={15} />
            <select value={taskFilter} onChange={(event) => setTaskFilter(event.target.value)}>
              <option value="all">All review items</option>
              {Object.entries(taskCounts).map(([task, count]) => (
                <option key={task} value={task}>
                  {taskLabel(task)} ({count})
                </option>
              ))}
            </select>
          </div>
        </div>

        <ReviewQueue
          items={filteredReviewItems}
          selectedId={selectedItem?.id ?? ""}
          pendingActions={pendingActions}
          onSelect={(item) => {
            setView("review");
            setSelectedId(item.id);
          }}
        />
      </aside>

      <main className="workbench">
        <div className="workbench-top">
          <div>
            <h1>{viewTitle(view, selectedItem)}</h1>
            <p>{viewSubtitle(view, bundle)}</p>
          </div>
          <div className="status-strip">
            {error && (
              <span className="status error">
                <AlertTriangle size={14} />
                {error}
              </span>
            )}
            {status && !error && <span className="status">{status}</span>}
          </div>
        </div>

        {view === "review" && selectedItem && (
          <ReviewDetail
            key={selectedItem.id}
            item={selectedItem}
            pending={itemHasPendingAction(selectedItem, pendingActions)}
            events={bundle.data.timeline.events}
            media={bundle.data.media}
            people={bundle.data.people}
            places={bundle.data.places}
            onPlay={setActiveMoment}
            onQueue={queueAction}
          />
        )}
        {view === "review" && !selectedItem && <EmptyState icon={Inbox} title="No review items" />}
        {view === "timeline" && <TimelineView events={bundle.data.timeline.events} media={bundle.data.media} onPlay={setActiveMoment} />}
        {view === "places" && (
          <PlacesView
            contexts={bundle.data.place_contexts}
            places={bundle.data.places}
            events={bundle.data.timeline.events}
            media={bundle.data.media}
            onPlay={setActiveMoment}
          />
        )}
        {view === "people" && <PeopleView people={bundle.data.people} media={bundle.data.media} onPlay={setActiveMoment} />}
      </main>

      <aside className="right-rail">
        <VideoPlayerPanel moment={activeMoment} onClear={() => setActiveMoment(null)} />
        <PendingActionsPanel actions={pendingActions} busy={busy} onApply={applyActions} onRemove={removeAction} />
        <ContextPanel item={selectedItem} events={bundle.data.timeline.events} media={bundle.data.media} onPlay={setActiveMoment} />
      </aside>
    </div>
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
              <strong>{item.title}</strong>
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
  onPlay,
  onQueue,
}: {
  item: ReviewItem;
  pending: boolean;
  events: EventRecord[];
  media: MediaRecord[];
  people: PersonRecord[];
  places: PlaceRecord[];
  onPlay: (moment: PlayerMoment) => void;
  onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void>;
}) {
  const eventsById = useMemo(() => new Map(events.map((event) => [event.id, event])), [events]);
  return (
    <section className="review-surface">
      <div className="review-media">
        {item.thumbnail_path ? (
          <img src={assetUrl(item.thumbnail_path)} alt="" />
        ) : (
          <div className="media-placeholder">
            <ImageIcon size={28} />
          </div>
        )}
      </div>
      <div className="review-body">
        <div className="review-heading">
          <span className={`task-badge ${item.task_type}`}>{taskLabel(item.task_type)}</span>
          <span className="review-confidence">{formatConfidence(item.confidence)}</span>
          {pending && <span className="pending-pill">Queued</span>}
        </div>
        <h2>{item.title}</h2>
        <p>{item.prompt}</p>
        <ReviewSubjectPreview item={item} eventsById={eventsById} people={people} places={places} />
        <EvidenceList item={item} media={media} onPlay={onPlay} />
        <ReviewActionControls item={item} onQueue={onQueue} />
      </div>
    </section>
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
        <strong>{person?.label || label}</strong>
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

function ReviewActionControls({ item, onQueue }: { item: ReviewItem; onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void> }) {
  if (item.task_type === "resolve_face_cluster") {
    return <FaceClusterActions item={item} onQueue={onQueue} />;
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
    return (
      <ActionRow>
        <CommandButton icon={Check} label="Confirm" onClick={() => onQueue(baseAction(item, "confirm_relationship"))} />
        <CommandButton icon={X} label="Reject" tone="danger" onClick={() => onQueue(baseAction(item, "reject_relationship"))} />
      </ActionRow>
    );
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

function FaceClusterActions({ item, onQueue }: { item: ReviewItem; onQueue: (action: ReviewAction | ReviewAction[]) => Promise<void> }) {
  const candidates = identityCandidates(item);
  const [selectedId, setSelectedId] = useState(candidates[0]?.face_identity_candidate_id ?? "");
  const selected = candidates.find((candidate) => candidate.face_identity_candidate_id === selectedId);
  return (
    <div className="action-block">
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
              <strong>{candidate.person_label}</strong>
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
  const candidate = item.candidate as { label?: string };
  const [label, setLabel] = useState(candidate.label ?? item.title.replace("Resolve person: ", ""));
  return (
    <div className="action-block">
      <label className="single-field">
        <span>Name</span>
        <input value={label} onChange={(event) => setLabel(event.target.value)} />
      </label>
      <ActionRow>
        <CommandButton icon={Check} label="Confirm" onClick={() => onQueue({ ...baseAction(item, "confirm_person"), label })} />
        <CommandButton icon={Pencil} label="Rename" onClick={() => onQueue({ ...baseAction(item, "rename_person"), label })} />
        <CommandButton icon={GitMerge} label="Role Only" onClick={() => onQueue(baseAction(item, "mark_role_only"))} />
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
  onRemove,
}: {
  actions: ReviewAction[];
  busy: boolean;
  onApply: () => Promise<void>;
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
  onPlay,
}: {
  events: EventRecord[];
  media: MediaRecord[];
  onPlay: (moment: PlayerMoment) => void;
}) {
  return (
    <section className="timeline-view">
      {events.map((event) => (
        <article key={event.id} className="timeline-row">
          <div className="timeline-thumb">{event.thumbnail_path ? <img src={assetUrl(event.thumbnail_path)} alt="" /> : <ImageIcon size={22} />}</div>
          <div className="timeline-copy">
            <div className="row-heading">
              <h2>{event.title}</h2>
              <MomentButton event={event} media={media} onPlay={onPlay} />
            </div>
            <p>{event.summary}</p>
            <div className="token-row">
              <Token>{event.event_type ?? "event"}</Token>
              <Token>{event.review_status}</Token>
              {event.people.slice(0, 4).map((person) => <Token key={person.id}>{person.label}</Token>)}
              {event.places.slice(0, 3).map((place) => <Token key={place.id}>{place.label}</Token>)}
            </div>
          </div>
        </article>
      ))}
    </section>
  );
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
              <h2>{context.label}</h2>
              <span>{context.place_count} places</span>
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
                  <span>{place.display_label}</span>
                  <small>{place.review_status}</small>
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
            <span>{place.display_label}</span>
            <small>{place.kind} · {place.place_type} · {place.review_status}</small>
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
  return actions.some((action) => targetIds.has(String(action.target_id)));
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

function viewTitle(view: ViewMode, selected: ReviewItem | null) {
  if (view === "review") return selected?.title ?? "Review";
  if (view === "timeline") return "Event Timeline";
  if (view === "places") return "Place Contexts";
  return "People";
}

function viewSubtitle(view: ViewMode, bundle: ProjectBundle) {
  const summary = bundle.data.summary;
  if (view === "review") {
    const backlog = summary.review_backlog_items ?? bundle.data.review_backlog?.length ?? 0;
    return `${summary.review_items ?? 0} primary review items · ${backlog} in backlog · ${bundle.pendingActions.length} pending`;
  }
  if (view === "timeline") return `${summary.events ?? 0} visible events across ${summary.source_videos ?? 0} videos`;
  if (view === "places") return `${summary.place_contexts ?? 0} contexts · ${summary.places ?? 0} places`;
  return `${summary.people ?? 0} people · ${summary.face_clusters ?? 0} face clusters`;
}

function formatConfidence(value?: number) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "n/a";
}

function formatTime(value?: number) {
  if (typeof value !== "number") return "00:00";
  const minutes = Math.floor(value / 60);
  const seconds = Math.floor(value % 60);
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function shortPath(path: string) {
  const parts = path.split("/");
  return parts.slice(-2).join("/");
}
