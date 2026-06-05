import type { ProjectBundle, ReviewAction } from "./types";

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

export function assetUrl(path?: string) {
  return path ? `/api/asset?path=${encodeURIComponent(path)}` : "";
}

async function readResponse<T>(response: Response): Promise<T> {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload?.error || `Request failed with ${response.status}`);
  }
  return payload as T;
}
