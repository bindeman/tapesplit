# Evaluation Workflow

TapeSplit evaluation is a family-review loop, not just a benchmark. The goal is
to find out which inferred events, albums, people, places, dates, relationships,
and context edges are correct enough to trust, which need corrections, and which
need another family member.

This is also a good SQL/data-science/prompting project for a reviewer who knows
some family context but is not the final authority.

## Build A Review Packet

Run this after the project has events, groups, relationships, and a context
graph:

```bash
.venv/bin/tapesplit eval build /path/to/family-videos.tapesplit --force
```

The command writes `/path/to/family-videos.tapesplit/eval_packet/`:

- `eval_items.jsonl`: every review task as structured JSON.
- `eval_items.csv`: spreadsheet-friendly view of the review tasks.
- `annotations.template.csv`: easiest file for a non-technical reviewer.
- `annotations.template.jsonl`: JSONL annotation template.
- `eval.sqlite`: local SQLite database for SQL practice and analysis.
- `README.md`: reviewer instructions for that specific packet.
- `eval_manifest.json`: counts and metadata.

By default the packet is capped at 200 items. Use `--max-items 0` to include all
items.

```bash
.venv/bin/tapesplit eval build /path/to/family-videos.tapesplit \
  --force \
  --max-items 0
```

The packet uses TapeSplit's visibility filter. Events and derived records that
are marked as unrelated, non-content, or excluded are omitted from the
family-facing review artifacts.

## Reviewer Workflow

The simple path:

1. Copy `annotations.template.csv` to `annotations.csv`.
2. Open `eval_items.csv` and `annotations.csv` side by side.
3. For each item, fill in `judgment`, `corrected_value`, `notes`, and
   `reviewer`.
4. Use `needs_followup` when the reviewer should ask a parent, sibling, cousin,
   or another family member.
5. Keep or edit `family_followup_question`; it becomes the follow-up queue.

Allowed judgments:

```text
correct
mostly_correct
partial
incorrect
not_sure
needs_followup
skip
```

Scored judgments are `correct`, `mostly_correct`, `partial`, and `incorrect`.
The other judgments keep the item reviewable without pretending the reviewer
knows the answer.

## Score The Packet

After the reviewer creates `annotations.csv`:

```bash
.venv/bin/tapesplit eval score /path/to/family-videos.tapesplit
```

Or pass a specific file:

```bash
.venv/bin/tapesplit eval score /path/to/family-videos.tapesplit \
  --annotations annotations.csv
```

Scoring writes:

- `eval_report.json`: aggregate score by task type.
- `scored_items.jsonl`: joined prediction and annotation records.
- `family_followups.jsonl`: questions to ask family members.
- `eval.sqlite` tables `scored_annotations` and `score_summary`.

The current score is intentionally simple:

```text
correct = 1.0
mostly_correct = 0.75
partial = 0.5
incorrect = 0.0
```

`not_sure`, `needs_followup`, and `skip` are not scored. That matters because
the reviewer should be allowed to say "I don't know" without lowering the model
score.

## SQL Learning Path

Open `eval.sqlite` with DB Browser for SQLite, the `sqlite3` CLI, Datasette, or a
notebook.

Starter queries:

```sql
SELECT * FROM task_counts;

SELECT id, task_type, predicted_label, family_followup_question
FROM followup_queue
ORDER BY task_type, start_s;

SELECT id, task_type, predicted_label, confidence, review_status
FROM low_confidence_queue
LIMIT 25;

SELECT task_type, judgment, COUNT(*) AS n
FROM scored_annotations
GROUP BY task_type, judgment
ORDER BY task_type, n DESC;
```

Good beginner data-science questions:

- Which task type has the lowest score?
- Are low-confidence items actually less accurate?
- Which places create the most follow-up questions?
- Which names or relationships are frequently marked partial or incorrect?
- Which source video produces the most uncertain items?

## Prompting Exercise

For hard rows, use the `prompting_lab` SQLite view. It contains:

- the review prompt
- structured predicted fields
- evidence summary

A reviewer can paste one row into a model and ask:

```text
Given only this evidence, what should a reviewer verify, and what question
should we ask a family member?
```

Then compare the model's proposed follow-up question with the human annotation.
This is useful for learning prompt design and for improving TapeSplit's future
review prompts.

## How This Improves The Product

Each scored packet becomes training data for product decisions:

- event detection accuracy
- album grouping accuracy
- name/alias resolution errors
- place-scope errors such as "home" meaning different homes in different eras
- date mistakes where spoken historical dates were treated as recording dates
- relationship claims that were overconfident
- context edges that were useful but not export-ready

The next step is to convert recurring corrections into reusable project memory:

- reviewed person aliases
- reviewed place aliases and scopes
- confirmed family relationships
- known residences by era
- known travel locations by trip
- examples of unrelated content

That memory should feed future grouping and search, while every inferred claim
still carries evidence and review status.
