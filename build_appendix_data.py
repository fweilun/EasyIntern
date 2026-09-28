"""
Export data/job_log.jsonl + data/descriptions.json into data/report_appendix.json
-- the flat, sorted structure reports/scripts/build_report.js reads to render
the appendix table in Robotics_Job_Market_Report.docx.

Run this (then `node reports/scripts/build_report.js`) whenever job_log.jsonl
has grown and it's time to refresh the report's data tables. The qualitative
sections (Executive Summary, skill gap table, learning priorities) are written
by hand/by Claude reading the underlying postings -- this script only refreshes
the mechanical data snapshot and appendix listing, not the analysis prose.
"""

import json

import job_store


def main():
    log = job_store.load_job_log()
    desc = job_store.load_descriptions()

    rows = []
    for r in log:
        d = desc.get(r["uid"], {})
        rows.append({
            "company": r.get("company") or "",
            "title": r.get("title") or "",
            "location": r.get("location") or "",
            "category": r.get("category", ""),
            "source": r.get("source", ""),
            "url": r.get("url") or "",
            "desc_status": d.get("status", "not_fetched"),
        })
    rows.sort(key=lambda r: (r["category"], r["company"], r["title"]))

    with open("data/report_appendix.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"wrote {len(rows)} rows to data/report_appendix.json")


if __name__ == "__main__":
    main()
