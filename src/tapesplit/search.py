from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any

from tapesplit.storage import read_jsonl
from tapesplit.visibility import build_visibility_filter


SEARCH_DB_NAME = "search.sqlite"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}

SEMANTIC_EXPANSIONS = {
    "birthday": {"cake", "party", "candles", "celebration"},
    "beach": {"ocean", "water", "swimming", "shore"},
    "child": {"kid", "boy", "girl", "children"},
    "classroom": {"school", "teacher", "students"},
    "family": {"home", "children", "parents"},
    "hike": {"trail", "mountain", "walk", "butte"},
    "school": {"classroom", "teacher", "students"},
    "swim": {"swimming", "water", "ocean", "pool"},
    "travel": {"trip", "vacation", "visit"},
    "trip": {"travel", "vacation", "visit"},
    "volcano": {"lava", "crater", "steam", "eruption"},
}


def build_search_index(
    project_dir: Path,
    *,
    include_groups: bool = True,
    embedding_backend: str = "local-sparse",
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    docs = collect_search_documents(project, include_groups=include_groups)
    db_path = project / SEARCH_DB_NAME
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        fts_enabled = _create_schema(conn)
        _insert_documents(conn, docs, fts_enabled=fts_enabled)
        _insert_sparse_vectors(conn, docs)
        dense_backend = _insert_dense_vectors(
            conn,
            docs,
            embedding_backend=embedding_backend,
            embedding_model=embedding_model,
        )
        conn.commit()
        dense_vector_index = _meta_value(conn, "dense_vector_index")
    finally:
        conn.close()
    return {
        "project": str(project),
        "output": str(db_path),
        "documents": len(docs),
        "fts_enabled": fts_enabled,
        "backend": _backend_label(fts_enabled=fts_enabled, dense_backend=dense_backend),
        "embedding_backend": dense_backend,
        "embedding_model": embedding_model if dense_backend == "sentence-transformers" else "",
        "dense_vector_index": dense_vector_index if dense_backend == "sentence-transformers" else "",
        "by_type": dict(Counter(doc["record_type"] for doc in docs)),
    }


def query_search_index(project_dir: Path, query: str, *, limit: int = 10) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    db_path = project / SEARCH_DB_NAME
    if not db_path.exists():
        raise FileNotFoundError(f"search index not found, run `tapesplit search build {project}` first")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        docs = _documents_by_id(conn)
        fts_scores = _fts_scores(conn, query, limit=max(limit * 4, 25))
        sparse_scores = _sparse_scores(conn, query)
        dense_scores = _dense_scores(conn, query)
        results = _rank_results(docs, query, fts_scores, sparse_scores, dense_scores, limit=limit)
    finally:
        conn.close()
    return {
        "project": str(project),
        "query": query,
        "results": results,
    }


def similar_search_documents(
    project_dir: Path,
    source_id: str,
    *,
    record_type: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    project = project_dir.expanduser().resolve()
    db_path = project / SEARCH_DB_NAME
    if not db_path.exists():
        raise FileNotFoundError(f"search index not found, run `tapesplit search build {project}` first")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        docs = _documents_by_id(conn)
        document_id = _resolve_document_id(docs, source_id, record_type=record_type)
        anchor = docs[document_id]
        sparse_scores = _sparse_document_scores(conn, document_id)
        dense_scores = _dense_document_scores(conn, document_id)
        results = _rank_similarity_results(docs, anchor, sparse_scores, dense_scores, limit=limit)
    finally:
        conn.close()
    return {
        "project": str(project),
        "source_id": source_id,
        "record_type": record_type,
        "anchor": _result_document(anchor),
        "results": results,
    }


def collect_search_documents(project: Path, *, include_groups: bool = True) -> list[dict[str, Any]]:
    docs = []
    visibility = build_visibility_filter(project)
    for row in read_jsonl(project / "transcript_segments.jsonl"):
        if visibility.excluded_row(row):
            continue
        docs.append(
            _document(
                "transcript",
                row.get("id"),
                source_video_id=row.get("source_video_id"),
                start_s=row.get("start_s"),
                end_s=row.get("end_s"),
                title=f"Transcript {_range_label(row.get('start_s'), row.get('end_s'))}",
                text=row.get("text"),
                metadata={
                    "language": row.get("language"),
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                },
            )
        )

    evidence_rows = read_jsonl(project / "evidence.jsonl") + read_jsonl(project / "gemini_evidence.jsonl")
    evidence_by_id = {row.get("id"): row for row in evidence_rows if row.get("id")}
    for row in read_jsonl(project / "scenes.jsonl"):
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        docs.append(
            _document(
                "scene",
                row.get("id"),
                source_video_id=row.get("source_video_id"),
                start_s=row.get("start_s"),
                end_s=row.get("end_s"),
                title=f"Scene {_range_label(row.get('start_s'), row.get('end_s'))}",
                text=" ".join(
                    [
                        str(row.get("scene_type") or ""),
                        str(row.get("label") or ""),
                        " ".join(_string_list(row.get("start_boundary_reasons"))),
                        " ".join(_string_list(row.get("end_boundary_reasons"))),
                    ]
                ),
                metadata={
                    "scene_type": row.get("scene_type"),
                    "label": row.get("label"),
                    "method": row.get("method"),
                },
            )
        )

    for row in evidence_rows:
        if row.get("kind") == "non_content_range":
            continue
        if visibility.excluded_row(row):
            continue
        docs.append(
            _document(
                "evidence",
                row.get("id"),
                source_video_id=row.get("source_video_id"),
                start_s=row.get("start_s"),
                end_s=row.get("end_s"),
                title=str(row.get("kind") or "Evidence"),
                text=row.get("text"),
                metadata={
                    "kind": row.get("kind"),
                    "modality": row.get("modality"),
                    "language": row.get("language"),
                },
            )
        )

    events = read_jsonl(project / "canonical_events.jsonl") or (
        read_jsonl(project / "events.jsonl") + read_jsonl(project / "gemini_events.jsonl")
    )
    for row in events:
        if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        docs.append(
            _document(
                "event",
                row.get("id"),
                source_video_id=_first_source_video_id(row.get("evidence_ids"), evidence_by_id),
                start_s=row.get("start_s"),
                end_s=row.get("end_s"),
                title=row.get("title"),
                text=" ".join(
                    [
                        str(row.get("summary") or ""),
                        " ".join(_string_list(metadata.get("people"))),
                        " ".join(_string_list(metadata.get("place_candidates"))),
                        " ".join(_string_list(metadata.get("date_candidates"))),
                        str(metadata.get("event_type") or ""),
                        str(row.get("relatedness") or ""),
                    ]
                ),
                metadata={"source": row.get("source"), "event_type": metadata.get("event_type")},
            )
        )

    if include_groups:
        for filename, record_type, title_key in [
            ("albums.jsonl", "album", "title"),
            ("event_groups.jsonl", "event_group", "title"),
            ("people_groups.jsonl", "people_group", "label"),
            ("place_groups.jsonl", "place_group", "label"),
            ("date_groups.jsonl", "date_group", "label"),
            ("language_groups.jsonl", "language_group", "language"),
            ("relationship_candidates.jsonl", "relationship_candidate", "predicate"),
            ("relationship_review_tasks.jsonl", "relationship_review_task", "question"),
            ("context_edges.jsonl", "context_edge", "predicate"),
            ("edge_metrics.jsonl", "edge_metric", "metric_set"),
            ("event_continuity_contexts.jsonl", "event_continuity_context", "context_label"),
        ]:
            for row in read_jsonl(project / filename):
                if visibility.excluded_row(row, evidence_by_id=evidence_by_id):
                    continue
                docs.append(_group_document(row, record_type=record_type, title_key=title_key))

    return [doc for doc in docs if doc["title"] or doc["text"]]


def _document(
    record_type: str,
    source_id: Any,
    *,
    source_video_id: Any,
    start_s: Any,
    end_s: Any,
    title: Any,
    text: Any,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    fallback_hash = hashlib.sha1(f"{record_type}|{title}|{text}|{start_s}|{end_s}".encode("utf-8")).hexdigest()[:12]
    source_id_text = str(source_id or f"{record_type}_{fallback_hash}")
    return {
        "id": f"{record_type}:{source_id_text}",
        "record_type": record_type,
        "source_id": source_id_text,
        "source_video_id": str(source_video_id or ""),
        "start_s": _number_or_none(start_s),
        "end_s": _number_or_none(end_s),
        "title": str(title or ""),
        "text": str(text or ""),
        "metadata": metadata,
    }


def _group_document(row: dict[str, Any], *, record_type: str, title_key: str) -> dict[str, Any]:
    text_parts = []
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    for key in [
        "album_type",
        "group_type",
        "kind",
        "place_type",
        "date_value",
        "precision",
        "people_labels",
        "place_label",
        "place_labels",
        "scope_label",
        "parent_place_labels",
        "nearby_place_labels",
        "date_label",
        "date_labels",
        "language_labels",
        "aliases",
        "normalized_names",
        "notes",
        "canonical_event_ids",
        "source_video_ids",
        "subject_label",
        "object_label",
        "predicate",
        "supporting_signals",
        "context_label",
        "anchor_event_ids",
        "basis",
        "question",
        "candidate_ids",
        "subject_type",
        "object_type",
        "computed_weight",
        "metric_set",
    ]:
        value = row.get(key)
        if isinstance(value, list):
            text_parts.append(" ".join(str(item) for item in value))
        elif value not in (None, ""):
            text_parts.append(str(value))
    for key in ["canonical_event_ids", "source_video_ids"]:
        value = scope.get(key)
        if isinstance(value, list):
            text_parts.append(" ".join(str(item) for item in value))
    return _document(
        record_type,
        row.get("id"),
        source_video_id=_first_source_id_from_row(row, scope),
        start_s=row.get("start_s") or row.get("first_start_s") or scope.get("start_s"),
        end_s=row.get("end_s") or row.get("last_end_s") or scope.get("end_s"),
        title=row.get(title_key),
        text=" ".join(text_parts),
        metadata={"review_status": row.get("review_status")},
    )


def _first_source_id_from_row(row: dict[str, Any], scope: dict[str, Any]) -> str:
    if isinstance(row.get("source_video_ids"), list) and row["source_video_ids"]:
        return str(row["source_video_ids"][0])
    if isinstance(scope.get("source_video_ids"), list) and scope["source_video_ids"]:
        return str(scope["source_video_ids"][0])
    return ""


def _create_schema(conn: sqlite3.Connection) -> bool:
    conn.executescript(
        """
        CREATE TABLE documents (
          id TEXT PRIMARY KEY,
          record_type TEXT NOT NULL,
          source_id TEXT NOT NULL,
          source_video_id TEXT,
          start_s REAL,
          end_s REAL,
          title TEXT,
          text TEXT,
          metadata_json TEXT
        );
        CREATE TABLE doc_terms (
          document_id TEXT NOT NULL,
          term TEXT NOT NULL,
          weight REAL NOT NULL,
          PRIMARY KEY (document_id, term)
        );
        CREATE INDEX doc_terms_term_idx ON doc_terms(term);
        CREATE TABLE doc_norms (
          document_id TEXT PRIMARY KEY,
          norm REAL NOT NULL
        );
        CREATE TABLE term_stats (
          term TEXT PRIMARY KEY,
          idf REAL NOT NULL
        );
        CREATE TABLE dense_vectors (
          document_id TEXT PRIMARY KEY,
          backend TEXT NOT NULL,
          model TEXT NOT NULL,
          dim INTEGER NOT NULL,
          vector_json TEXT NOT NULL
        );
        CREATE TABLE meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    try:
        conn.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(id UNINDEXED, title, text, record_type UNINDEXED)")
        return True
    except sqlite3.OperationalError:
        return False


def _insert_documents(conn: sqlite3.Connection, docs: list[dict[str, Any]], *, fts_enabled: bool) -> None:
    for doc in docs:
        conn.execute(
            """
            INSERT INTO documents (id, record_type, source_id, source_video_id, start_s, end_s, title, text, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc["id"],
                doc["record_type"],
                doc["source_id"],
                doc["source_video_id"],
                doc["start_s"],
                doc["end_s"],
                doc["title"],
                doc["text"],
                json.dumps(doc["metadata"], sort_keys=True),
            ),
        )
        if fts_enabled:
            conn.execute(
                "INSERT INTO documents_fts (id, title, text, record_type) VALUES (?, ?, ?, ?)",
                (doc["id"], doc["title"], doc["text"], doc["record_type"]),
            )
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("documents", str(len(docs))))


def _insert_sparse_vectors(conn: sqlite3.Connection, docs: list[dict[str, Any]]) -> None:
    doc_counts = {doc["id"]: Counter(_semantic_terms(f"{doc['title']} {doc['text']}")) for doc in docs}
    df: Counter[str] = Counter()
    for counts in doc_counts.values():
        for term in counts:
            df[term] += 1
    total_docs = max(1, len(docs))
    idf = {term: math.log((1 + total_docs) / (1 + freq)) + 1.0 for term, freq in df.items()}
    for term, value in idf.items():
        conn.execute("INSERT INTO term_stats (term, idf) VALUES (?, ?)", (term, value))
    for doc_id, counts in doc_counts.items():
        weights = {}
        for term, count in counts.items():
            weights[term] = (1.0 + math.log(count)) * idf[term]
        norm = math.sqrt(sum(weight * weight for weight in weights.values())) or 1.0
        conn.execute("INSERT INTO doc_norms (document_id, norm) VALUES (?, ?)", (doc_id, norm))
        for term, weight in weights.items():
            conn.execute("INSERT INTO doc_terms (document_id, term, weight) VALUES (?, ?, ?)", (doc_id, term, weight))


def _insert_dense_vectors(
    conn: sqlite3.Connection,
    docs: list[dict[str, Any]],
    *,
    embedding_backend: str,
    embedding_model: str,
) -> str:
    if embedding_backend not in {"local-sparse", "sentence-transformers", "auto"}:
        raise ValueError("--embedding-backend must be local-sparse, sentence-transformers, or auto")
    if embedding_backend == "local-sparse":
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("embedding_backend", "local-sparse"))
        return "local-sparse"
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as exc:
        if embedding_backend == "auto":
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("embedding_backend", "local-sparse"))
            return "local-sparse"
        raise RuntimeError(
            "sentence-transformers is not installed. Install the local-ai extra or use "
            "`--embedding-backend local-sparse`."
        ) from exc

    model = SentenceTransformer(embedding_model)
    texts = [f"{doc['title']}\n{doc['text']}".strip() for doc in docs]
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    vector_index = _create_dense_vector_index(conn, vectors[0] if len(vectors) else [])
    for doc, vector in zip(docs, vectors, strict=False):
        values = [round(float(value), 6) for value in vector]
        conn.execute(
            """
            INSERT INTO dense_vectors (document_id, backend, model, dim, vector_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (doc["id"], "sentence-transformers", embedding_model, len(values), json.dumps(values)),
        )
        if vector_index:
            conn.execute(
                "INSERT INTO dense_vector_index (document_id, embedding) VALUES (?, ?)",
                (doc["id"], vector_index["serialize"](values)),
            )
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("embedding_backend", "sentence-transformers"))
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("embedding_model", embedding_model))
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        ("dense_vector_index", "sqlite-vec" if vector_index else "exact-json"),
    )
    return "sentence-transformers"


def _create_dense_vector_index(conn: sqlite3.Connection, sample_vector: Any) -> dict[str, Any] | None:
    values = [float(value) for value in sample_vector]
    if not values:
        return None
    sqlite_vec = _load_sqlite_vec(conn)
    if not sqlite_vec:
        return None
    dim = len(values)
    conn.execute(f"CREATE VIRTUAL TABLE dense_vector_index USING vec0(document_id TEXT PRIMARY KEY, embedding float[{dim}])")
    return {"serialize": sqlite_vec.serialize_float32}


def _documents_by_id(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM documents").fetchall()
    docs = {}
    for row in rows:
        docs[row["id"]] = {
            "id": row["id"],
            "record_type": row["record_type"],
            "source_id": row["source_id"],
            "source_video_id": row["source_video_id"],
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "title": row["title"],
            "text": row["text"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }
    return docs


def _fts_scores(conn: sqlite3.Connection, query: str, *, limit: int) -> dict[str, float]:
    if not _has_fts(conn):
        return {}
    fts_query = _fts_query(query)
    if not fts_query:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT id, bm25(documents_fts) AS rank
            FROM documents_fts
            WHERE documents_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    scores = {}
    for position, row in enumerate(rows, start=1):
        scores[row["id"]] = 1.0 / position
    return scores


def _sparse_scores(conn: sqlite3.Connection, query: str) -> dict[str, float]:
    query_counts = Counter(_semantic_terms(query))
    if not query_counts:
        return {}
    placeholders = ",".join("?" for _ in query_counts)
    idf_rows = conn.execute(
        f"SELECT term, idf FROM term_stats WHERE term IN ({placeholders})",
        tuple(query_counts),
    ).fetchall()
    idf = {row["term"]: row["idf"] for row in idf_rows}
    query_weights = {
        term: (1.0 + math.log(count)) * idf.get(term, 1.0)
        for term, count in query_counts.items()
    }
    query_norm = math.sqrt(sum(weight * weight for weight in query_weights.values())) or 1.0
    rows = conn.execute(
        f"""
        SELECT dt.document_id, dt.term, dt.weight, dn.norm
        FROM doc_terms dt
        JOIN doc_norms dn ON dn.document_id = dt.document_id
        WHERE dt.term IN ({placeholders})
        """,
        tuple(query_counts),
    ).fetchall()
    dot_products: defaultdict[str, float] = defaultdict(float)
    norms = {}
    for row in rows:
        dot_products[row["document_id"]] += row["weight"] * query_weights.get(row["term"], 0.0)
        norms[row["document_id"]] = row["norm"]
    return {
        doc_id: dot / (query_norm * (norms.get(doc_id) or 1.0))
        for doc_id, dot in dot_products.items()
    }


def _sparse_document_scores(conn: sqlite3.Connection, document_id: str) -> dict[str, float]:
    anchor_rows = conn.execute(
        "SELECT term, weight FROM doc_terms WHERE document_id = ?",
        (document_id,),
    ).fetchall()
    if not anchor_rows:
        return {}
    anchor_weights = {row["term"]: row["weight"] for row in anchor_rows}
    anchor_norm_row = conn.execute(
        "SELECT norm FROM doc_norms WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    anchor_norm = float(anchor_norm_row["norm"]) if anchor_norm_row else 1.0
    placeholders = ",".join("?" for _ in anchor_weights)
    rows = conn.execute(
        f"""
        SELECT dt.document_id, dt.term, dt.weight, dn.norm
        FROM doc_terms dt
        JOIN doc_norms dn ON dn.document_id = dt.document_id
        WHERE dt.term IN ({placeholders})
          AND dt.document_id != ?
        """,
        (*anchor_weights.keys(), document_id),
    ).fetchall()
    dot_products: defaultdict[str, float] = defaultdict(float)
    norms = {}
    for row in rows:
        dot_products[row["document_id"]] += row["weight"] * anchor_weights.get(row["term"], 0.0)
        norms[row["document_id"]] = row["norm"]
    return {
        doc_id: dot / (anchor_norm * (norms.get(doc_id) or 1.0))
        for doc_id, dot in dot_products.items()
    }


def _dense_scores(conn: sqlite3.Connection, query: str) -> dict[str, float]:
    backend = _meta_value(conn, "embedding_backend")
    if backend != "sentence-transformers":
        return {}
    model_name = _meta_value(conn, "embedding_model")
    if not model_name:
        return {}
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "this index was built with sentence-transformers, but the package is not installed"
        ) from exc
    model = SentenceTransformer(model_name)
    query_vector = [float(value) for value in model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]]
    indexed_scores = _sqlite_vec_query_scores(conn, query_vector)
    if indexed_scores is not None:
        return indexed_scores
    rows = conn.execute("SELECT document_id, vector_json FROM dense_vectors").fetchall()
    scores = {}
    for row in rows:
        vector = json.loads(row["vector_json"])
        scores[row["document_id"]] = _dot(query_vector, vector)
    return scores


def _dense_document_scores(conn: sqlite3.Connection, document_id: str) -> dict[str, float]:
    backend = _meta_value(conn, "embedding_backend")
    if backend != "sentence-transformers":
        return {}
    indexed_scores = _sqlite_vec_document_scores(conn, document_id)
    if indexed_scores is not None:
        return indexed_scores
    anchor_row = conn.execute(
        "SELECT vector_json FROM dense_vectors WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if not anchor_row:
        return {}
    anchor_vector = json.loads(anchor_row["vector_json"])
    rows = conn.execute(
        "SELECT document_id, vector_json FROM dense_vectors WHERE document_id != ?",
        (document_id,),
    ).fetchall()
    scores = {}
    for row in rows:
        vector = json.loads(row["vector_json"])
        scores[row["document_id"]] = _dot(anchor_vector, vector)
    return scores


def _sqlite_vec_query_scores(conn: sqlite3.Connection, query_vector: list[float]) -> dict[str, float] | None:
    sqlite_vec = _load_sqlite_vec(conn)
    if not sqlite_vec or not _has_table(conn, "dense_vector_index"):
        return None
    total = _indexed_document_count(conn)
    rows = conn.execute(
        """
        SELECT document_id, distance
        FROM dense_vector_index
        WHERE embedding MATCH ? AND k = ?
        """,
        (sqlite_vec.serialize_float32(query_vector), total),
    ).fetchall()
    return {row["document_id"]: _normalized_l2_to_cosine(row["distance"]) for row in rows}


def _sqlite_vec_document_scores(conn: sqlite3.Connection, document_id: str) -> dict[str, float] | None:
    sqlite_vec = _load_sqlite_vec(conn)
    if not sqlite_vec or not _has_table(conn, "dense_vector_index"):
        return None
    anchor_row = conn.execute(
        "SELECT vector_json FROM dense_vectors WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if not anchor_row:
        return {}
    anchor_vector = json.loads(anchor_row["vector_json"])
    total = _indexed_document_count(conn)
    rows = conn.execute(
        """
        SELECT document_id, distance
        FROM dense_vector_index
        WHERE embedding MATCH ? AND k = ?
        """,
        (sqlite_vec.serialize_float32(anchor_vector), total),
    ).fetchall()
    return {
        row["document_id"]: _normalized_l2_to_cosine(row["distance"])
        for row in rows
        if row["document_id"] != document_id
    }


def _rank_results(
    docs: dict[str, dict[str, Any]],
    query: str,
    fts_scores: dict[str, float],
    sparse_scores: dict[str, float],
    dense_scores: dict[str, float],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    ids = set(fts_scores) | set(sparse_scores) | set(dense_scores)
    query_text = query.casefold().strip()
    ranked = []
    for doc_id in ids:
        doc = docs.get(doc_id)
        if not doc:
            continue
        title = doc["title"] or ""
        text = doc["text"] or ""
        exact_bonus = 0.25 if query_text and query_text in f"{title} {text}".casefold() else 0.0
        score = (
            dense_scores.get(doc_id, 0.0)
            + (0.8 * sparse_scores.get(doc_id, 0.0))
            + (0.35 * fts_scores.get(doc_id, 0.0))
            + exact_bonus
        )
        ranked.append(
            {
                "score": round(score, 4),
                "semantic_score": round(sparse_scores.get(doc_id, 0.0), 4),
                "embedding_score": round(dense_scores.get(doc_id, 0.0), 4),
                "text_score": round(fts_scores.get(doc_id, 0.0), 4),
                "record_type": doc["record_type"],
                "source_id": doc["source_id"],
                "source_video_id": doc["source_video_id"] or None,
                "start_s": doc["start_s"],
                "end_s": doc["end_s"],
                "time_label": _range_label(doc["start_s"], doc["end_s"]),
                "title": title,
                "snippet": _snippet(text or title, query),
                "metadata": doc["metadata"],
            }
        )
    ranked.sort(key=lambda row: row["score"], reverse=True)
    return ranked[:limit]


def _rank_similarity_results(
    docs: dict[str, dict[str, Any]],
    anchor: dict[str, Any],
    sparse_scores: dict[str, float],
    dense_scores: dict[str, float],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    ids = set(sparse_scores) | set(dense_scores)
    ranked = []
    anchor_text = f"{anchor['title']} {anchor['text']}".strip()
    for doc_id in ids:
        doc = docs.get(doc_id)
        if not doc:
            continue
        dense_score = dense_scores.get(doc_id, 0.0)
        sparse_score = sparse_scores.get(doc_id, 0.0)
        score = dense_score + (0.8 * sparse_score)
        result = _result_document(doc, query=anchor_text)
        result.update(
            {
                "score": round(score, 4),
                "similarity_score": round(score, 4),
                "semantic_score": round(sparse_score, 4),
                "embedding_score": round(dense_score, 4),
            }
        )
        ranked.append(result)
    ranked.sort(key=lambda row: row["score"], reverse=True)
    return ranked[:limit]


def _resolve_document_id(
    docs: dict[str, dict[str, Any]],
    source_id: str,
    *,
    record_type: str | None,
) -> str:
    value = str(source_id or "").strip()
    if not value:
        raise ValueError("source_id is required")
    if ":" in value and value in docs:
        if record_type and docs[value]["record_type"] != record_type:
            raise ValueError(f"{value} exists but has record_type={docs[value]['record_type']!r}")
        return value
    matches = [
        doc_id
        for doc_id, doc in docs.items()
        if doc["source_id"] == value and (record_type is None or doc["record_type"] == record_type)
    ]
    if not matches and record_type:
        typed_id = f"{record_type}:{value}"
        if typed_id in docs:
            return typed_id
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"no indexed document found for source_id={value!r}")
    types = sorted({docs[doc_id]["record_type"] for doc_id in matches})
    raise ValueError(
        f"source_id={value!r} matches multiple record types {types}; pass --record-type"
    )


def _result_document(doc: dict[str, Any], *, query: str = "") -> dict[str, Any]:
    title = doc["title"] or ""
    text = doc["text"] or ""
    return {
        "record_type": doc["record_type"],
        "source_id": doc["source_id"],
        "source_video_id": doc["source_video_id"] or None,
        "start_s": doc["start_s"],
        "end_s": doc["end_s"],
        "time_label": _range_label(doc["start_s"], doc["end_s"]),
        "title": title,
        "snippet": _snippet(text or title, query or title),
        "metadata": doc["metadata"],
    }


def _semantic_terms(text: str) -> list[str]:
    base_terms = [
        _light_stem(token)
        for token in re.findall(r"[\w']+", text.casefold(), flags=re.UNICODE)
        if len(token) > 1 and token not in STOPWORDS and not token.isdigit()
    ]
    expanded = []
    for term in base_terms:
        expanded.append(term)
        expanded.extend(SEMANTIC_EXPANSIONS.get(term, set()))
    return expanded


def _fts_query(query: str) -> str:
    terms = [
        token
        for token in re.findall(r"[\w']+", query.casefold(), flags=re.UNICODE)
        if len(token) > 1 and token not in STOPWORDS
    ]
    escaped = [term.replace('"', '""') for term in terms[:12]]
    return " OR ".join(f'"{term}"' for term in escaped)


def _light_stem(token: str) -> str:
    for suffix in ["ing", "ed", "ies", "s"]:
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            if suffix == "ies":
                return token[: -len(suffix)] + "y"
            return token[: -len(suffix)]
    return token


def _has_fts(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "documents_fts")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _meta_value(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


def _normalized_l2_to_cosine(distance: Any) -> float:
    number = _number_or_none(distance) or 0.0
    return max(-1.0, min(1.0, 1.0 - ((number * number) / 2.0)))


def _indexed_document_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM documents").fetchone()
    return max(1, int(row["count"] if row else 1))


def _load_sqlite_vec(conn: sqlite3.Connection) -> Any | None:
    try:
        import sqlite_vec  # type: ignore
    except ImportError:
        return None
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return sqlite_vec
    except sqlite3.Error:
        return None


def _backend_label(*, fts_enabled: bool, dense_backend: str) -> str:
    parts = []
    if fts_enabled:
        parts.append("sqlite_fts5")
    parts.append("local_sparse_vectors")
    if dense_backend == "sentence-transformers":
        parts.append("sentence_transformers")
    return "+".join(parts)


def _snippet(text: str, query: str, *, max_chars: int = 220) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    terms = [term for term in re.findall(r"[\w']+", query.casefold(), flags=re.UNICODE) if len(term) > 2]
    lower = cleaned.casefold()
    position = min((lower.find(term) for term in terms if lower.find(term) >= 0), default=0)
    start = max(0, position - 70)
    end = min(len(cleaned), start + max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(cleaned) else ""
    return prefix + cleaned[start:end].strip() + suffix


def _string_list(values: Any) -> list[str]:
    if isinstance(values, list):
        return [str(item) for item in values if item not in (None, "")]
    if values in (None, ""):
        return []
    return [str(values)]


def _first_source_video_id(evidence_ids: Any, evidence_by_id: dict[str, dict[str, Any]]) -> str:
    for evidence_id in _string_list(evidence_ids):
        source_id = evidence_by_id.get(evidence_id, {}).get("source_video_id")
        if source_id:
            return str(source_id)
    return ""


def _range_label(start_s: Any, end_s: Any) -> str:
    start = _format_seconds(start_s)
    end = _format_seconds(end_s)
    if start and end:
        return f"{start}-{end}"
    return start or end or "unknown time"


def _format_seconds(value: Any) -> str:
    number = _number_or_none(value)
    if number is None:
        return ""
    seconds = max(0, int(round(number)))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
