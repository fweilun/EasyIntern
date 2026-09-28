"""
Fill in data/descriptions.json with the full job-description text for every
AV-scoped posting in data/job_log.jsonl that doesn't have one yet.

Per platform:
  greenhouse        -> per-job detail endpoint's `content` field (HTML)
  lever / ashby      -> already embedded in the board's list response
                        (descriptionPlain / descriptionHtml), so this re-fetches
                        the company's list once and reuses it for every job
  smartrecruiters    -> per-job detail endpoint's jobAd.sections (HTML each)
  simplify (external company URL, unknown platform)
                     -> best-effort generic HTML fetch + tag-strip. Works for
                        static career pages, fails on JS-rendered ones (SPA
                        Workday/Greenhouse-embed pages) -- those are recorded
                        with status="fetch_failed" and just keep their URL.

Run daily via GitHub Actions (separate, slower cadence than the 30-min alert
polls -- description fetching hits many more external hosts and doesn't need
to be instant).
"""

import argparse
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.request

import check_company_boards as boards
import job_store
from html_text import html_to_text

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# For SimplifyJobs entries the company/platform is unknown up front, but a
# lot of them turn out to be hosted on a platform we already know how to read
# structured data from -- just not one of the 44 boards in ats_config.json.
# Recognizing the URL shape avoids falling back to a raw HTML fetch, which
# comes back empty on any JS-rendered page (most Workday tenants).
ASHBY_URL_RE = re.compile(r"^https://jobs\.ashbyhq\.com/([^/]+)/([0-9a-fA-F-]{36})")
WORKDAY_URL_RE = re.compile(r"^(https://[\w-]+\.wd\d+\.myworkdayjobs\.com)(/.+)$")


def fetch_url(url, timeout=20, user_agent=boards.UA):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, timeout=20):
    return json.loads(fetch_url(url, timeout))


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def fetch_greenhouse(slug, job_id):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
    detail = fetch_json(url)
    return html_to_text(detail.get("content", ""))


def fetch_smartrecruiters(slug, job_id):
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"
    detail = fetch_json(url)
    sections = ((detail.get("jobAd") or {}).get("sections") or {})
    parts = []
    for sec in sections.values():
        title = sec.get("title", "")
        text = html_to_text(sec.get("text", ""))
        if text:
            parts.append(f"## {title}\n{text}" if title else text)
    return "\n\n".join(parts)


def fetch_generic(url):
    raw = fetch_url(url, timeout=20, user_agent=BROWSER_UA)
    charset = "utf-8"
    try:
        text = raw.decode(charset)
    except UnicodeDecodeError:
        text = raw.decode(charset, errors="replace")
    return html_to_text(text)


def fetch_ashby_by_url(url):
    m = ASHBY_URL_RE.match(url)
    org_slug, job_id = m.group(1), m.group(2)
    data = fetch_json(boards.ENDPOINTS["ashby"].format(slug=org_slug))
    for j in data.get("jobs", []):
        if j["id"] == job_id:
            return html_to_text(j.get("descriptionHtml", ""))
    raise ValueError("job id not found on that org's current Ashby board (likely closed)")


def fetch_workday_by_url(url):
    m = WORKDAY_URL_RE.match(url)
    origin, path = m.group(1), m.group(2)
    tenant = origin.split("//", 1)[1].split(".", 1)[0]
    cxs_url = f"{origin}/wday/cxs/{tenant}{path}"
    detail = fetch_json(cxs_url)
    return html_to_text((detail.get("jobPostingInfo") or {}).get("jobDescription", ""))


def fetch_simplify_url(url):
    """Recognizes Ashby/Workday URLs and reads structured JSON from them;
    anything else falls back to a raw HTML fetch+strip (fails on JS pages)."""
    if ASHBY_URL_RE.match(url):
        return fetch_ashby_by_url(url), "ashby_by_url"
    if WORKDAY_URL_RE.match(url):
        return fetch_workday_by_url(url), "workday_cxs"
    return fetch_generic(url), "generic_html"


