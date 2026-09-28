"""
Watch the career boards of the software companies in ats_config.json and
alert on newly-posted internships — the goal being to see a Summer 2027 req
the same day it goes up, not weeks later once an aggregator picks it up.

Each company is polled through its ATS's public JSON API (Greenhouse, Lever,
Ashby, SmartRecruiters). Postings already seen are recorded in
seen_company_jobs.json so only genuinely new ones alert.

First use should be `python check_company_boards.py --seed`, which records
everything currently posted WITHOUT sending it to Discord. After that, every
run reports only what appeared since.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request
import dotenv
from concurrent.futures import ThreadPoolExecutor
import job_store

HERE = os.path.dirname(os.path.abspath(__file__))
ATS_CONFIG_FILE = os.path.join(HERE, "ats_config.json")
SEEN_FILE = os.path.join(HERE, "seen_company_jobs.json")

UA = "cs-job-alert/1.0 (+https://github.com/kaipowei/cs-job-alert)"

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
}

INTERN_RE = re.compile(r"\b(intern|internship|co-?op)\b", re.IGNORECASE)
# A req naming a season we've already passed is not what we're watching for.
STALE_YEAR_RE = re.compile(r"\b(2024|2025|2026)\b")
TARGET_YEAR_RE = re.compile(r"\b2027\b")
# Scope is Summer 2027 only -- a title naming another season doesn't count even
# if it also says 2027. Notion and Databricks both post paired Winter/Summer
# 2027 reqs, so Winter matters as much as Spring here.
OFF_SEASON_RE = re.compile(r"\b(spring|winter|fall|autumn)\b", re.IGNORECASE)

# Eric is US-based (UMich), and these boards carry the companies' overseas
# intern classes too. Match on what to REJECT rather than what to accept:
# plenty of US postings list a bare city ("Sunnyvale") that no allowlist of
# state names would catch, and dropping those would lose prime targets.
NON_US_RE = re.compile(
    r"\b(Germany|Deutschland|Hannover|Hanover|Frankfurt|Berlin|Munich|München"
    r"|France|Paris|Toulouse|Spain|Madrid|Barcelona|Italy|Milan|Rome"
    r"|Netherlands|Amsterdam|Belgium|Sweden|Stockholm|Gothenburg|Norway|Oslo"
    r"|Denmark|Copenhagen|Finland|Helsinki|Poland|Warsaw|Kraków|Czech|Prague"
    r"|Romania|Bucharest|Timișoara|Hungary|Budapest|Portugal|Lisbon|Austria|Vienna"
    r"|Switzerland|Zurich|Geneva|Ireland|Dublin"
    r"|United Kingdom|\bUK\b|England|London|Cambridge, UK|Oxford|Bristol"
    r"|India|Bangalore|Bengaluru|Hyderabad|Pune|Chennai|Mumbai|Delhi|Gurgaon"
    r"|China|Shanghai|Beijing|Shenzhen|Guangzhou|Hong Kong"
    r"|Japan|Tokyo|Osaka|Yokohama|Korea|Seoul"
    r"|Singapore|Malaysia|Selangor|Kuala Lumpur|Petaling Jaya"
    r"|Thailand|Bangkok|Rayong|Vietnam|Hanoi|Philippines|Manila|Indonesia|Jakarta"
    r"|Taiwan|Taipei|Australia|Sydney|Melbourne|New Zealand"
    r"|Mexico|Querétaro|Guadalajara|Monterrey|Brazil|Brasil|São Paulo"
    r"|Argentina|Chile|Santiago|Colombia|Bogotá"
    r"|Israel|Tel Aviv|Jerusalem|Haifa|Turkey|Istanbul"
    r"|South Africa|Egypt|Cairo|Morocco|Tunisia|Nigeria|Kenya|Ghana|Rwanda"
    r"|Slovakia|Bratislava|Bulgaria|Sofia|Serbia|Belgrade|Ukraine|Greece|Athens)\b",
    re.IGNORECASE,
)


def location_ok(location):
    """US/Canada/remote/unknown pass; anything clearly overseas is dropped."""
    if not location:
        return True
    return not NON_US_RE.search(location)


def _kw_regex(terms):
    """Whole-word alternation so 'ml' can't match inside 'html'."""
    return re.compile(
        r"(?<!\w)(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")(?!\w)",
        re.IGNORECASE,
    )


def load_keywords(path=None):
    """
    Returns both tiers. `core` is the software-specific set, safe to run against
    a general aggregator; `broad` adds general-engineering terms and is only
    appropriate for the pre-curated software-company boards in ats_config.json.
    """
    path = path or os.path.join(HERE, "keywords.json")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    core = cfg["core_cs"]
    return {
        "core": _kw_regex(core),
        "broad": _kw_regex(core + cfg["broad_engineering"]),
        "exclude": _kw_regex(cfg["exclude"]),
        "us_only": cfg.get("us_canada_only", True),
    }


