"""
Poll SimplifyJobs/Summer2027-Internships for Summer 2027 internships matching
the keyword set in keywords.json, and push new, currently-active matches to a
Discord channel via webhook. Already-posted listings are tracked in
posted_ids.json so re-runs don't spam duplicates.
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.request

import job_store
from check_company_boards import KW, location_ok

LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/"
    "Summer2027-Internships/dev/.github/scripts/listings.json"
)
POSTED_IDS_FILE = os.path.join(os.path.dirname(__file__), "posted_ids.json")
# The repo carries rolling Summer/Fall 2026 postings alongside next season's —
# restrict to the term actually being recruited for.
TARGET_TERM = "Summer 2027"


def fetch_listings():
    with urllib.request.urlopen(LISTINGS_URL, timeout=60) as resp:
        return json.load(resp)


def load_posted_ids():
    if not os.path.exists(POSTED_IDS_FILE):
        return set()
    with open(POSTED_IDS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_posted_ids(ids):
    with open(POSTED_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)
        f.write("\n")


def matches(entry):
    if not entry.get("active", False):
        return False
    if not entry.get("is_visible", True):
        return False
    if TARGET_TERM not in entry.get("terms", []):
        return False
    title = entry.get("title", "")
    company = entry.get("company_name", "")
    if KW["exclude"].search(title):
        return False
    if KW["us_only"] and not location_ok(" ".join(entry.get("locations", []))):
        return False
    # Core tier only: this feed carries hardware, quant-trading-desk and
    # non-technical reqs alongside the software ones. Title only -- unlike the
    # robotics version this was forked from, matching the COMPANY name is no
    # help here: half the core terms ("data", "ai", "search") are words tech
    # companies put in their own names, which would match everything they post.
    return bool(KW["core"].search(title))


def build_embed(entry):
    locations = ", ".join(entry.get("locations", [])) or "Location TBD"
    terms = ", ".join(entry.get("terms", []))
    return {
        "title": f'{entry.get("company_name", "?")} — {entry.get("title", "?")}',
        "url": entry.get("url", ""),
        "color": 0x2ECC71,
        "fields": [
            {"name": "Location", "value": locations, "inline": True},
            {"name": "Term", "value": terms or "N/A", "inline": True},
            {"name": "Sponsorship", "value": entry.get("sponsorship", "Unknown"), "inline": True},
        ],
        "footer": {"text": "SimplifyJobs Summer2027-Internships"},
    }


def post_to_discord(webhook_url, entries):
    # Discord allows up to 10 embeds per message; batch accordingly.
    batch_size = 10
    for i in range(0, len(entries), batch_size):
        batch = entries[i : i + batch_size]
        payload = {
            "content": f"💻 {len(batch)} new CS internship posting(s) found:",
            "embeds": [build_embed(e) for e in batch],
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                # Discord's edge rejects the default Python-urllib UA with a 403.
                "User-Agent": "cs-job-alert (https://github.com/kaipowei/cs-job-alert, 1.0)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        time.sleep(1)  # stay well under Discord's webhook rate limit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true",
                    help="record current matches as the baseline without alerting")
    args = ap.parse_args()

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url and not args.seed:
        print("DISCORD_WEBHOOK_URL not set", file=sys.stderr)
        sys.exit(1)

    listings = fetch_listings()
    posted_ids = load_posted_ids()

    new_matches = [
        e for e in listings if matches(e) and e.get("id") not in posted_ids
    ]

    # Unlike the robotics version, the core tier barely narrows this feed --
    # it's a SWE-internship aggregator, so a cold start matches ~900 postings
    # at once. Seeding adopts them silently instead of firing 900 webhooks.
    if args.seed:
        posted_ids.update(e["id"] for e in new_matches)
        save_posted_ids(posted_ids)
        log_jobs(new_matches)
        print(f"Seeded baseline with {len(new_matches)} postings (nothing sent).")
        return

    if not new_matches:
        print("No new CS postings.")
        return

    # Oldest first so the Discord channel reads chronologically.
    new_matches.sort(key=lambda e: e.get("date_posted", 0))

    post_to_discord(webhook_url, new_matches)

    posted_ids.update(e["id"] for e in new_matches)
    save_posted_ids(posted_ids)
    log_jobs(new_matches)
    print(f"Posted {len(new_matches)} new CS internship(s).")


def log_jobs(entries):
    """
    Append full metadata to data/job_log.jsonl for the description fetcher,
    tagged with a CS sub-category (see job_store.classify_job) so a
    later analysis can tell AV apart from humanoid/drone/warehouse/etc.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    records = [
        {
            "uid": f"simplify:{e['id']}",
            "source": "simplify",
            "company": e.get("company_name", ""),
            "title": e.get("title", ""),
            "url": e.get("url", ""),
            "location": ", ".join(e.get("locations", [])),
            "terms": e.get("terms", []),
            "sponsorship": e.get("sponsorship", ""),
            "category": job_store.classify_job(e.get("company_name", ""), e.get("title", "")),
            "first_logged": now,
            "alerted_to_discord": True,
        }
        for e in entries
    ]
    n = job_store.append_job_log(records)
    if n:
        print(f"Logged {n} job record(s) to data/job_log.jsonl.")


if __name__ == "__main__":
    main()