def resolve_board_job(record, ats_config, lever_cache, ashby_cache):
    """Returns (text, method) for a 'board'-source record, or raises."""
    uid = record["uid"]
    platform_prefix, job_id = uid.split(":", 1)
    cfg = ats_config.get(record["company"])
    if not cfg:
        raise ValueError(f"company {record['company']!r} not in ats_config.json")
    slug = cfg["slug"]

    if platform_prefix == "gh":
        return fetch_greenhouse(slug, job_id), "greenhouse_detail"

    if platform_prefix == "lv":
        if slug not in lever_cache:
            lever_cache[slug] = fetch_json(boards.ENDPOINTS["lever"].format(slug=slug))
        for j in lever_cache[slug]:
            if j["id"] == job_id:
                parts = [j.get("openingPlain", ""), j.get("descriptionPlain", ""),
                          j.get("additionalPlain", "")]
                return "\n\n".join(p for p in parts if p), "lever_list"
        raise ValueError("job id not found in current Lever list (likely closed)")

    if platform_prefix == "ab":
        if slug not in ashby_cache:
            ashby_cache[slug] = fetch_json(boards.ENDPOINTS["ashby"].format(slug=slug))
        for j in ashby_cache[slug].get("jobs", []):
            if j["id"] == job_id:
                return html_to_text(j.get("descriptionHtml", "")), "ashby_list"
        raise ValueError("job id not found in current Ashby list (likely closed)")

    if platform_prefix == "sr":
        return fetch_smartrecruiters(slug, job_id), "smartrecruiters_detail"

    raise ValueError(f"unknown platform prefix {platform_prefix!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-fetch even if already cached")
    ap.add_argument("--retry-failed", action="store_true",
                     help="also re-fetch uids cached as empty/fetch_failed")
    ap.add_argument("--limit", type=int, default=None, help="cap how many to fetch this run")
    args = ap.parse_args()

    with open(boards.ATS_CONFIG_FILE, "r", encoding="utf-8") as f:
        ats_config = json.load(f)

    log = job_store.load_job_log()
    descriptions = job_store.load_descriptions()

    def needs_fetch(r):
        if args.force:
            return True
        cached = descriptions.get(r["uid"])
        if cached is None:
            return True
        if args.retry_failed and cached["status"] != "ok":
            return True
        return False

    todo = [r for r in log if needs_fetch(r)]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("Nothing to fetch.")
        return

    lever_cache, ashby_cache = {}, {}
    ok, failed = 0, 0
    for i, record in enumerate(todo, 1):
        uid = record["uid"]
        try:
            if record["source"] == "board":
                text, method = resolve_board_job(record, ats_config, lever_cache, ashby_cache)
            elif record["source"] == "simplify":
                if not record.get("url"):
                    raise ValueError("no url recorded")
                text, method = fetch_simplify_url(record["url"])
            else:
                raise ValueError(f"unknown source {record['source']!r}")

            descriptions[uid] = {
                "text": text,
                "char_count": len(text),
                "fetched_at": now_iso(),
                "status": "ok" if text.strip() else "empty",
                "method": method,
            }
            ok += 1
            print(f"  [{i}/{len(todo)}] OK   {record['company']} - {record['title'][:50]}"
                  f" ({len(text)} chars, {method})")
        except Exception as e:
            descriptions[uid] = {
                "text": "",
                "char_count": 0,
                "fetched_at": now_iso(),
                "status": "fetch_failed",
                "method": None,
                "error": str(e),
            }
            failed += 1
            print(f"  [{i}/{len(todo)}] FAIL {record['company']} - {record['title'][:50]}: {e}",
                  file=sys.stderr)

        # Be polite, especially to the many distinct external hosts SimplifyJobs
        # URLs point at.
        time.sleep(0.5)

    job_store.save_descriptions(descriptions)
    print(f"\nFetched {ok} ok, {failed} failed, out of {len(todo)} attempted.")


if __name__ == "__main__":
    main()
