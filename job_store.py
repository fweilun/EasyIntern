"""
Append-only ledger of every job ever alerted to Discord, plus a cache of
fetched job-description text. Both watchers (check_company_boards.py,
check_simplify_jobs.py) log through this; fetch_descriptions.py and
build_report.py read from it.

data/job_log.jsonl   one JSON object per line, one line per uid, ever-growing.
                     Never rewritten in place -- append only, so a crash mid-
                     write can't corrupt earlier entries.
data/descriptions.json   dict: uid -> {text, fetched_at, status, method}
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
JOB_LOG_FILE = os.path.join(DATA_DIR, "job_log.jsonl")
DESCRIPTIONS_FILE = os.path.join(DATA_DIR, "descriptions.json")
KEYWORDS_FILE = os.path.join(HERE, "keywords.json")


CATEGORY_ORDER = [
    "av", "humanoid", "drone_aerial", "warehouse_logistics",
    "industrial_manufacturing", "healthcare_service", "defense",
    "general_robotics_ai",
]
FALLBACK_CATEGORY = "general_robotics_other"


def _kw_regex(terms):
    return re.compile(
        r"(?<!\w)(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")(?!\w)",
        re.IGNORECASE,
    )


def _load_category_scope():
    with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    companies = {c.lower(): cat for c, cat in cfg["company_categories"].items()}
    term_res = {cat: _kw_regex(terms) for cat, terms in cfg["category_terms"].items()}
    return companies, term_res


_COMPANY_CATEGORIES, _CATEGORY_TERM_RES = _load_category_scope()


def classify_job(company, title):
    """
    Tags every logged job with a robotics sub-category, so the report can
    compare skill demand across sub-fields (AV vs humanoid vs warehouse...)
    instead of lumping everything together. A company in the curated map
    wins outright; otherwise category_terms is tried in CATEGORY_ORDER
    (av first, since that's Eric's focus and some terms are ambiguous --
    e.g. "world model" shows up in both AV and general-robotics postings)
    against the title and company name, first match wins. No match at all
    falls back to FALLBACK_CATEGORY.
    """
    company = company or ""
    title = title or ""
    cat = _COMPANY_CATEGORIES.get(company.lower())
    if cat:
        return cat
    for cat in CATEGORY_ORDER:
        rx = _CATEGORY_TERM_RES[cat]
        if rx.search(title) or rx.search(company):
            return cat
    return FALLBACK_CATEGORY


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_logged_uids():
    """Every uid already in job_log.jsonl, so callers can skip re-appending."""
    if not os.path.exists(JOB_LOG_FILE):
        return set()
    uids = set()
    with open(JOB_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            uids.add(json.loads(line)["uid"])
    return uids


def load_job_log():
    """All logged records as a list, in the order they were appended."""
    if not os.path.exists(JOB_LOG_FILE):
        return []
    records = []
    with open(JOB_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def append_job_log(records):
    """Append new records (list of dicts with at least 'uid'). Skips dupes."""
    if not records:
        return 0
    _ensure_data_dir()
    existing = load_logged_uids()
    fresh = [r for r in records if r["uid"] not in existing]
    if not fresh:
        return 0
    with open(JOB_LOG_FILE, "a", encoding="utf-8") as f:
        for r in fresh:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(fresh)


def load_descriptions():
    if not os.path.exists(DESCRIPTIONS_FILE):
        return {}
    with open(DESCRIPTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_descriptions(descriptions):
    _ensure_data_dir()
    with open(DESCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(descriptions, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
