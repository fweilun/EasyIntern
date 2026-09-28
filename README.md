# cs-job-alert

Watches for Summer 2027 software-engineering internships, pushes new postings
to a Discord channel, and builds a monthly skill-gap report from everything
it's caught. Runs on GitHub Actions — no server needed.

Forked from a robotics/AV version of the same pipeline; the code is unchanged
in shape, the targeting is not (see **What changed from the robotics version**).

## Discord alerts

Two watchers feed the same channel, because they fail in opposite directions:

| Watcher | Source | Catches | Misses | Cadence |
|---|---|---|---|---|
| `check_company_boards.py` | 47 company career boards, polled directly through their ATS's public JSON API | New reqs the day they go up, before any aggregator indexes them | Anyone not on greenhouse/lever/ashby/smartrecruiters — which is most of big tech (Google, Meta, Amazon, Microsoft, Apple all run their own) | every 30 min |
| `check_simplify_jobs.py` | [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) | The big-tech shops the boards watcher can't reach | A day or two of lag; only what contributors submit | every 30 min |

Both dedupe against their own state file (`seen_company_jobs.json`,
`posted_ids.json`), so a posting alerts once.

### Volume

This is the one thing that behaves differently from the robotics version. There,
the core keyword tier cut a 14k-listing aggregator down to a handful of robotics
reqs. Here the aggregator *is* the target — it's a SWE-internship list — so the
core tier barely narrows it: **898 Summer 2027 postings matched at seed time,
and 30–50 new ones land per day in peak season.** The company-board watcher is
the quiet one (87 postings at seed).

If that's too loud, the lever to pull is `check_simplify_jobs.matches()` — e.g.
restrict it to a company allowlist instead of running the keyword tier over the
whole feed.

### Tuning what gets alerted

Edit **`keywords.json`** — both watchers read it. Two tiers, deliberately:

- **`core_cs`** — tight, software-specific terms. Used *alone* against
  SimplifyJobs, which also carries hardware, quant-trading-desk and
  non-technical reqs. Matched against the job title only: unlike the robotics
  version, matching the *company* name is no help here, because half the core
  terms (`data`, `ai`, `search`) are words tech companies put in their own names.
- **`broad_engineering`** — general terms, added *on top of* core but only for
  the curated company boards. Every company in `ats_config.json` is already a
  software company, so any engineering intern there is worth seeing.
- **`exclude`** — dropped from both even on a keyword hit. On a CS list this
  does more work than the tiers do: tech-company boards are full of Sales
  Engineer / Solutions Engineer / Support Engineer reqs that hit the broad
  tier's bare `engineer` but aren't software roles. Same for People Analytics,
  User Research and Account Development Representative, which hit `analytics`,
  `research` and `development`.
- **`us_canada_only`** — drops postings whose location is clearly overseas.

Terms match as whole words, case-insensitive, so `ml` won't hit `html`. Keep
bare generic words out of `core_cs` — it runs against an aggregator with no
company-level filtering, so `engineer`, `systems` or a bare `ai` drag in
hardware, civil and sales reqs. Those belong in `broad_engineering`, or as a
qualified compound in core (`ai engineer`, `systems software`).

Scope is Summer 2027 specifically. `check_company_boards.py`'s `OFF_SEASON_RE`
drops Spring/Winter/Fall titles even when 2027 also appears — Notion and
Databricks both post paired Winter/Summer 2027 reqs.

### Which companies are watched

`ats_config.json` maps each company to its ATS platform and slug — 47 entries,
each verified by actually polling the API and checking the returned jobs belong
to that company. Several obvious slugs are squatted or wrong, so don't add one
without checking.

To add a company, find its board URL and add an entry:

```json
"Company Name": { "platform": "greenhouse", "slug": "theirslug" }
```

`platform` is one of `greenhouse`, `lever`, `ashby`, `smartrecruiters` —
readable off the careers URL (`job-boards.greenhouse.io/doordashusa` →
greenhouse, slug `doordashusa`). Also add it to `company_categories` in
`keywords.json`.

## Job-log write-back (for the monthly report)

Every posting either watcher alerts also gets appended to `data/job_log.jsonl`
(full metadata: company, title, url, location) — the opaque ids in
`seen_company_jobs.json`/`posted_ids.json` alone weren't enough to build a
report from later. Every logged record is tagged with a sub-category so the
report can compare skill demand across sub-fields instead of lumping everything
together.

> **Still to do:** the sub-categories in `job_store.CATEGORY_ORDER` and
> `keywords.json` are still the robotics ones (`av`, `humanoid`, `drone_aerial`
> …), so the 985 seeded records carry meaningless tags. Alerting is unaffected —
> categories only feed the monthly report. To redo them: set the new category
> list, then wipe `data/job_log.jsonl` and re-run both watchers with `--seed`.

