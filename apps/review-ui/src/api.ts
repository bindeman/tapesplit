import type { ProjectBundle, ReviewAction, SearchResponse } from "./types";

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

export async function reapplyReviewCorrections(): Promise<ProjectBundle> {
  const response = await fetch("/api/reapply", { method: "POST" });
  return readResponse(response);
}

export async function searchProject(query: string, limit = 12): Promise<SearchResponse> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const response = await fetch(`/api/search?${params.toString()}`);
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
