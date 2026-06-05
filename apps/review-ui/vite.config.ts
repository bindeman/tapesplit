import react from "@vitejs/plugin-react";
import { spawn } from "node:child_process";
import { createReadStream, existsSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { appendFile, mkdir, readFile, rm } from "node:fs/promises";
import { basename, dirname, extname, join, resolve, sep } from "node:path";
import type { IncomingMessage, ServerResponse } from "node:http";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const appRoot = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(appRoot, "../..");
const defaultProject = resolve(repoRoot, "examples/family-haul.tapesplit");
const projectDir = resolve(process.env.TAPESPLIT_PROJECT || defaultProject);
const pendingActionsPath = join(projectDir, "review-actions.pending.jsonl");
const appliedActionsPath = join(projectDir, "review-actions.applied.jsonl");
const tapesplitBin = existsSync(join(repoRoot, ".venv/bin/tapesplit"))
  ? join(repoRoot, ".venv/bin/tapesplit")
  : "tapesplit";

function reviewApiPlugin(): Plugin {
  return {
    name: "tapesplit-review-api",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (!req.url?.startsWith("/api/")) {
          next();
          return;
        }
        try {
          if (req.method === "GET" && req.url.startsWith("/api/project")) {
            await sendJson(res, await loadProject());
            return;
          }
          if (req.method === "GET" && req.url.startsWith("/api/asset")) {
            await sendAsset(req, res);
            return;
          }
          if (req.method === "POST" && req.url.startsWith("/api/actions")) {
            const body = await readJson(req);
            const actions = Array.isArray(body) ? body : [body];
            await appendPendingActions(actions);
            await sendJson(res, await loadProject());
            return;
          }
          if (req.method === "DELETE" && req.url.startsWith("/api/actions")) {
            const url = new URL(req.url, "http://localhost");
            const actionId = url.searchParams.get("id");
            await removePendingAction(actionId);
            await sendJson(res, await loadProject());
            return;
          }
          if (req.method === "POST" && req.url.startsWith("/api/apply")) {
            await applyPendingActions();
            await sendJson(res, await loadProject());
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
    return;
  }
  await run(tapesplitBin, ["review", "apply", projectDir, pendingActionsPath], repoRoot);
  await run(tapesplitBin, ["export-visualization", projectDir], repoRoot);
  await appendFile(appliedActionsPath, pending.map((row) => JSON.stringify(row)).join("\n") + "\n", "utf8");
  const archivePath = join(projectDir, `review-actions.applied.${new Date().toISOString().replace(/[:.]/g, "-")}.jsonl`);
  renameSync(pendingActionsPath, archivePath);
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

function sendJson(res: ServerResponse, payload: unknown, status = 200) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  res.end(JSON.stringify(payload));
}

function mimeType(ext: string) {
  if (ext === ".jpg" || ext === ".jpeg") return "image/jpeg";
  if (ext === ".png") return "image/png";
  if (ext === ".webp") return "image/webp";
  return "application/octet-stream";
}

export default defineConfig({
  plugins: [react(), reviewApiPlugin()],
  server: {
    port: 5173,
    strictPort: false,
  },
});
