import type { JournalPost, ProjectBundle, ReviewAction, SearchResponse, SemanticResponse, StampFrame } from "./types";

export async function loadProject(): Promise<ProjectBundle> {
  const response = await fetch("/api/project");
  return readResponse(response);
}

export async function queueReviewAction(action: ReviewAction | ReviewAction[]): Promise<ProjectBundle> {
  const response = await fetch("/api/actions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(action),
  });
  return readResponse(response);
}

export async function removeReviewAction(id?: string): Promise<ProjectBundle> {
  const response = await fetch(`/api/actions${id ? `?id=${encodeURIComponent(id)}` : ""}`, {
    method: "DELETE",
  });
  return readResponse(response);
}

export async function applyReviewActions(): Promise<ProjectBundle> {
  const response = await fetch("/api/apply", { method: "POST" });
  return readResponse(response);
}

export async function applySuggestedReviewActions(tier = "primary"): Promise<ProjectBundle> {
  const params = new URLSearchParams({ tier });
  const response = await fetch(`/api/apply-suggestions?${params.toString()}`, { method: "POST" });
  return readResponse(response);
}

export async function reapplyReviewCorrections(): Promise<ProjectBundle> {
  const response = await fetch("/api/reapply", { method: "POST" });
  return readResponse(response);
}

export async function searchProject(query: string, limit = 12): Promise<SearchResponse> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const response = await fetch(`/api/search?${params.toString()}`);
  return readResponse(response);
}

export async function semanticSearchProject(query: string, limit = 8): Promise<SemanticResponse> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const response = await fetch(`/api/search/semantic?${params.toString()}`);
  return readResponse(response);
}

export function assetUrl(path?: string) {
  return path ? `/api/asset?path=${encodeURIComponent(path)}` : "";
}

export function videoUrl(videoId?: string) {
  return videoId ? `/api/video?id=${encodeURIComponent(videoId)}` : "";
}

async function readResponse<T>(response: Response): Promise<T> {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload?.error || `Request failed with ${response.status}`);
  }
  return payload as T;
}

// One fetch of the Journal per page load: the Journal view and the moment
// sheet's translated quotes share it.
let journalRequest: Promise<JournalPost[]> | null = null;

export function loadJournalPosts(): Promise<JournalPost[]> {
  if (!journalRequest) {
    journalRequest = fetch("/api/journal")
      .then((response) => readResponse<{ posts: JournalPost[] }>(response))
      .then((payload) => payload.posts ?? [])
      .catch((error) => {
        journalRequest = null;
        throw error;
      });
  }
  return journalRequest;
}

// Camcorder date stamps read in a stretch of tape, with their boxes.
export async function loadStamps(videoId: string, startS: number, endS: number): Promise<StampFrame[]> {
  const params = new URLSearchParams({ video: videoId, start: String(Math.max(0, startS)), end: String(Math.max(startS, endS)) });
  const response = await fetch(`/api/stamps?${params.toString()}`);
  const payload = await readResponse<{ frames: StampFrame[] }>(response);
  return payload.frames ?? [];
}
