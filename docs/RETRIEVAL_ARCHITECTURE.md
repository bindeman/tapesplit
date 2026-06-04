# Retrieval Architecture

TapeSplit should use vector search, but not only vector search.

Home-video understanding is a hybrid retrieval problem:

- text search finds exact names, signs, dates, transcript phrases, and OCR
- sparse vectors find rough semantic overlap without heavy dependencies
- dense vectors find paraphrases and similar descriptions
- graph edges recover people/place/event relationships
- temporal intervals keep scenes, carry-forward context, and nearby evidence
  bounded in time
- entity-resolution memory carries reviewed corrections across future tapes

## Current MVP

The local MVP uses one SQLite database:

```text
search.sqlite
  documents          transcript/evidence/event/group/context records
  documents_fts      SQLite FTS5 text index when available
  doc_terms          dependency-free sparse vectors
  doc_norms          sparse vector norms
  term_stats         IDF values
  dense_vectors      optional sentence-transformer vectors
  dense_vector_index optional sqlite-vec index when sqlite-vec is installed
```

Supported commands:

```bash
tapesplit search build /path/to/project.tapesplit
tapesplit search query /path/to/project.tapesplit "first day of school"
tapesplit search similar /path/to/project.tapesplit canonical_event_000080 --record-type event
```

The default build is fully local and dependency-free. If `sentence-transformers`
is installed, `--embedding-backend sentence-transformers` adds dense local
embeddings. Similarity search uses sparse vectors by default and dense vectors
when present. If `sqlite-vec` is installed, the dense vectors are also written
to a SQLite vector index and queried there before falling back to exact JSON
vector scan.

For small family hauls, exact vector scan in SQLite is acceptable. `sqlite-vec`
keeps vector search inside the same local SQLite artifact. The product can move
to a heavier approximate nearest-neighbor store when the number of indexed
records becomes large enough to make local SQLite vector search slow.

## Better Accuracy Than Plain Vector Search

### 1. Multi-Vector Records

Each clip/event should eventually have several vectors, not one:

```text
transcript_original
transcript_translation
visual_caption
ocr_text
place_context
people_context
narrative_summary
```

This avoids one summary vector washing out important details like a sign,
spoken date, or uncommon name.

### 2. Late Fusion

Rank results by combining independent signals:

```text
score =
  text_bm25
  + sparse_semantic_similarity
  + dense_embedding_similarity
  + graph_context_boost
  + temporal_proximity_boost
  + reviewed_entity_boost
  - excluded_or_conflicting_context_penalty
```

This is more accurate than asking a vector store for nearest neighbors and
trusting that result directly.

### 3. Graph-Aware Reranking

Use `context_edges.jsonl` as a graph index:

```text
event -> person
event -> place
event -> date
place -> nearby_place
place -> inside_place
person -> appears_with
event -> same_date_context
```

When a query or anchor result mentions `Apartment complex`, boost records
connected through `nearby_time_place_context` to `Community gardens`,
`Kindergarten`, and `Playground`, but keep the edge reviewable and
non-exportable as GPS.

### 4. Temporal Interval Index

A local interval index should answer:

```text
what happened within +/- 10 minutes?
what evidence overlaps this event?
what was the most recent direct date/place anchor?
where should carry-forward context stop?
```

SQLite can start with indexed `start_s`/`end_s` columns. Later, use SQLite RTree
or an in-memory interval tree for faster overlap queries.

### 5. Entity Candidate Indexes

Use blocking keys and union-find style candidate clusters for reviewed aliases:

```text
Philip / Phillip
Maplewood School / Maplewood Elementary School
home scoped to Madison era
home scoped to Oregon era
```

Do not collapse these silently. Store candidate links, reasons, conflicts, and
review state.

### 6. When To Add A Dedicated Vector Store

Keep SQLite until it becomes a bottleneck. Move dense vectors to a dedicated
local vector store when:

- exact scan becomes slow enough to affect review UX
- the index grows past hundreds of thousands of records
- we need filtered ANN queries by family, tape, time range, modality, or entity
- we want memory-mapped vector files or server-side vector filtering

Good candidates to evaluate later:

```text
sqlite-vec  current local vector extension, stays close to SQLite packaging
LanceDB     local embedded vector tables with metadata filtering
FAISS       fast local ANN library for dense vectors
Qdrant      local/server vector database with filtering and payloads
```

The product advantage is not the vector database. It is the evidence-aware
fusion layer over vectors, graph edges, time intervals, and user corrections.
