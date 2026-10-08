# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately. Do not open a public issue or pull request for them.

1. Open the [Security tab](https://github.com/bindeman/tapesplit/security) of this repository.
2. Choose **Report a vulnerability**. This opens a private security advisory that only you and the maintainer can see. (Direct link: <https://github.com/bindeman/tapesplit/security/advisories/new>.)

Include the commit you tested, the steps you took, what happened, and the impact you expect. A reproduction on a synthetic project (a few JSONL rows with made-up names) is ideal. Do not attach real family media, transcripts, or API keys. If a report seems to need a sample of your data, describe its shape instead.

tapesplit has a single maintainer, so replies are best-effort. Please give a fix time to land before you disclose publicly; the advisory thread is where we will coordinate.

## Supported versions

tapesplit is pre-1.0 and under active development. Only the latest commit on `main` is supported. Fixes land there, and older commits and tags do not get backports.

## How tapesplit is meant to be run

tapesplit is local-first. It reads the video files you point it at, writes a `.tapesplit` project folder, and serves a review UI on your own machine.

- **API keys live only in your local `.env`.** Provider keys (TwelveLabs, Azure OpenAI, Google Maps, Hugging Face) are read from a git-ignored `.env` file in the directory you run `tapesplit` from. `.env.example` lists the variables and contains no values. Vertex Gemini uses your Google application-default credentials instead of a key. `tapesplit doctor` reports whether each provider is configured and never prints a secret.
- **Data leaves your machine only through providers you turn on.** Video, frames, audio, and transcript text go to Vertex Gemini, TwelveLabs, or Azure OpenAI only when you configure that provider and run (or opt in to) the matching stage, and `tapesplit auto --profile local` skips the cloud stages. Place names go to Google Places or OpenStreetMap Nominatim only when you run a `tapesplit geocode` command, and Google also needs `GOOGLE_MAPS_ENABLED=true`, a budget, and `--allow-api`.
- **The review UI is a local development server.** `tapesplit ui` and `npm run dev` start Vite bound to `127.0.0.1`, with a small API in `apps/review-ui/vite.config.ts` that reads project files, streams source video, and records review actions. It has no authentication and is not meant to be exposed to a network. Do not bind it to `0.0.0.0`, forward its port, or put it behind a reverse proxy.

## What is in scope

- Credentials from `.env` or your environment showing up in logs, project outputs, or exported files.
- Path traversal or arbitrary file read or write through project files, review actions, or the review UI's API.
- Command injection through filenames, media metadata, transcripts, or other project data. tapesplit runs `ffmpeg`, `ffprobe`, `exiftool`, and the `tapesplit` CLI itself as subprocesses.
- Code execution when loading a project, an imported transcript or speaker file, or model output.
- Ways for a web page or another local user to reach or drive the review UI despite its loopback binding.

## What is out of scope

- Reaching the review UI after you have deliberately exposed it to a network.
- Vulnerabilities in third-party dependencies or model providers that have no tapesplit-specific impact. Report those upstream; Dependabot tracks version updates here.
- Wrong or unflattering AI guesses about names, dates, or relationships. File those as regular issues.
- Attacks that need physical or administrator access to a machine that already holds the archive.
