"""
Measure what fraction of the available literature this feed actually takes.

There is no denominator for the whole field. Nobody knows how many
environmental findings were published worldwide this month, and journalism has
no register at all — which is why the feed states the shape of its sample
rather than a coverage percentage.

But one slice IS countable. Every OpenAlex query can be asked how many works
match it in the same window the harvester uses, which gives a real ratio for
the peer-reviewed portion: taken / available. That number is defensible
because both halves come from the same API and the same filter.

    python -m pipeline.coverage_report

Reports per query and in total. Everything outside the OpenAlex sources —
journalism, institutional reporting, preprints — is left uncounted and said
to be uncounted.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .harvest import _phrase, openalex_get

ROOT = Path(__file__).resolve().parent.parent


def run():
    conf = yaml.safe_load((ROOT / "sources.yaml").read_text())
    cfg = conf["defaults"]
    report_path = ROOT / "harvest_report.json"
    taken = {}
    if report_path.exists():
        taken = {s["source"]: s["count"]
                 for s in json.loads(report_path.read_text())["sources"]}

    rows, tot_taken, tot_avail = [], 0, 0
    for src in conf["sources"]:
        if src["kind"] != "openalex":
            continue
        days = src.get("lookback_days", cfg["lookback_days"])
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        terms = "|".join(_phrase(t) for t in src["query"].split(" OR "))
        r = openalex_get({
            "per-page": 1,
            "filter": (f"title_and_abstract.search:{terms},type:article,"
                       f"from_publication_date:{since},has_abstract:true"),
        })
        if r.status_code != 200:
            rows.append((src["id"], taken.get(src["id"], 0), None))
            continue
        avail = r.json().get("meta", {}).get("count", 0)
        got = taken.get(src["id"], 0)
        rows.append((src["id"], got, avail))
        tot_taken += got
        tot_avail += avail

    print(f"{'query':<36}{'taken':>7}{'available':>11}{'share':>8}")
    for sid, got, avail in rows:
        if avail is None:
            print(f"{sid:<36}{got:>7}{'  (failed)':>11}")
        else:
            share = f"{got / avail * 100:.1f}%" if avail else "—"
            print(f"{sid:<36}{got:>7}{avail:>11}{share:>8}")

    if tot_avail:
        print(f"\n{'PEER-REVIEWED SLICE':<36}{tot_taken:>7}{tot_avail:>11}"
              f"{tot_taken / tot_avail * 100:>7.1f}%")
    print("\nThis figure covers ONLY the OpenAlex place queries. Journalism,")
    print("institutional reporting and preprints have no countable denominator")
    print("and are excluded — the true share of the field is unknown and this")
    print("number should never be presented as it.")

    (ROOT / "coverage_report.json").write_text(json.dumps({
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "scope": "openalex place queries only",
        "taken": tot_taken, "available": tot_avail,
        "queries": [{"id": s, "taken": t, "available": a} for s, t, a in rows],
    }, indent=1))


if __name__ == "__main__":
    run()
