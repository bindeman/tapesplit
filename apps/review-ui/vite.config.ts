import react from "@vitejs/plugin-react";
import { spawn } from "node:child_process";
import { gzipSync } from "node:zlib";
import { createReadStream, existsSync, readdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { appendFile, mkdir, readFile, rm } from "node:fs/promises";
import { basename, dirname, extname, join, resolve, sep } from "node:path";
import type { IncomingMessage, ServerResponse } from "node:http";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const appRoot = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(appRoot, "../..");
const projectDir = resolve(process.env.TAPESPLIT_PROJECT || discoverDefaultProject());
const pendingActionsPath = join(projectDir, "review-actions.pending.jsonl");
const appliedActionsPath = join(projectDir, "review-actions.applied.jsonl");
const tapesplitBin = existsSync(join(repoRoot, ".venv/bin/tapesplit"))
  ? join(repoRoot, ".venv/bin/tapesplit")
  : "tapesplit";
const searchEmbeddingBackend = process.env.TAPESPLIT_SEARCH_EMBEDDING_BACKEND || "local-sparse";
const searchEmbeddingModel = process.env.TAPESPLIT_SEARCH_EMBEDDING_MODEL || "";

// Without TAPESPLIT_PROJECT, open the first *.tapesplit project under examples/.
function discoverDefaultProject(): string {
  const examplesDir = resolve(repoRoot, "examples");
  if (existsSync(examplesDir)) {
    const projects = readdirSync(examplesDir).filter((name) => name.endsWith(".tapesplit")).sort();
    if (projects.length) return join(examplesDir, projects[0]);
  }
  return resolve(examplesDir, "my-tapes.tapesplit");
}

function reviewApiPlugin(): Plugin {
  return {
    name: "tapesplit-review-api",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (!req.url?.startsWith("/api/")) {
          next();
          return;
        }
        if (!isTrustedLocalRequest(req)) {
          sendJson(res, { error: "forbidden" }, 403);
          return;
        }
        try {
          if (req.method === "GET" && req.url.startsWith("/api/project")) {
            await sendProjectJson(req, res, await loadProject());
            return;
          }
          if (req.method === "GET" && req.url.startsWith("/api/asset")) {
            await sendAsset(req, res);
            return;
          }
          if (req.method === "GET" && req.url.startsWith("/api/video")) {
            await sendVideo(req, res);
            return;
          }
          if (req.method === "GET" && req.url.startsWith("/api/journal")) {
            await sendJson(res, await loadJournalPosts());
            return;
          }
          if (req.method === "GET" && req.url.startsWith("/api/stamps")) {
            sendStamps(req, res);
            return;
          }
          if (req.method === "GET" && req.url.startsWith("/api/search/semantic")) {
            await sendSemanticSearch(req, res);
            return;
          }
          if (req.method === "GET" && req.url.startsWith("/api/search")) {
            await sendSearch(req, res);
            return;
          }
          if (req.method === "POST" && req.url.startsWith("/api/actions")) {
            const body = await readJson(req);
            const actions = Array.isArray(body) ? body : [body];
            await appendPendingActions(actions);
            await sendProjectJson(req, res, await loadProject());
            return;
          }
          if (req.method === "DELETE" && req.url.startsWith("/api/actions")) {
            const url = new URL(req.url, "http://localhost");
            const actionId = url.searchParams.get("id");
            await removePendingAction(actionId);
            await sendProjectJson(req, res, await loadProject());
            return;
          }
          if (req.method === "POST" && req.url.startsWith("/api/apply-suggestions")) {
            await applySuggestedActions(req);
            await sendProjectJson(req, res, await loadProject());
            return;
          }
          if (req.method === "POST" && req.url.startsWith("/api/apply")) {
            await applyPendingActions();
            await sendProjectJson(req, res, await loadProject());
            return;
          }
          if (req.method === "POST" && req.url.startsWith("/api/reapply")) {
            await reapplyCorrections();
            await sendProjectJson(req, res, await loadProject());
            return;
          }
          sendJson(res, { error: "not_found" }, 404);
        } catch (error) {
          sendJson(res, { error: error instanceof Error ? error.message : String(error) }, 500);
        }
      });
    },
  };
}

