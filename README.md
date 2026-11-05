# Lecture Companion

A private, local, single-user web app that turns course lectures into detailed study notes and interview preparation. Built for the Interview Kickstart Forward Deployed Engineer course, with ADHD-friendly reading features.

**Release 1 (this version): Otter.ai transcript import → review → detailed notes → edit → export (PDF/DOCX/Markdown/HTML).**
Releases 2 (video/audio processing) and 3 (live Zoom) are planned; see *Roadmap* below.

## Start

Requirements: macOS/Linux, [uv](https://docs.astral.sh/uv/), Google Chrome (for PDF export; HTML/DOCX/Markdown work without it).

```bash
./run.sh                 # creates .venv on first run, then serves http://127.0.0.1:8765
```

Then open http://127.0.0.1:8765 → **Settings** → paste your Anthropic (or OpenAI) API key. The key is written to `data/secrets.env` (mode 0600), never to source control or the browser. Click **Test connection**.

Environment variables override settings: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LC_PROVIDER`, `LC_MODEL`, `LC_MAX_CONCURRENT_JOBS`, `LC_DATA_DIR`, etc. (see `app/config.py`).

To explore the UI without a key, load the synthetic fixtures with the offline mock provider:

```bash
LC_PROVIDER=mock .venv/bin/python scripts/load_fixtures.py && LC_PROVIDER=mock ./run.sh
```

## Workflow

1. **Import** (`/import`): upload one or many Otter exports (TXT, SRT, VTT, DOCX, text PDF) or paste a transcript. Optionally attach slides (PDF), screenshots, or a recording.
2. **Review**: confirm title / course / week / date per lecture, see the source assessment (full transcript vs Otter summary, word count, timestamps, speakers), preview the parsed content, then **Confirm and generate**.
3. **Generate**: a durable background job (2 concurrent by default) runs the pipeline: image captioning → per-chunk content inventory → ordered concept outline → one detailed entry per concept → papers/resources consolidation + online verification → overview/summary/glossary/practice → coverage report.
4. **Study** (`/lectures/<id>`): overview, expandable concepts, interview tab, practice tab (hidden answers), source tab (full searchable transcript, screenshots), coverage tab. Focus mode (`f`), study timer, saved reading position, bookmarks/notes/review markers (click any concept or bullet), read/practiced/reviewed states.
5. **Edit** (`Edit & regenerate` tab): form-based editing per section; edited fields are tracked and preserved when sections are regenerated. Regenerate selected sections cheaply (intermediate results are reused).
6. **Export**: PDF, DOCX, Markdown, HTML, each in *detailed* or *concise* mode, all rendered from the same notes model with images embedded.

## Notes structure

Every lecture gets: A. overview (objectives, prerequisites, intro, 10+ takeaways) · B. concepts in lecture order, each with plain-English explanation, 10+ teaching bullets, definitions, why/when, analogy + limits, steps, examples/code, assumptions/tradeoffs/limitations/mistakes, connections, deeper explanation, FDE applications, demos/troubleshooting/corrections/Q&A, interview questions (30–60 s answer, deeper answer, follow-ups, conditions; marked generated vs instructor) · C. glossary · D. papers mentioned (with arXiv/Crossref verification; uncertain matches flagged; additional reading kept separate) · E. websites/resources (URL verification) · F. summary + "next topic announced" vs "suggested next steps" · G. practice (recall with hidden answers, exercises, progression) · H. source material · coverage report.

Every claim carries provenance: **Taught** (with transcript refs such as `P12@1:02:03`, or `P12` when the export has no timestamps), **Additional explanation**, or **Needs verification**. Nothing is invented: no timestamps, titles, authors, or URLs beyond what the source or a verified lookup supports.

## Verified in this release

Automated (`.venv/bin/python -m pytest`, 15 tests) and a scripted HTTP walk-through against the running server, using clearly labelled synthetic fixtures and the offline mock provider:

- Parsing of Otter TXT (speakers + timestamps), SRT, VTT, untimed text (paragraph citations), Otter summary exports (detected and flagged as insufficient), DOCX and text PDF (embedded images extracted and associated with paragraph/page, never a timestamp).
- Multi-file import with attachments, review/confirm, two concurrent jobs, long-transcript chunking (3 chunks), coverage report with gap detection, missing-timestamp citations, optional slides/screenshots, edit → regenerate preserving edits, search (FTS5), interview and practice views, annotations, reading position / resume.
- Restart recovery: the server was SIGKILLed mid-job; on restart the job was re-queued and completed from its checkpoints.
- Exports in all four formats and both modes; the PDF (45 pages) was rendered to images and checked for overflow/empty pages (none); DOCX structure checked (headings, lists, 6 embedded images, no leftover Markdown markup).
- Provider layer: Anthropic request shape validated against the live API (rejected only for the dummy key); OpenAI path exercised with a real `gpt-5` run on the short fixture (see measured results below).

## Measured results

See the **Jobs** page for per-call tokens, cost, and seconds on your own lectures. Fixture measurements are in the delivery notes; real lecture timings depend on the provider and model and are recorded per job, so no fixed speed is promised.

Default model: Anthropic `claude-opus-5-5` (list price $4 / $20 per million tokens). A 4-hour lecture (~35k words) is roughly 50k input tokens per pass; with the inventory pass plus one call per concept the expected total is on the order of 300–500k input and 100–150k output tokens, i.e. a few dollars per lecture. Spending limits per lecture and in total are enforced in Settings; a job that hits the limit stops at a checkpoint and resumes when the limit is raised.

## Layout

```
app/config.py       settings + secrets (data/config.json, data/secrets.env)
app/db.py           SQLite models, FTS5 search index
app/parsers/        transcript parsers (TXT/SRT/VTT/DOCX/PDF)
app/importer.py     lecture creation, attachments (PDF slides → page images)
app/notes/schema.py the single structured notes model (pydantic)
app/notes/pipeline.py  job handlers: generate_notes, regenerate_sections
app/notes/prompts.py   prompts
app/notes/links.py     arXiv/Crossref/URL verification
app/providers/      anthropic | openai | openai_compatible | mock
app/jobs/runner.py  durable job queue (SQLite), checkpoints, retries, recovery
app/exporters/      Markdown → HTML → PDF (headless Chrome); DOCX (python-docx)
app/main.py         FastAPI routes;  app/web/  templates, CSS, JS
data/               your lectures (gitignored): lectures/<id>/{sources,images,pipeline,exports}
tests/              pytest suite + synthetic fixtures (scripts/make_fixtures.py)
```

## Privacy and deletion

Everything stays on this machine except the transcript text sent to the configured AI provider and the paper/URL lookups (arXiv, Crossref, and the URLs themselves) when link verification is on. Each lecture's files are isolated in their own directory. The Source tab deletes originals, generated outputs, or the whole lecture.

## Roadmap

- **Release 2, recordings**: ffmpeg audio extraction, configurable transcription provider with word timestamps, scene/diff/text-based screenshot capture with deduplication and manual capture, parallel audio/visual processing. ffmpeg 9 is already installed on this machine. Interview Kickstart recording import will be investigated only through permitted downloads.
- **Release 3, live Zoom**: investigate supported capture (Zoom app/SDK permissions, macOS screen/audio capture), Sunday 9:00–13:30 schedule in the configured timezone, provisional notes during class, reconciliation afterwards.
- Otter auto-sync: Otter has no documented public API for personal accounts; manual export stays the supported path until that changes.
- PPTX slide import and OCR for scanned PDFs.
