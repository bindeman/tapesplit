# TapeSplit Review UI

Local React review surface for a generated `.tapesplit` project.

```bash
cd apps/review-ui
npm install
TAPESPLIT_PROJECT=/path/to/project.tapesplit npm run dev
```

The Vite dev server exposes a local middleware API:

- `GET /api/project` loads `visualization.json` and pending review actions.
- `GET /api/asset?path=...` serves project-relative thumbnails and keyframes.
- `GET /api/video?id=...` streams a source tape by `video_id` with byte-range support for the player.
- `POST /api/actions` appends review actions to `review-actions.pending.jsonl`.
- `DELETE /api/actions?id=...` removes one pending action, or clears all when no id is provided.
- `POST /api/apply` runs `tapesplit review apply`, rebuilds review evidence/search, and refreshes `visualization.json`.
- `POST /api/reapply` runs `tapesplit review reapply`, rebuilds review evidence/search, and refreshes `visualization.json`.

Search refresh defaults to the dependency-free local sparse index. Set
`TAPESPLIT_SEARCH_EMBEDDING_BACKEND=sentence-transformers` and optionally
`TAPESPLIT_SEARCH_EMBEDDING_MODEL=...` when you want the UI middleware to
rebuild dense search after corrections.

The UI is intentionally project-directory based so the same React surface can later be wrapped by Electron with a native project picker and packaged Python/CLI backend.