async function loadJournalPosts() {
  const journalPath = join(projectDir, "journal_posts.jsonl");
  return { projectDir, posts: readJsonlIfExists(journalPath) };
}

// --- Camcorder date stamps --------------------------------------------------
// The moment sheet shows the frame a date came from, with the boxes Apple
// Vision drew around the stamp. Only stamp-shaped text ("SEP", "7 2005",
// "FEB 17 2006", "11:57:19AM") ever leaves this function, so OCR of signs,
// name tags or class lists never reaches the UI through it.

type StampRow = { video: string; time: number; text: string; image: string; bbox: { x: number; y: number; width: number; height: number } };

// Camcorder OSD text only: a month with digits, a bare date or year, or a
// clock. OCR noise around a stamp ("FEB 192006*") is tolerated at the end.
const STAMP_TEXT =
  /^(?:(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\.?(?:\s*\d{1,2})?(?:[\s,]*\d{4,5})?|\d{1,2}[\s,]+\d{4}|\d{4,5}|\d{1,2}:\d{2}(?::\d{2})?\s?(?:AM|PM)?)\W?$/i;
const STAMP_DATE = /(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\.?\s*(\d{1,2})[\s,]*((?:19|20)\d{2})/i;
const MONTH_INDEX = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
const VIDEO_ID = /^[A-Za-z0-9_-]{1,64}$/;
const ISO_DAY = /^(?:19|20)\d{2}-\d{2}-\d{2}$/;
let stampCache: { mtimeMs: number; rows: StampRow[] } | null = null;

type StampFrame = { image: string; time_s: number; date?: string; boxes: Array<{ text: string } & StampRow["bbox"]> };

// "SEP" + "7 2005" on one frame → 2005-09-07. Boxes are read left to right
// within a line, the way the camcorder printed them.
function frameDate(frame: StampFrame): string | undefined {
  const text = [...frame.boxes]
    .sort((a, b) => (Math.abs(a.y - b.y) > 12 ? a.y - b.y : a.x - b.x))
    .map((box) => box.text)
    .join(" ");
  const match = STAMP_DATE.exec(text);
  if (!match) {
    return undefined;
  }
  const month = MONTH_INDEX.indexOf(match[1].toUpperCase()) + 1;
  const day = Number(match[2]);
  if (day < 1 || day > 31) {
    return undefined;
  }
  return `${match[3]}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function loadStampRows(): StampRow[] {
  const path = join(projectDir, "visual_text_observations.jsonl");
  if (!existsSync(path)) {
    return [];
  }
  const { mtimeMs } = statSync(path);
  if (stampCache && stampCache.mtimeMs === mtimeMs) {
    return stampCache.rows;
  }
  const rows: StampRow[] = [];
  for (const row of readJsonlIfExists(path)) {
    const text = typeof row.text === "string" ? row.text.trim() : "";
    const bbox = row.bbox as StampRow["bbox"] | undefined;
    const image = typeof row.source_image_path === "string" ? row.source_image_path : "";
    const video = typeof row.source_video_id === "string" ? row.source_video_id : "";
    const time = typeof row.time_s === "number" ? row.time_s : NaN;
    if (!text || !STAMP_TEXT.test(text) || !bbox || !image || !video || !Number.isFinite(time)) {
      continue;
    }
    // Scene keyframes only: their ids are stable, so the boxes line up with
    // the picture the UI loads.
    if (!image.startsWith("keyframes/scenes/") || image.split(/[\\/]/).some((part) => part === ".." || part === "")) {
      continue;
    }
    rows.push({ video, time, text, image, bbox: { x: bbox.x, y: bbox.y, width: bbox.width, height: bbox.height } });
  }
  stampCache = { mtimeMs, rows };
  return rows;
}

// GET /api/stamps?video=&start=&end=[&date=YYYY-MM-DD]
// Stamp frames inside a stretch of tape. With `date`, the frames anywhere on
// that tape whose stamp reads that day come first: a moment dated by a stamp
// is often confirmed by one printed a few seconds before it starts.
function sendStamps(req: IncomingMessage, res: ServerResponse) {
  const url = new URL(req.url || "", "http://localhost");
  const video = url.searchParams.get("video") || "";
  const start = Number(url.searchParams.get("start"));
  const end = Number(url.searchParams.get("end"));
  const date = url.searchParams.get("date") || "";
  if (
    !VIDEO_ID.test(video) ||
    !Number.isFinite(start) ||
    !Number.isFinite(end) ||
    start < 0 ||
    end < start ||
    end - start > 6 * 3600 ||
    (date && !ISO_DAY.test(date))
  ) {
    sendJson(res, { error: "bad_request" }, 400);
    return;
  }
  const byImage = new Map<string, StampFrame>();
  for (const row of loadStampRows()) {
    if (row.video !== video) {
      continue;
    }
    const frame = byImage.get(row.image) ?? { image: row.image, time_s: row.time, boxes: [] };
    if (frame.boxes.length < 8) {
      frame.boxes.push({ text: row.text, ...row.bbox });
    }
    byImage.set(row.image, frame);
  }
  // A frame counts as a stamp only if it shows a month or a clock time; bare
  // numbers on their own are altimeters, scoreboards and house numbers.
  const anchored = /^(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)|\d:\d{2}(?::\d{2}|\s?(?:AM|PM))/i;
  const stamped = [...byImage.values()]
    .filter((frame) => frame.boxes.some((box) => anchored.test(box.text)))
    .map((frame) => ({ ...frame, date: frameDate(frame) }));
  const inRange = (frame: StampFrame) => frame.time_s >= start - 1 && frame.time_s <= end + 1;
  const distance = (frame: StampFrame) => (inRange(frame) ? 0 : Math.min(Math.abs(frame.time_s - start), Math.abs(frame.time_s - end)));
  const matching = date ? stamped.filter((frame) => frame.date === date).sort((a, b) => distance(a) - distance(b)) : [];
  const nearby = stamped.filter((frame) => inRange(frame) && !matching.includes(frame)).sort((a, b) => a.time_s - b.time_s);
  sendJson(res, { video, frames: [...matching, ...nearby].slice(0, 4) });
}

async function loadProject() {
  const visualizationPath = join(projectDir, "visualization.json");
  if (!existsSync(visualizationPath)) {
    throw new Error(`Missing visualization.json in ${projectDir}`);
  }
  const data = JSON.parse(await readFile(visualizationPath, "utf8"));
  return {
    projectDir,
    visualizationPath,
    pendingActionsPath,
    pendingActions: readJsonlIfExists(pendingActionsPath),
    data,
  };
}

async function sendSearch(req: IncomingMessage, res: ServerResponse) {
  const url = new URL(req.url || "", "http://localhost");
  const query = (url.searchParams.get("q") || "").trim();
  const rawLimit = Number(url.searchParams.get("limit") || 12);
  const limit = Number.isFinite(rawLimit) ? Math.min(Math.max(rawLimit, 1), 50) : 12;
  if (!query) {
    await sendJson(res, { project: projectDir, query: "", results: [] });
    return;
  }
  if (!existsSync(join(projectDir, "search.sqlite"))) {
    await run(tapesplitBin, ["search", "build", projectDir], repoRoot);
  }
  await sendJson(res, await runJson(tapesplitBin, ["search", "query", projectDir, query, "--limit", String(limit)], repoRoot));
}

// --- Warm semantic-search sidecar -----------------------------------------
// `tapesplit search serve` keeps the embedding models loaded; we hold one
// child process, speak JSON-lines over stdio, and respawn it on death.

type SidecarPending = { resolve: (value: unknown) => void; reject: (error: Error) => void; timer: NodeJS.Timeout };

let sidecarChild: ReturnType<typeof spawn> | null = null;
let sidecarBuffer = "";
let sidecarRequestId = 0;
const sidecarPending = new Map<number, SidecarPending>();

function ensureSidecar() {
  if (sidecarChild) {
    return sidecarChild;
  }
  const child = spawn(tapesplitBin, ["search", "serve", projectDir], { cwd: repoRoot, stdio: "pipe" });
  sidecarBuffer = "";
  child.stdout.on("data", (chunk) => {
    sidecarBuffer += chunk.toString();
    let newline = sidecarBuffer.indexOf("\n");
    while (newline >= 0) {
      const line = sidecarBuffer.slice(0, newline).trim();
      sidecarBuffer = sidecarBuffer.slice(newline + 1);
      newline = sidecarBuffer.indexOf("\n");
      if (!line) continue;
      try {
        const payload = JSON.parse(line) as { id?: number };
        if (typeof payload.id === "number" && sidecarPending.has(payload.id)) {
          const pending = sidecarPending.get(payload.id)!;
          sidecarPending.delete(payload.id);
          clearTimeout(pending.timer);
          pending.resolve(payload);
        }
      } catch {
        // ready banner or noise; ignore
      }
    }
  });
  child.on("exit", () => {
    if (sidecarChild === child) {
      sidecarChild = null;
    }
    for (const [, pending] of sidecarPending) {
      clearTimeout(pending.timer);
      pending.reject(new Error("semantic search sidecar exited"));
    }
    sidecarPending.clear();
  });
  child.on("error", () => {
    if (sidecarChild === child) {
      sidecarChild = null;
    }
  });
  sidecarChild = child;
  return child;
}

function sidecarQuery(query: string, limit: number): Promise<unknown> {
  const child = ensureSidecar();
  const id = ++sidecarRequestId;
  return new Promise((resolveQuery, rejectQuery) => {
    const timer = setTimeout(() => {
      sidecarPending.delete(id);
      rejectQuery(new Error("semantic search timed out (models may still be loading)"));
    }, 30000);
    sidecarPending.set(id, { resolve: resolveQuery, reject: rejectQuery, timer });
    child.stdin?.write(JSON.stringify({ id, op: "query", q: query, limit }) + "\n");
  });
}

async function sendSemanticSearch(req: IncomingMessage, res: ServerResponse) {
  const url = new URL(req.url || "", "http://localhost");
  const query = (url.searchParams.get("q") || "").trim();
  const rawLimit = Number(url.searchParams.get("limit") || 8);
  const limit = Number.isFinite(rawLimit) ? Math.min(Math.max(rawLimit, 1), 24) : 8;
  if (!query) {
    await sendJson(res, { query: "", semantic: false, sections: {} });
    return;
  }
  try {
    const payload = (await sidecarQuery(query, limit)) as { ok?: boolean; result?: unknown; error?: string };
    if (payload.ok && payload.result) {
      await sendJson(res, payload.result);
      return;
    }
    await sendJson(res, { error: payload.error || "semantic search failed" }, 500);
  } catch (error) {
    await sendJson(res, { error: error instanceof Error ? error.message : String(error) }, 500);
  }
}

async function sendAsset(req: IncomingMessage, res: ServerResponse) {
  const url = new URL(req.url || "", "http://localhost");
  const rel = url.searchParams.get("path") || "";
  const safeRel = rel.split(/[\\/]/).filter((part) => part && part !== "." && part !== "..").join(sep);
  const assetPath = resolve(projectDir, safeRel);
  if (!assetPath.startsWith(projectDir + sep) || !existsSync(assetPath) || !statSync(assetPath).isFile()) {
    sendJson(res, { error: "asset_not_found" }, 404);
    return;
  }
  const type = mimeType(extname(assetPath));
  res.writeHead(200, { "Content-Type": type, "Cache-Control": "no-store" });
  createReadStream(assetPath).pipe(res);
}

async function sendVideo(req: IncomingMessage, res: ServerResponse) {
  const url = new URL(req.url || "", "http://localhost");
  const videoId = url.searchParams.get("id") || "";
  const tape = readJsonlIfExists(join(projectDir, "tapes.jsonl")).find((row) => row.id === videoId);
  if (!tape) {
    sendJson(res, { error: "video_not_found" }, 404);
    return;
  }
  const videoPath = resolveVideoPath(tape);
  if (!videoPath || !existsSync(videoPath) || !statSync(videoPath).isFile()) {
    sendJson(res, { error: "video_file_not_found" }, 404);
    return;
  }

  const stat = statSync(videoPath);
  const range = req.headers.range;
  const contentType = mimeType(extname(videoPath));
  if (!range) {
    res.writeHead(200, {
      "Content-Type": contentType,
      "Content-Length": stat.size,
      "Accept-Ranges": "bytes",
      "Cache-Control": "no-store",
    });
    createReadStream(videoPath).pipe(res);
    return;
  }

  const match = /^bytes=(\d*)-(\d*)$/.exec(range);
  if (!match) {
    res.writeHead(416, { "Content-Range": `bytes */${stat.size}` });
    res.end();
    return;
  }
  const start = match[1] ? Number(match[1]) : 0;
  const end = match[2] ? Number(match[2]) : stat.size - 1;
  if (Number.isNaN(start) || Number.isNaN(end) || start > end || start >= stat.size) {
    res.writeHead(416, { "Content-Range": `bytes */${stat.size}` });
    res.end();
    return;
  }
  const safeEnd = Math.min(end, stat.size - 1);
  res.writeHead(206, {
    "Content-Type": contentType,
    "Content-Length": safeEnd - start + 1,
    "Content-Range": `bytes ${start}-${safeEnd}/${stat.size}`,
    "Accept-Ranges": "bytes",
    "Cache-Control": "no-store",
  });
  createReadStream(videoPath, { start, end: safeEnd }).pipe(res);
}

async function appendPendingActions(actions: unknown[]) {
  await mkdir(projectDir, { recursive: true });
  const now = new Date().toISOString();
  const rows = actions.map((action, index) => {
    if (!action || typeof action !== "object") {
      throw new Error("review action must be an object");
    }
    const row = action as Record<string, unknown>;
    return {
      id: row.id || `ui_action_${Date.now()}_${index}`,
      reviewer: row.reviewer || "review-ui",
      reviewed_at: row.reviewed_at || now,
      ...row,
    };
  });
  await appendFile(pendingActionsPath, rows.map((row) => JSON.stringify(row)).join("\n") + "\n", "utf8");
}

async function removePendingAction(actionId: string | null) {
  if (!existsSync(pendingActionsPath)) {
    return;
  }
  if (!actionId) {
    await rm(pendingActionsPath, { force: true });
    return;
  }
  const remaining = readJsonlIfExists(pendingActionsPath).filter((row) => row.id !== actionId);
  writeFileSync(pendingActionsPath, remaining.map((row) => JSON.stringify(row)).join("\n") + (remaining.length ? "\n" : ""), "utf8");
}

async function applyPendingActions() {
  const pending = readJsonlIfExists(pendingActionsPath);
  if (!pending.length) {
    await refreshDerivedOutputs();
    return;
  }
  await run(tapesplitBin, ["review", "apply", projectDir, pendingActionsPath], repoRoot);
  await refreshDerivedOutputs();
  await appendFile(appliedActionsPath, pending.map((row) => JSON.stringify(row)).join("\n") + "\n", "utf8");
  const archivePath = join(projectDir, `review-actions.applied.${new Date().toISOString().replace(/[:.]/g, "-")}.jsonl`);
  renameSync(pendingActionsPath, archivePath);
}

async function applySuggestedActions(req: IncomingMessage) {
  const url = new URL(req.url || "", "http://localhost");
  const tier = url.searchParams.get("tier") || "primary";
  await run(tapesplitBin, ["review", "apply-suggestions", projectDir, "--tier", tier, "--reviewer", "review-ui-bulk"], repoRoot);
  await refreshDerivedOutputs();
}

async function reapplyCorrections() {
  await run(tapesplitBin, ["review", "reapply", projectDir], repoRoot);
  await refreshDerivedOutputs();
}

async function refreshDerivedOutputs() {
  await run(tapesplitBin, ["build-evidence", projectDir], repoRoot);
  await run(tapesplitBin, ["search", "build", projectDir, ...searchBuildArgs()], repoRoot);
  await run(tapesplitBin, ["export-visualization", projectDir], repoRoot);
}

function searchBuildArgs() {
  const args = ["--embedding-backend", searchEmbeddingBackend];
  if (searchEmbeddingModel) {
    args.push("--embedding-model", searchEmbeddingModel);
  }
  return args;
}

function run(command: string, args: string[], cwd: string) {
  return new Promise<void>((resolveRun, rejectRun) => {
    const child = spawn(command, args, { cwd, stdio: "pipe" });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", rejectRun);
    child.on("close", (code) => {
      if (code === 0) {
        resolveRun();
      } else {
        rejectRun(new Error(`${basename(command)} ${args.join(" ")} failed (${code}): ${stderr.trim()}`));
      }
    });
  });
}

function runJson(command: string, args: string[], cwd: string) {
  return new Promise<unknown>((resolveRun, rejectRun) => {
    const child = spawn(command, args, { cwd, stdio: "pipe" });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", rejectRun);
    child.on("close", (code) => {
      if (code !== 0) {
        rejectRun(new Error(`${basename(command)} ${args.join(" ")} failed (${code}): ${stderr.trim()}`));
        return;
      }
      try {
        resolveRun(JSON.parse(stdout));
      } catch (error) {
        rejectRun(new Error(`Failed to parse JSON output from ${basename(command)}: ${error instanceof Error ? error.message : String(error)}`));
      }
    });
  });
}

async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) : {};
}

function readJsonlIfExists(path: string): Array<Record<string, unknown>> {
  if (!existsSync(path)) {
    return [];
  }
  return readFileSync(path, "utf8")
    .split(/\r?\n/)
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line));
}

function resolveVideoPath(tape: Record<string, unknown>) {
  const absolutePath = typeof tape.path === "string" ? resolve(tape.path) : "";
  if (absolutePath) {
    return absolutePath;
  }
  const relativePath = typeof tape.relative_path === "string" ? tape.relative_path : "";
  if (!relativePath) {
    return "";
  }
  return resolve(projectDir, relativePath);
}

// The review API reads the whole archive and can rewrite corrections, so it
// answers only the local UI. A foreign Host header means DNS rebinding; a
// cross-site Origin or Sec-Fetch-Site on a write means another website is
// driving the browser. TAPESPLIT_UI_ALLOWED_HOSTS (comma-separated hostnames)
// widens the Host check for anyone deliberately serving the UI on a LAN.
const allowedHostnames = new Set([
  "127.0.0.1",
  "localhost",
  "[::1]",
  ...(process.env.TAPESPLIT_UI_ALLOWED_HOSTS || "")
    .split(",")
    .map((name) => name.trim().toLowerCase())
    .filter(Boolean),
]);

function isTrustedLocalRequest(req: IncomingMessage): boolean {
  const host = (req.headers.host || "").toLowerCase();
  const hostname = host.startsWith("[") ? host.slice(0, host.indexOf("]") + 1) : host.split(":")[0];
  if (!allowedHostnames.has(hostname)) return false;
  if (req.method === "GET" || req.method === "HEAD") return true;
  const fetchSite = req.headers["sec-fetch-site"];
  if (fetchSite === "cross-site" || fetchSite === "same-site") return false;
  const origin = req.headers.origin;
  if (!origin) return true;
  try {
    return new URL(origin).host.toLowerCase() === host;
  } catch {
    return false;
  }
}

function sendJson(res: ServerResponse, payload: unknown, status = 200) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  res.end(JSON.stringify(payload));
}

// The project bundle is tens of megabytes of JSON; compressing it turns the
// first paint from a file download into a page load.
function sendProjectJson(req: IncomingMessage, res: ServerResponse, payload: unknown) {
  const body = JSON.stringify(payload);
  const acceptsGzip = /\bgzip\b/.test(String(req.headers["accept-encoding"] ?? ""));
  if (!acceptsGzip || body.length < 262144) {
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    res.end(body);
    return;
  }
  res.writeHead(200, {
    "Content-Type": "application/json",
    "Content-Encoding": "gzip",
    "Cache-Control": "no-store",
  });
  res.end(gzipSync(body));
}

function mimeType(ext: string) {
  if (ext === ".jpg" || ext === ".jpeg") return "image/jpeg";
  if (ext === ".png") return "image/png";
  if (ext === ".webp") return "image/webp";
  if (ext === ".mp4" || ext === ".m4v") return "video/mp4";
  if (ext === ".mov") return "video/quicktime";
  return "application/octet-stream";
}

export default defineConfig({
  plugins: [react(), reviewApiPlugin()],
  server: {
    port: 5173,
    strictPort: false,
  },
});
