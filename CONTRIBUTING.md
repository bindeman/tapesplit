# Contributing to TapeSplit

Thanks for helping. TapeSplit turns digitized VHS and home-video files into a reviewable, searchable family archive: a Python CLI in `src/tapesplit` and a React review UI in `apps/review-ui`. Small, focused pull requests are the easiest to review. For anything bigger than a bug fix, open an issue first so we can agree on the approach before you spend the time.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Report security problems privately as described in [SECURITY.md](SECURITY.md), not in a public issue.

## Licensing of contributions

TapeSplit is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE): free for personal and other noncommercial use, with commercial rights reserved to the author. So that a contribution can ship in every version of TapeSplit, including commercial ones:

- You confirm that you wrote the contribution, or otherwise have the right to submit it.
- You keep the copyright in your contribution.
- By submitting it (a pull request, a patch, or code in an issue), you grant Phillip Bindeman a perpetual, worldwide, non-exclusive, royalty-free, irrevocable license to use, copy, modify, distribute, sublicense and relicense it under any terms, including commercial ones.

If you can't agree to these terms, open an issue that describes the change instead of sending code.

## Set up

You need Python 3.11 or newer (CI runs 3.11 and 3.12), ffmpeg (it provides `ffprobe`), and Node 22 for the review UI.

```bash
git clone https://github.com/bindeman/tapesplit.git
cd tapesplit

python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[local-ai,vision,visual-ai]' pytest

brew install ffmpeg            # macOS
sudo apt-get install ffmpeg    # Debian / Ubuntu
```

`local-ai` and `visual-ai` pull in PyTorch through Whisper and sentence-transformers, so the first install is a large download. The `macos` extra (Apple Vision OCR and face detection) only installs on macOS; add it there with `'.[local-ai,vision,visual-ai,macos]'`. Install `face-ai` (InsightFace), `speaker-ai` (pyannote, speechbrain), and `twelvelabs` only when you work on those backends. [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md) lists what each extra enables.

`.venv/bin/tapesplit doctor` shows which stages can run on your machine.

## Run the tests

```bash
.venv/bin/python -m pytest -q
```

The suite takes seconds and needs no network, cloud credentials, GPU, or real media. Tests build tiny project directories under `tmp_path` from a few JSONL rows (see `tests/test_review_actions.py`) and fake provider calls with `monkeypatch` (see `tests/test_azure_diarize.py`).

CI installs only the base package (`pip install -e . pytest`), so a test that needs an optional package must skip itself rather than fail. Put `pytest.importorskip("numpy")` (or `sklearn`, `cv2`, ...) on the first line of the test, or at module level when every test in the file needs it. Do not add an optional package to the base `dependencies`. Before opening a PR, you can run the suite in a throwaway venv with only `pip install -e . pytest` to see what CI sees.

## Build the review UI

```bash
cd apps/review-ui
npm ci
npm run build                                              # tsc type-check + Vite production build
TAPESPLIT_PROJECT=/path/to/project.tapesplit npm run dev   # dev server on 127.0.0.1
```

`tapesplit ui /path/to/project.tapesplit` starts the same dev server from the repo root. UI work follows [docs/DESIGN.md](docs/DESIGN.md): every color, size, weight, radius, duration, and shadow comes from the tokens declared at `:root` in `apps/review-ui/src/styles.css`. A value that is not a token is a defect.

## House rules

These come from "Development Rules" in [docs/HANDOFF.md](docs/HANDOFF.md).

1. **Preserve user and media data.** Do not delete generated project artifacts unless the task is explicitly regeneration or cleanup.
2. **Never commit secrets, media, or project outputs.** That means `.env`, API tokens, `cost_rates.json`, video files (`*.mp4`, `*.mov`, ...), and `*.tapesplit/` project directories. `.gitignore` already blocks them; do not force-add.
3. **Keep providers swappable.** Prefer local, deterministic modules for VHS-specific logic. Gemini, TwelveLabs, and Azure sit behind adapters, and a stage whose provider is missing should be skipped with a reason, not fail the run.
4. **Keep corrections durable and replayable.** Reviewer decisions live in `corrections.jsonl` and must survive a rebuild (`tapesplit auto` and `rebuild` replay them). A change that stops a correction from replaying is a bug.
5. **Add tests for anything that affects review decisions, entity resolution, relationship inference, places, or export-visible metadata.**

## Privacy: tests, docs, and issues

TapeSplit exists to process people's family tapes, so nothing from a real archive belongs in this repository.

- Use fictional people and places in tests, docs, screenshots, and issue reports.
- Never paste real transcripts, names, faces, or frames from your own tapes into a test, doc, issue, or PR. To show a bug, write a few synthetic JSONL rows or describe the shape of the data.
- Facts that are true only of one archive (for example who usually held the camera) go in `<project>/project_hints.json`, inside the git-ignored project directory, never in source. See `src/tapesplit/project_hints.py`.

## Pull requests

Branch from `main` and open the PR against `main`. Say what changed for the user and how you checked it. Before you ask for review:

- [ ] `python -m pytest -q` passes.
- [ ] `npm run build` passes in `apps/review-ui` if you touched the UI, and new styles use the design tokens.
- [ ] Changed behavior has tests, especially anything covered by house rule 5.
- [ ] The diff contains no secrets, media, project outputs, or real personal data, including in screenshots.
- [ ] Docs, `--help` text, and `.env.example` are updated if you changed a command or a setting.

The pull request template carries the same checklist.

## Finding your way around

Start with [docs/HANDOFF.md](docs/HANDOFF.md). It is the first thing to read: product goal, architecture, current state, and the development rules above. Then read [docs/AUTOMATION.md](docs/AUTOMATION.md) for the `tapesplit auto` stage graph, resume state, graceful degradation, and the auto-accept policy. The other design docs sit beside them in `docs/` (entity resolution, relationship inference, retrieval, visualization backend).

A few landmarks in the code:

- `src/tapesplit/cli.py` defines every command.
- `src/tapesplit/auto.py` is the stage graph behind `tapesplit auto`.
- `src/tapesplit/review_actions.py` applies and replays reviewer corrections.
- `apps/review-ui/vite.config.ts` holds the local API the review UI talks to.
