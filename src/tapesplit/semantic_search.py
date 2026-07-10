"""Semantic global search: multilingual text vectors + CLIP text→image.

`search.py` builds the hybrid FTS/sparse index on every finalize; this module
upgrades that index with the layers that make search *semantic*:

- dense multilingual sentence embeddings for the user-facing document types
  (transcripts, events, albums, people, places, captions, OCR), cached by
  content hash in ``semantic_cache.sqlite`` so re-finalizes only encode new
  text;
- a ``clip_vectors`` table copied from the visual-embed stage's scene
  keyframe embeddings, queried with the CLIP text tower so "birthday cake"
  finds footage nobody transcribed or captioned (English queries only — CLIP's
  text tower is not multilingual);
- a sectioned query API (People / Places / Moments / Spoken / Seen) consumed
  by the CLI, the review UI's ⌘K overlay, and a warm stdin/stdout sidecar so
  interactive queries never pay model-load time.

The store stays plain SQLite (sqlite-vec when available): local-first, one
file, no server. Brute-force cosine over ~60k vectors is single-digit
milliseconds; an ANN index can be added later without schema changes.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from tapesplit.search import DEFAULT_EMBEDDING_MODEL, SEARCH_DB_NAME
from tapesplit.storage import read_jsonl
from tapesplit.visual_embeddings import DEFAULT_VISUAL_EMBEDDING_MODEL

SEMANTIC_CACHE_DB_NAME = "semantic_cache.sqlite"
DEFAULT_CLIP_MODEL = DEFAULT_VISUAL_EMBEDDING_MODEL

# Document types worth dense-embedding: the user-facing search surface.
# Graph plumbing (edges, alignments, corrections) stays sparse/FTS-only.
EMBEDDED_RECORD_TYPES = {
    "transcript",
    "event",
    "album",
    "event_group",
    "people_group",
    "place_group",
    "visual_caption",
    "visual_text",
}

SECTION_RECORD_TYPES = {
    "people": {"people_group"},
    "places": {"place_group"},
    "moments": {"event", "album", "event_group"},
    "spoken": {"transcript"},
    "seen": {"visual_caption", "visual_text"},
}
SECTIONS = tuple(SECTION_RECORD_TYPES)

CLIP_MIN_SCORE = 0.2
DEFAULT_LIMIT_PER_SECTION = 8

TextEncoder = Callable[[list[str]], list[list[float]]]


def check_semantic_search_config() -> dict[str, Any]:
    try:
        import sentence_transformers  # noqa: F401

        available = True
    except ImportError:
        available = False
    return {
        "semantic_search_backend": "sentence-transformers" if available else "",
        "semantic_search_text_model": DEFAULT_EMBEDDING_MODEL if available else "",
        "semantic_search_clip_model": DEFAULT_CLIP_MODEL if available else "",
    }


# ---------------------------------------------------------------------------
# Index upgrade


def upgrade_search_index_semantic(
    project_dir: Path,
    *,
    model_name: str = DEFAULT_EMBEDDING_MODEL,
    clip_model_name: str = DEFAULT_CLIP_MODEL,
    include_clip: bool = True,
    batch_size: int = 128,
    text_encoder: TextEncoder | None = None,
) -> dict[str, Any]:
    """Add dense text vectors and CLIP scene vectors to search.sqlite.

    Idempotent and incremental: embeddings are cached by content hash in
    ``semantic_cache.sqlite`` (which survives index rebuilds), so re-running
    after a finalize only encodes text that actually changed.
    """

    project = project_dir.expanduser().resolve()
    db_path = project / SEARCH_DB_NAME
    if not db_path.exists():
        raise FileNotFoundError(
            f"search index not found at {db_path}; run `tapesplit search build {project}` (or finalize) first"
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cache = sqlite3.connect(project / SEMANTIC_CACHE_DB_NAME)
    try:
        _ensure_cache_schema(cache)
        docs = conn.execute(
            "SELECT id, record_type, title, text FROM documents"
        ).fetchall()
        targets = [row for row in docs if row["record_type"] in EMBEDDED_RECORD_TYPES]
        texts = [_document_text(row) for row in targets]
        hashes = [_content_hash(text) for text in texts]

        cached = _cached_vectors(cache, hashes, model_name)
        missing_indexes = [index for index, digest in enumerate(hashes) if digest not in cached]
        if missing_indexes:
            encoder = text_encoder or _sentence_transformer_encoder(model_name)
            for start in range(0, len(missing_indexes), batch_size):
                chunk = missing_indexes[start : start + batch_size]
                vectors = encoder([texts[index] for index in chunk])
                for index, vector in zip(chunk, vectors, strict=True):
                    values = [round(float(value), 6) for value in _normalized(vector)]
                    cached[hashes[index]] = values
                    cache.execute(
                        "INSERT OR REPLACE INTO text_embeddings (hash, model, dim, vector_json) VALUES (?, ?, ?, ?)",
                        (hashes[index], model_name, len(values), json.dumps(values)),
                    )
                cache.commit()

        _replace_dense_vectors(conn, targets, hashes, cached, model_name)
        clip_count = 0
        if include_clip:
            clip_count = _build_clip_table(conn, project, clip_model_name)
        conn.commit()
    finally:
        cache.close()
        conn.close()

    return {
        "project": str(project),
        "output": str(db_path),
        "model": model_name,
        "documents": len(targets),
        "embedded": len(missing_indexes),
        "cached_hits": len(targets) - len(missing_indexes),
        "clip_vectors": clip_count,
        "clip_model": clip_model_name if include_clip else "",
    }


def _document_text(row: sqlite3.Row) -> str:
    return f"{row['title'] or ''}\n{row['text'] or ''}".strip()


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _ensure_cache_schema(cache: sqlite3.Connection) -> None:
    cache.execute(
        """
        CREATE TABLE IF NOT EXISTS text_embeddings (
          hash TEXT NOT NULL,
          model TEXT NOT NULL,
          dim INTEGER NOT NULL,
          vector_json TEXT NOT NULL,
          PRIMARY KEY (hash, model)
        )
        """
    )
    cache.commit()


def _cached_vectors(
    cache: sqlite3.Connection, hashes: list[str], model_name: str
) -> dict[str, list[float]]:
    found: dict[str, list[float]] = {}
    unique = list(dict.fromkeys(hashes))
    for start in range(0, len(unique), 500):
        chunk = unique[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = cache.execute(
            f"SELECT hash, vector_json FROM text_embeddings WHERE model = ? AND hash IN ({placeholders})",
            (model_name, *chunk),
        ).fetchall()
        for digest, vector_json in rows:
            found[digest] = json.loads(vector_json)
    return found


def _replace_dense_vectors(
    conn: sqlite3.Connection,
    targets: list[sqlite3.Row],
    hashes: list[str],
    vectors_by_hash: dict[str, list[float]],
    model_name: str,
) -> None:
    conn.execute("DELETE FROM dense_vectors")
    sqlite_vec = _load_sqlite_vec(conn)  # vec0 tables need the extension even to DROP
    try:
        conn.execute("DROP TABLE IF EXISTS dense_vector_index")
    except sqlite3.OperationalError:
        pass  # extension missing and no vec table to replace; JSON fallback still works
    sample = next((vectors_by_hash[digest] for digest in hashes if digest in vectors_by_hash), None)
    vector_index = None
    if sample:
        if sqlite_vec:
            conn.execute(
                f"CREATE VIRTUAL TABLE dense_vector_index USING vec0(document_id TEXT PRIMARY KEY, embedding float[{len(sample)}])"
            )
            vector_index = sqlite_vec
    for row, digest in zip(targets, hashes, strict=True):
        values = vectors_by_hash.get(digest)
        if not values:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO dense_vectors (document_id, backend, model, dim, vector_json) VALUES (?, ?, ?, ?, ?)",
            (row["id"], "sentence-transformers", model_name, len(values), json.dumps(values)),
        )
        if vector_index:
            conn.execute(
                "INSERT INTO dense_vector_index (document_id, embedding) VALUES (?, ?)",
                (row["id"], vector_index.serialize_float32(values)),
            )
    for key, value in [
        ("embedding_backend", "sentence-transformers"),
        ("embedding_model", model_name),
        ("dense_vector_index", "sqlite-vec" if vector_index else "exact-json"),
    ]:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def _build_clip_table(conn: sqlite3.Connection, project: Path, clip_model_name: str) -> int:
    thumbnails = {
        str(row.get("id") or ""): str(row.get("thumbnail_path") or row.get("keyframe_path") or "")
        for row in read_jsonl(project / "visual_assets.jsonl")
    }
    conn.execute("DROP TABLE IF EXISTS clip_vectors")
    conn.execute(
        """
        CREATE TABLE clip_vectors (
          asset_id TEXT PRIMARY KEY,
          source_video_id TEXT,
          subject_id TEXT,
          time_s REAL,
          start_s REAL,
          end_s REAL,
          thumbnail_path TEXT,
          dim INTEGER NOT NULL,
          vector_json TEXT NOT NULL
        )
        """
    )
    count = 0
    for row in read_jsonl(project / "visual_embeddings.jsonl"):
        if str(row.get("source_subject_type") or "") != "scene":
            continue
        vector = row.get("vector")
        asset_id = str(row.get("visual_asset_id") or row.get("id") or "")
        if not vector or not asset_id:
            continue
        values = [round(float(value), 6) for value in _normalized(vector)]
        conn.execute(
            "INSERT OR REPLACE INTO clip_vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                asset_id,
                str(row.get("source_video_id") or ""),
                str(row.get("source_subject_id") or ""),
                _float_or_none(row.get("time_s")),
                _float_or_none(row.get("start_s")),
                _float_or_none(row.get("end_s")),
                thumbnails.get(asset_id, ""),
                len(values),
                json.dumps(values),
            ),
        )
        count += 1
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("clip_model", clip_model_name)
    )
    return count


# ---------------------------------------------------------------------------
# Sectioned query


def semantic_query(
    project_dir: Path,
    query: str,
    *,
    limit_per_section: int = DEFAULT_LIMIT_PER_SECTION,
    sections: tuple[str, ...] = SECTIONS,
    text_encoder: TextEncoder | None = None,
    clip_encoder: TextEncoder | None = None,
) -> dict[str, Any]:
    from tapesplit.search import _documents_by_id, _fts_scores, _snippet, _sparse_scores

    project = project_dir.expanduser().resolve()
    db_path = project / SEARCH_DB_NAME
    if not db_path.exists():
        raise FileNotFoundError(
            f"search index not found at {db_path}; run `tapesplit search build {project}` first"
        )
    started = time.monotonic()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        semantic_ready = _meta(conn, "embedding_backend") == "sentence-transformers"
        docs = _documents_by_id(conn)
        fts = _fts_scores(conn, query, limit=200)
        sparse = _sparse_scores(conn, query)
        dense: dict[str, float] = {}
        if semantic_ready:
            model_name = _meta(conn, "embedding_model") or DEFAULT_EMBEDDING_MODEL
            encoder = text_encoder or _sentence_transformer_encoder(model_name)
            query_vector = _normalized(encoder([query])[0])
            dense = _dense_scores_for_vector(conn, query_vector)

        query_text = query.casefold().strip()
        scored: dict[str, list[dict[str, Any]]] = {section: [] for section in sections}
        for doc_id in set(fts) | set(sparse) | set(dense):
            doc = docs.get(doc_id)
            if not doc:
                continue
            section = _section_for(doc["record_type"], sections)
            if section is None:
                continue
            exact = 0.25 if query_text and query_text in f"{doc['title']} {doc['text']}".casefold() else 0.0
            score = dense.get(doc_id, 0.0) + 0.8 * sparse.get(doc_id, 0.0) + 0.35 * fts.get(doc_id, 0.0) + exact
            scored[section].append(_query_result(doc, score, _snippet(doc["text"] or doc["title"], query)))

        seen_clip: list[dict[str, Any]] = []
        if "seen" in sections and _has_table(conn, "clip_vectors"):
            clip_model = _meta(conn, "clip_model") or DEFAULT_CLIP_MODEL
            encoder = clip_encoder or _sentence_transformer_encoder(clip_model)
            clip_vector = _normalized(encoder([query])[0])
            seen_clip = _clip_hits(conn, clip_vector, limit=limit_per_section)

        result_sections: dict[str, list[dict[str, Any]]] = {}
        for section in sections:
            rows = sorted(scored[section], key=lambda row: row["score"], reverse=True)[:limit_per_section]
            if section == "seen":
                rows = (seen_clip + rows)[: max(limit_per_section, len(seen_clip))]
            result_sections[section] = rows
    finally:
        conn.close()

    return {
        "project": str(project),
        "query": query,
        "semantic": semantic_ready,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "sections": result_sections,
    }


def _query_result(doc: dict[str, Any], score: float, snippet: str) -> dict[str, Any]:
    return {
        "kind": "document",
        "score": round(score, 4),
        "record_type": doc["record_type"],
        "source_id": doc["source_id"],
        "source_video_id": doc["source_video_id"] or None,
        "start_s": doc["start_s"],
        "end_s": doc["end_s"],
        "title": doc["title"] or "",
        "snippet": snippet,
        "metadata": doc["metadata"],
    }


def _clip_hits(conn: sqlite3.Connection, query_vector: list[float], *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM clip_vectors").fetchall()
    hits = []
    for row in rows:
        vector = json.loads(row["vector_json"])
        score = sum(a * b for a, b in zip(query_vector, vector, strict=False))
        if score < CLIP_MIN_SCORE:
            continue
        hits.append(
            {
                "kind": "keyframe",
                "score": round(score, 4),
                "record_type": "clip_keyframe",
                "source_id": row["asset_id"],
                "source_video_id": row["source_video_id"] or None,
                "start_s": row["start_s"],
                "end_s": row["end_s"],
                "time_s": row["time_s"],
                "title": "",
                "snippet": "",
                "thumbnail_path": row["thumbnail_path"] or "",
                "metadata": {"subject_id": row["subject_id"]},
            }
        )
    hits.sort(key=lambda hit: hit["score"], reverse=True)
    return hits[:limit]


def _dense_scores_for_vector(conn: sqlite3.Connection, query_vector: list[float]) -> dict[str, float]:
    sqlite_vec = _load_sqlite_vec(conn)
    if sqlite_vec and _has_table(conn, "dense_vector_index"):
        total = conn.execute("SELECT COUNT(*) FROM dense_vectors").fetchone()[0]
        if total:
            rows = conn.execute(
                "SELECT document_id, distance FROM dense_vector_index WHERE embedding MATCH ? AND k = ?",
                (sqlite_vec.serialize_float32(query_vector), min(int(total), 4096)),
            ).fetchall()
            return {
                row["document_id"]: max(-1.0, min(1.0, 1.0 - (row["distance"] ** 2) / 2.0))
                for row in rows
            }
    rows = conn.execute("SELECT document_id, vector_json FROM dense_vectors").fetchall()
    return {
        row["document_id"]: sum(
            a * b for a, b in zip(query_vector, json.loads(row["vector_json"]), strict=False)
        )
        for row in rows
    }


def _section_for(record_type: str, sections: tuple[str, ...]) -> str | None:
    for section in sections:
        if record_type in SECTION_RECORD_TYPES.get(section, set()):
            return section
    return None


# ---------------------------------------------------------------------------
# Warm sidecar (stdin/stdout JSON lines)


def handle_semantic_request(
    request: dict[str, Any],
    project: Path,
    *,
    text_encoder: TextEncoder | None,
    clip_encoder: TextEncoder | None,
) -> dict[str, Any]:
    request_id = request.get("id")
    op = str(request.get("op") or "query")
    try:
        if op == "ping":
            return {"id": request_id, "ok": True, "pong": True}
        if op == "query":
            query = str(request.get("q") or "").strip()
            if not query:
                return {"id": request_id, "ok": True, "result": {"query": "", "sections": {}}}
            limit = int(request.get("limit") or DEFAULT_LIMIT_PER_SECTION)
            result = semantic_query(
                project,
                query,
                limit_per_section=max(1, min(limit, 24)),
                text_encoder=text_encoder,
                clip_encoder=clip_encoder,
            )
            return {"id": request_id, "ok": True, "result": result}
        return {"id": request_id, "ok": False, "error": f"unknown op: {op}"}
    except Exception as exc:  # sidecar must never die on a bad query
        return {"id": request_id, "ok": False, "error": str(exc)}


def serve_semantic_search(
    project_dir: Path,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Long-lived JSON-lines server: loads models once, answers until EOF."""

    project = project_dir.expanduser().resolve()
    reader = stdin or sys.stdin
    writer = stdout or sys.stdout

    text_encoder: TextEncoder | None = None
    clip_encoder: TextEncoder | None = None
    db_path = project / SEARCH_DB_NAME
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            if _meta(conn, "embedding_backend") == "sentence-transformers":
                text_encoder = _sentence_transformer_encoder(
                    _meta(conn, "embedding_model") or DEFAULT_EMBEDDING_MODEL
                )
            if _has_table(conn, "clip_vectors"):
                clip_encoder = _sentence_transformer_encoder(
                    _meta(conn, "clip_model") or DEFAULT_CLIP_MODEL
                )
        finally:
            conn.close()

    writer.write(json.dumps({"ok": True, "ready": True, "semantic": text_encoder is not None}) + "\n")
    writer.flush()
    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            writer.write(json.dumps({"ok": False, "error": f"bad request json: {exc}"}) + "\n")
            writer.flush()
            continue
        response = handle_semantic_request(
            request, project, text_encoder=text_encoder, clip_encoder=clip_encoder
        )
        writer.write(json.dumps(response) + "\n")
        writer.flush()


# ---------------------------------------------------------------------------
# Helpers


_ENCODER_CACHE: dict[str, Any] = {}


def _sentence_transformer_encoder(model_name: str) -> TextEncoder:
    model = _ENCODER_CACHE.get(model_name)
    if model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "semantic search needs sentence-transformers (install '.[local-ai]')"
            ) from exc
        model = SentenceTransformer(model_name)
        _ENCODER_CACHE[model_name] = model

    def encode(texts: list[str]) -> list[list[float]]:
        vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [[float(value) for value in vector] for vector in vectors]

    return encode


def _normalized(values: Any) -> list[float]:
    vector = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0] if not isinstance(row, sqlite3.Row) else row["value"]) if row else ""


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def _load_sqlite_vec(conn: sqlite3.Connection) -> Any | None:
    try:
        import sqlite_vec
    except ImportError:
        return None
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return sqlite_vec
    except sqlite3.Error:
        return None
