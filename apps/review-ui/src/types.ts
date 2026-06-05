export type TaskType =
  | "resolve_face_cluster"
  | "confirm_place_context"
  | "confirm_relationship"
  | "resolve_place"
  | "resolve_person"
  | "review_event"
  | "resolve_date";

export interface ProjectBundle {
  projectDir: string;
  visualizationPath: string;
  pendingActionsPath: string;
  pendingActions: ReviewAction[];
  data: VisualizationData;
}

export interface VisualizationData {
  generated_at: string;
  project: string;
  summary: Record<string, number>;
  media: MediaRecord[];
  timeline: {
    events: EventRecord[];
    scenes: SceneRecord[];
  };
  tracks: {
    people: PersonRecord[];
    places: PlaceRecord[];
    albums: AlbumRecord[];
  };
  places: PlaceRecord[];
  people: PersonRecord[];
  place_contexts: PlaceContext[];
  relationships: {
    nodes: GraphNode[];
    edges: GraphEdge[];
    candidates: RelationshipCandidate[];
    place_context_edges: PlaceContextEdge[];
    face_identity_candidates: FaceIdentityCandidate[];
  };
  review_queue: ReviewItem[];
  assets: {
    visual: VisualAsset[];
    faces: FaceObservation[];
    face_clusters: FaceCluster[];
    face_identity_candidates: FaceIdentityCandidate[];
  };
}

export interface MediaRecord {
  id: string;
  filename: string;
  relative_path: string;
  duration_s?: number;
  width?: number;
  height?: number;
}

export interface EventRecord {
  id: string;
  type: "event";
  title: string;
  summary: string;
  start_s?: number;
  end_s?: number;
  source_video_ids: string[];
  event_type?: string;
  relatedness?: string;
  confidence?: number;
  review_status: string;
  people: EntityRef[];
  places: EntityRef[];
  dates: DateRef[];
  thumbnail_path?: string;
  keyframe_path?: string;
  evidence_ids: string[];
}

export interface SceneRecord {
  id: string;
  source_video_id: string;
  start_s?: number;
  end_s?: number;
  label?: string;
  scene_type?: string;
  thumbnail_path?: string;
}

export interface EntityRef {
  id: string;
  type: string;
  label: string;
}

export interface DateRef extends EntityRef {
  date_value?: string;
  precision?: string;
}

export interface PersonRecord {
  id: string;
  label: string;
  aliases: string[];
  kind?: string;
  confidence?: number;
  review_status: string;
  appearance_count?: number;
  appearances?: EventEntry[];
  thumbnail_path?: string;
  candidate_face_clusters?: Array<{
    face_cluster_id: string;
    thumbnail_path?: string;
    face_count?: number;
    confidence?: number;
    review_status?: string;
  }>;
}

export interface PlaceRecord {
  id: string;
  label: string;
  display_label: string;
  kind?: string;
  place_type?: string;
  scope_label?: string;
  confidence?: number;
  review_status: string;
  appearance_count?: number;
  appearances?: EventEntry[];
  parent_place_labels?: string[];
  nearby_place_labels?: string[];
  coordinates?: { lat: number; lng: number } | null;
  context?: {
    id: string;
    key: string;
    label: string;
    basis: string;
  };
}

export interface PlaceContext {
  id: string;
  key: string;
  label: string;
  basis: string;
  place_ids: string[];
  places: Array<{
    id: string;
    label: string;
    display_label: string;
    kind?: string;
    place_type?: string;
    appearance_count?: number;
    review_status: string;
    not_exportable_as_gps?: boolean;
  }>;
  events: EventEntry[];
  event_count: number;
  place_count: number;
  source_video_ids: string[];
  date_years: string[];
}

export interface AlbumRecord {
  id: string;
  title: string;
  album_type?: string;
  date_label?: string;
  place_label?: string;
  people_labels?: string[];
  events?: EventEntry[];
  thumbnail_path?: string;
  review_status?: string;
}

export interface EventEntry {
  event_id: string;
  title: string;
  start_s?: number;
  end_s?: number;
  source_video_ids: string[];
}

export interface GraphNode {
  id: string;
  type: string;
  label: string;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  predicate: string;
  label: string;
  weight?: number;
  confidence?: number;
  review_status: string;
}

export interface RelationshipCandidate {
  id: string;
  subject_label?: string;
  predicate?: string;
  object_label?: string;
  confidence?: number;
  review_status: string;
}

export interface PlaceContextEdge {
  id: string;
  source: string;
  target: string;
  source_label: string;
  target_label: string;
  predicate: string;
  label: string;
  confidence?: number;
  review_status: string;
  not_exportable_as_gps?: boolean;
}

export interface ReviewItem {
  id: string;
  task_type: TaskType;
  source_record_type: string;
  source_id: string;
  title: string;
  prompt: string;
  priority: number;
  confidence?: number;
  review_status: string;
  related_event_ids?: string[];
  events: EventEntry[];
  thumbnail_path?: string;
  candidate: Record<string, unknown>;
  actions: string[];
}

export interface FaceIdentityCandidate {
  id: string;
  face_cluster_id: string;
  person_group_id: string;
  person_label: string;
  confidence?: number;
  review_status: string;
  supporting_event_ids?: string[];
  supporting_event_titles?: string[];
}

export interface FaceCluster {
  id: string;
  label?: string;
  face_count: number;
  thumbnail_path?: string;
  candidate_people?: Array<{
    person_group_id: string;
    person_label: string;
    confidence?: number;
  }>;
  review_status: string;
}

export interface FaceObservation {
  id: string;
  face_cluster_id?: string;
  person_group_id?: string;
  face_thumbnail_path?: string;
  source_subject_id?: string;
}

export interface VisualAsset {
  id: string;
  subject_type: string;
  subject_id: string;
  source_video_id: string;
  thumbnail_path?: string;
  keyframe_path?: string;
  time_s?: number;
  label?: string;
}

export interface ReviewAction {
  id?: string;
  action: string;
  target_id: string;
  target_type?: string;
  reviewer?: string;
  reviewed_at?: string;
  notes?: string;
  payload?: Record<string, unknown>;
  [key: string]: unknown;
}
