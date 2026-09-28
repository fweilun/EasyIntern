"""
One-time (well, re-runnable) backfill: reconstruct full metadata (company,
title, url, location) for every uid that was already alerted before
job_store.py existed -- i.e. everything currently sitting in
seen_company_jobs.json / posted_ids.json as a bare id.

Company-board postings are recovered by re-polling every company's board and
matching on uid; only still-open postings can be recovered this way, since
closed reqs usually drop out of the ATS's list endpoint entirely.

SimplifyJobs listings are recovered from the full (unfiltered) listings.json
dump by id -- that repo keeps closed entries with active=false rather than
deleting them, so this recovers essentially all of them regardless of
whether they're still open.

Every recovered job is tagged with a CS sub-category (see
job_store.classify_job), matching what the two watchers now do going forward.

Anything that can't be recovered (closed posting dropped from the board) is
skipped rather than logged as a stub, since without a title there's nothing
to categorize or show in the report either.
"""

import datetime
import json
from concurrent.futures import ThreadPoolExecutor

import check_company_boards as boards
import check_simplify_jobs as simplify
import job_store


def backfill_boards():
    already = job_store.load_logged_uids()
    seen = boards.load_seen()
    missing = {u for u in seen if u not in already}
    if not missing:
        print("Company boards: nothing to backfill.")
        return

    with open(boards.ATS_CONFIG_FILE, "r", encoding="utf-8") as f:
        companies = json.load(f)

    # Re-poll every company's FULL board (not just is_wanted() matches) so we
    # can recover a uid even if it would no longer pass today's keyword filter.
    def poll_all(item):
        company, cfg = item
        url = boards.ENDPOINTS[cfg["platform"]].format(slug=cfg["slug"])
        try:
            data = boards.fetch(url)
        except Exception:
            return []
        out = []
        for raw in boards.extract(cfg["platform"], data):
            try:
                out.append(boards.normalize(company, cfg["platform"], raw))
            except Exception:
                continue
        return out

    live = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(poll_all, companies.items()):
            for j in res:
                live[j["uid"]] = j

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    records = []
    recovered, lost = 0, 0
    for uid in missing:
        j = live.get(uid)
        if j:
            records.append({
                "uid": uid, "source": "board", "company": j["company"],
                "title": j["title"], "url": j["url"], "location": j["location"],
                "category": job_store.classify_job(j["company"], j["title"]),
                "first_logged": now, "alerted_to_discord": True,
                "backfilled": True,
            })
            recovered += 1
        else:
            lost += 1
    job_store.append_job_log(records)
    print(f"Company boards: recovered {recovered}, lost {lost} (of {len(missing)} missing).")


def backfill_simplify():
    already = job_store.load_logged_uids()
    posted = simplify.load_posted_ids()
    missing = {i for i in posted if f"simplify:{i}" not in already}
    if not missing:
        print("SimplifyJobs: nothing to backfill.")
        return

    listings = simplify.fetch_listings()
    by_id = {e["id"]: e for e in listings}

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    records = []
    recovered, lost = 0, 0
    for i in missing:
        e = by_id.get(i)
        if not e:
            lost += 1
            continue
        company, title = e.get("company_name", ""), e.get("title", "")
        records.append({
            "uid": f"simplify:{i}", "source": "simplify",
            "company": company, "title": title,
            "url": e.get("url", ""), "location": ", ".join(e.get("locations", [])),
            "terms": e.get("terms", []), "sponsorship": e.get("sponsorship", ""),
            "category": job_store.classify_job(company, title),
            "first_logged": now, "alerted_to_discord": True, "backfilled": True,
        })
        recovered += 1
    job_store.append_job_log(records)
    print(f"SimplifyJobs: recovered {recovered}, lost {lost} (of {len(missing)} missing).")


if __name__ == "__main__":
    backfill_boards()
    backfill_simplify()