Classification (`job_store.classify_job()`) checks `company_categories` in
`keywords.json` first (a manually curated map of every `ats_config.json`
company), then falls back to `category_terms` keyword matching for SimplifyJobs
entries the company map doesn't cover.

`backfill_job_log.py` reconstructs full metadata for postings alerted before
this existed: company-board uids by re-polling live boards (only still-open
postings are recoverable that way), SimplifyJobs ids from the full unfiltered
`listings.json` dump (which keeps closed entries rather than deleting them, so
nearly 100% recoverable there).

## Job descriptions

`fetch_descriptions.py` fills `data/descriptions.json` with full JD text for
everything in `job_log.jsonl`, reading structured data wherever possible
instead of scraping rendered HTML:

- **Greenhouse** — per-job detail endpoint's `content` field
- **Lever / Ashby** — already embedded in the board's list response
- **SmartRecruiters** — per-job detail endpoint's `jobAd.sections`
- **Anything else** (SimplifyJobs' arbitrary external URLs) — first checks if
  the URL happens to be an Ashby or Workday posting outside `ats_config.json`
  (reads their structured JSON/CXS API the same way), then falls back to a
  generic HTML fetch + tag-strip, which fails on JS-rendered pages (most
  Workday tenants, bot-blocked career sites).

Runs daily (`.github/workflows/fetch-descriptions.yml`) rather than on the
30-min alert cadence, since it hits many more external hosts. Re-run failed
fetches with `python fetch_descriptions.py --retry-failed`. `--limit N` caps a
run; `--force` re-fetches everything.

## Monthly skill-gap report

`reports/Robotics_Job_Market_Report.docx` is the old robotics report, kept as an
artifact. `reports/scripts/build_report.js` still carries its hand-written
robotics prose and category labels — **it has not been converted to CS yet.**

The refresh procedure is otherwise unchanged:

1. `python fetch_descriptions.py --retry-failed` — make sure descriptions are current
2. `python build_appendix_data.py` — refresh `data/report_appendix.json`
3. Re-read the accumulated postings (grouped by category) against the current CV
   and rewrite the qualitative sections (Executive Summary, what each sub-field
   is hiring for, the skill-gap table, learning priorities) — a synthesis task,
   done by reading the actual job text each time, not scripted
4. `node reports/scripts/build_report.js` — renders the .docx

`reports/scripts/build_report.js` uses `docx` (npm); `cd reports/scripts &&
npm install` if `node_modules` isn't present (it's gitignored).

## Running it locally

The system Python is externally managed (PEP 668), so use the venv:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`.env` holds `DISCORD_WEBHOOK_URL` and is gitignored. Then:

```bash
.venv/bin/python check_company_boards.py      # alerts on anything new
.venv/bin/python check_simplify_jobs.py
```

## Setup (GitHub Actions)

1. Discord channel → Integrations → Webhooks → New Webhook → copy the URL.
2. Repo → Settings → Secrets and variables → Actions → new secret
   `DISCORD_WEBHOOK_URL`.
3. Repo → Settings → Actions → General → Workflow permissions →
   **Read and write** (both workflows commit their state files back).

## Seeding

Both watchers alert only on postings that appear *after* their baseline. The
baselines in this repo are seeded (87 board + 898 SimplifyJobs postings), so
nothing needs doing.

After widening `keywords.json`, newly-matching existing postings would all fire
at once. To adopt them silently instead, both watchers take `--seed`:

```bash
.venv/bin/python check_company_boards.py --seed
.venv/bin/python check_simplify_jobs.py --seed
```

## State files

- `seen_company_jobs.json` / `posted_ids.json` — dedupe state for the two
  Discord watchers (bare ids only)
- `data/job_log.jsonl` — append-only, full metadata for every alerted posting,
  categorized
- `data/descriptions.json` — fetched JD text, keyed by the same uid as the log
- `data/report_appendix.json` — regenerable flat export of the above two, for
  `build_report.js`

Both `.github/workflows/*.yml` commit their outputs back after each run.
Deleting a dedupe state file replays that source's entire current set on the
next run; `data/job_log.jsonl` and `descriptions.json` are meant to keep
growing and shouldn't normally be deleted.

## What changed from the robotics version

| | Robotics | CS |
|---|---|---|
| `ats_config.json` | 44 robotics/AV companies | 47 software companies, all re-verified |
| `keywords.json` | `core_robotics` | `core_cs`; broad and exclude tiers rewritten |
| SimplifyJobs matching | title **or** company name | title only |
| Off-season filter | Spring | Spring / Winter / Fall |
| `check_robotics_jobs.py` | — | renamed `check_simplify_jobs.py`, gained `--seed` |
| Volume | a handful/day | 30–50/day from the aggregator |

Two pre-existing bugs fixed along the way: the alert workflow's `git add` never
included `data/job_log.jsonl`, so the log was never committed back; and `.env`
(which holds the live webhook) wasn't in `.gitignore`.