KW = load_keywords()


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def normalize(company, platform, raw):
    """Flatten one ATS's job object into a common shape."""
    if platform == "greenhouse":
        return {
            "uid": f"gh:{raw['id']}",
            "company": company,
            "title": raw.get("title", ""),
            "url": raw.get("absolute_url", ""),
            "location": (raw.get("location") or {}).get("name", ""),
        }
    if platform == "lever":
        cats = raw.get("categories") or {}
        return {
            "uid": f"lv:{raw['id']}",
            "company": company,
            "title": raw.get("text", ""),
            "url": raw.get("hostedUrl", ""),
            "location": cats.get("location", ""),
        }
    if platform == "ashby":
        return {
            "uid": f"ab:{raw['id']}",
            "company": company,
            "title": raw.get("title", ""),
            "url": raw.get("jobUrl", ""),
            "location": raw.get("location", ""),
        }
    if platform == "smartrecruiters":
        loc = raw.get("location") or {}
        city = ", ".join(x for x in [loc.get("city"), loc.get("region")] if x)
        return {
            "uid": f"sr:{raw['id']}",
            "company": company,
            "title": raw.get("name", ""),
            "url": f"https://jobs.smartrecruiters.com/{raw.get('id', '')}",
            "location": city,
        }
    raise ValueError(platform)


def extract(platform, data):
    if platform == "greenhouse":
        return data.get("jobs", [])
    if platform == "lever":
        return data if isinstance(data, list) else []
    if platform == "ashby":
        return data.get("jobs", [])
    if platform == "smartrecruiters":
        return data.get("content", [])
    return []


def is_wanted(job):
    title = job["title"]
    if not INTERN_RE.search(title):
        return False
    if KW["exclude"].search(title):
        return False
    if OFF_SEASON_RE.search(title):
        return False
    # Broad tier: every company on these boards is already a software company.
    if not KW["broad"].search(title):
        return False
    if KW["us_only"] and not location_ok(job["location"]):
        return False
    # Explicitly-2027 titles always qualify; otherwise accept anything that
    # doesn't name an older season, since most reqs omit the year entirely.
    if TARGET_YEAR_RE.search(title):
        return True
    return not STALE_YEAR_RE.search(title)


def poll_company(item):
    company, cfg = item
    url = ENDPOINTS[cfg["platform"]].format(slug=cfg["slug"])
    try:
        data = fetch(url)
    except Exception as e:
        print(f"  !! {company}: {e}", file=sys.stderr)
        return []
    out = []
    for raw in extract(cfg["platform"], data):
        try:
            job = normalize(company, cfg["platform"], raw)
        except Exception:
            continue
        if is_wanted(job):
            out.append(job)
    return out


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(uids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(uids), f, indent=2)
        f.write("\n")


def build_embed(job):
    is_2027 = bool(TARGET_YEAR_RE.search(job["title"]))
    return {
        "title": f'{job["company"]} — {job["title"]}'[:250],
        "url": job["url"],
        # Highlight an explicit 2027 req; everything else is a normal alert.
        "color": 0xF1C40F if is_2027 else 0x3498DB,
        "fields": [
            {"name": "Location", "value": job["location"] or "N/A", "inline": True},
            {"name": "Source", "value": "Company career page", "inline": True},
        ],
        "footer": {"text": "🎯 Explicit 2027 posting" if is_2027 else "New internship posting"},
    }


def post_to_discord(webhook_url, jobs):
    for i in range(0, len(jobs), 10):
        batch = jobs[i : i + 10]
        payload = {
            "content": f"🚨 {len(batch)} new internship posting(s) on tracked software career pages:",
            "embeds": [build_embed(j) for j in batch],
        }
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true",
                    help="record current postings as the baseline without alerting")
    args = ap.parse_args()

    with open(ATS_CONFIG_FILE, "r", encoding="utf-8") as f:
        companies = json.load(f)

    jobs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(poll_company, companies.items()):
            jobs.extend(res)

    seen = load_seen()
    new = [j for j in jobs if j["uid"] not in seen]
    print(f"Polled {len(companies)} companies: {len(jobs)} intern postings, {len(new)} new.")

    if args.seed:
        save_seen(seen | {j["uid"] for j in jobs})
        log_jobs(jobs, alerted=False)
        print(f"Seeded baseline with {len(jobs)} postings (nothing sent).")
        return

    if not new:
        return
    
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL not set", file=sys.stderr)
        sys.exit(1)

    new.sort(key=lambda j: (not TARGET_YEAR_RE.search(j["title"]), j["company"]))
    post_to_discord(webhook_url, new)
    save_seen(seen | {j["uid"] for j in jobs})
    log_jobs(new, alerted=True)
    print(f"Posted {len(new)} new posting(s).")


def log_jobs(jobs, alerted):
    """
    Append full metadata to data/job_log.jsonl for the description fetcher,
    tagged with a CS sub-category (see job_store.classify_job) so a
    later analysis can tell AV apart from humanoid/drone/warehouse/etc.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    records = [
        {
            "uid": j["uid"],
            "source": "board",
            "company": j["company"],
            "title": j["title"],
            "url": j["url"],
            "location": j["location"],
            "category": job_store.classify_job(j["company"], j["title"]),
            "first_logged": now,
            "alerted_to_discord": alerted,
        }
        for j in jobs
    ]
    n = job_store.append_job_log(records)
    if n:
        print(f"Logged {n} job record(s) to data/job_log.jsonl.")


if __name__ == "__main__":
    main()
