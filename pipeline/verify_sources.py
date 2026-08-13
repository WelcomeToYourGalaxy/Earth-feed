"""
Live source verification. Run this before the first harvest and on a schedule.

Every URL in sources.yaml is fetched and checked for a parseable response with
recent entries. Failures are written to source_status.json with the reason, and
the harvester skips them. Nothing enters the feed from an unverified source.

Feed URLs move and die constantly. Treat a red line here as information about
the source, not as a bug in this file.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
UA = "FrontlineFeed/1.0 (+https://welcometoyourgalaxy.com; source verification)"
TIMEOUT = 25


# Common feed paths, in the order they are worth trying.
CANDIDATE_PATHS = [
    "/feed/", "/feed", "/rss/", "/rss", "/rss.xml", "/atom.xml", "/feed.xml",
    "/index.xml", "/?feed=rss2", "/feed/rss/", "/en/feed/", "/en/rss.xml",
    "/feeds/posts/default", "/rss/all.xml", "/api/rss", "/rss/feed",
    "/feeds/all.rss", "/services/rss", "/category/all/feed", "/news/feed/",
    "/en/rss/", "/en/feed/rss/", "/blog/feed/", "/all/feed", "/rss/nyheter",
]


def _looks_like_feed(content):
    head = content[:400].lstrip().lower()
    return head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head


def _alternate_links(html, base):
    """Read <link rel=alternate type=application/rss+xml> — the standard way a
    page advertises its own feed. Solves most wrong-URL cases outright."""
    from urllib.parse import urljoin
    found = []
    for tag in re.findall(r"<link[^>]+>", html.decode("utf-8", "ignore"), re.I):
        if "alternate" not in tag.lower():
            continue
        if not re.search(r"type\s*=\s*[\"\']?application/(rss|atom)\+xml", tag, re.I):
            continue
        m = re.search(r"href\s*=\s*[\"\']([^\"\']+)", tag, re.I)
        if m:
            found.append(urljoin(base, m.group(1)))
    return found


def diagnose(url):
    """
    Report what the server actually returned, then try to find a working feed.

    Fifteen sources failing with 'not well-formed' says nothing useful. Four of
    them failing at the identical byte offset (2:1326) says they are all being
    handed the same page — almost certainly a bot-protection interstitial, not
    four broken feeds. Status, content-type and a snippet make that visible.
    """
    from urllib.parse import urlsplit
    from .harvest import fetch_feed_bytes, BROWSER_UA

    info = {"url": url}
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA,
                                       "Accept": "application/rss+xml, application/xml, */*"},
                         timeout=TIMEOUT, allow_redirects=True)
        info["status"] = r.status_code
        info["content_type"] = (r.headers.get("content-type") or "?").split(";")[0]
        info["final_url"] = r.url
        body = r.content
        info["snippet"] = body[:110].decode("utf-8", "ignore").replace("\n", " ").strip()
    except Exception as exc:
        info["error"] = str(exc)[:140]
        return info

    if _looks_like_feed(body):
        parsed = feedparser.parse(body)
        info["entries"] = len(parsed.entries)
        if parsed.entries:
            info["working_url"] = url
            return info
        info["parse_error"] = str(parsed.get("bozo_exception"))[:120]

    # Served HTML: ask the site where its feed is, then fall back to guessing.
    parts = urlsplit(url)
    host = parts.netloc
    roots = [f"{parts.scheme}://{host}"]
    bare = host[4:] if host.startswith("www.") else host
    for variant in (f"en.{bare}", bare, f"www.{bare}"):
        if variant != host:
            roots.append(f"{parts.scheme}://{variant}")

    tried = list(_alternate_links(body, r.url))

    # The homepage is where a site advertises its feed; a 404 page is not.
    for root in roots:
        try:
            home = requests.get(root, headers={"User-Agent": BROWSER_UA},
                                timeout=TIMEOUT, allow_redirects=True)
            if home.status_code == 200:
                tried += _alternate_links(home.content, home.url)
        except Exception:
            pass

    for root in roots:
        tried += [root + path for path in CANDIDATE_PATHS]

    seen = set()
    for cand in tried:
        if cand in seen or cand == url:
            continue
        seen.add(cand)
        try:
            content = fetch_feed_bytes(cand)
            parsed = feedparser.parse(content)
            if len(parsed.entries) >= 3 and "comment" not in cand.lower():
                info["working_url"] = cand
                info["entries"] = len(parsed.entries)
                return info
        except Exception:
            continue
    info["working_url"] = None
    return info


def check_rss(source):
    from .harvest import fetch_feed_bytes
    parsed = feedparser.parse(fetch_feed_bytes(source["url"]))
    if parsed.get("bozo") and not parsed.entries:
        return False, f"unparseable: {str(parsed.get('bozo_exception'))[:120]}"
    if not parsed.entries:
        return False, "parsed but returned no entries"
    dated = sum(1 for e in parsed.entries
                if e.get("published_parsed") or e.get("updated_parsed"))
    note = f"{len(parsed.entries)} entries"
    if dated == 0:
        note += " (no dates — lookback filter cannot apply)"
    return True, note


def _openalex_get(params):
    # One attempt only. A 429 here is classified transient and the source is
    # not benched, so there is nothing to gain from waiting it out during a
    # check — and eight sources each backing off made verification look hung.
    from .harvest import openalex_get
    return openalex_get(params, attempts=1)


def check_openalex(source):
    from .harvest import OpenAlexKeyMissing
    from .harvest import _phrase
    terms = "|".join(_phrase(t) for t in source["query"].split(" OR "))
    r = _openalex_get({"per-page": 1,
                       "filter": f"title_and_abstract.search:{terms},type:article,has_abstract:true"})
    if r.status_code == 429:
        remaining = r.headers.get("X-RateLimit-Remaining")
        if remaining in ("0", 0):
            return False, "daily credits exhausted — resets at midnight UTC"
        return False, "HTTP 429 rate-limited — check skipped, source not benched"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    n = r.json().get("meta", {}).get("count", 0)
    return (n > 0), f"{n} works match query"


def check_europepmc(source):
    r = requests.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                     params={"query": "environment", "format": "json", "pageSize": 1},
                     headers={"User-Agent": UA}, timeout=TIMEOUT)
    return (r.status_code == 200), f"HTTP {r.status_code}"


def check_crossref(source):
    live, dead = [], []
    for issn in source["issn"]:
        try:
            r = requests.get("https://api.crossref.org/journals/" + issn,
                             headers={"User-Agent": UA}, timeout=TIMEOUT)
            (live if r.status_code == 200 else dead).append(issn)
        except Exception:
            dead.append(issn)
    if not live:
        return False, "no ISSN resolved"
    return True, f"{len(live)} ISSN live" + (f", dead: {', '.join(dead)}" if dead else "")


def check_biorxiv(source):
    today = datetime.now(timezone.utc).date().isoformat()
    r = requests.get(f"https://api.biorxiv.org/details/{source['server']}/{today}/{today}",
                     headers={"User-Agent": UA}, timeout=TIMEOUT)
    return (r.status_code == 200), f"HTTP {r.status_code}"


def check_reliefweb(source):
    r = requests.post("https://api.reliefweb.int/v2/reports",
                      json={"appname": "welcometoyourgalaxy-frontline", "limit": 1},
                      headers={"User-Agent": UA}, timeout=TIMEOUT)
    return (r.status_code == 200), f"HTTP {r.status_code}"


def check_worldbank(source):
    r = requests.get("https://search.worldbank.org/api/v2/projects",
                     params={"format": "json", "rows": 1},
                     headers={"User-Agent": UA}, timeout=TIMEOUT)
    return (r.status_code == 200), f"HTTP {r.status_code}"


CHECKS = {
    "rss": check_rss, "openalex": check_openalex, "europepmc": check_europepmc,
    "crossref": check_crossref, "biorxiv": check_biorxiv,
    "reliefweb": check_reliefweb, "worldbank": check_worldbank,
}


def run():
    conf = yaml.safe_load((ROOT / "sources.yaml").read_text())
    status, ok_count = {}, 0

    for source in conf["sources"]:
        check = CHECKS.get(source["kind"])
        if not check:
            status[source["id"]] = {"ok": False, "note": f"no checker for {source['kind']}"}
            continue
        try:
            ok, note = check(source)
        except Exception as exc:
            ok, note = False, str(exc)[:200]
        if not ok and source["kind"] == "rss":
            d = diagnose(source["url"])
            bits = [f"HTTP {d.get('status', d.get('error', '?'))}",
                    d.get("content_type", "")]
            if d.get("working_url"):
                bits.append(f"WORKING URL -> {d['working_url']} ({d['entries']} entries)")
            elif d.get("snippet"):
                bits.append(f"got: {d['snippet'][:70]}")
            note = " | ".join(b for b in bits if b)
        # Network-layer trouble is the machine's, not the feed's. DNS
        # resolution failures and read timeouts were being treated as dead
        # feeds and benching working sources — Kloop went down that way.
        transient = (not ok) and bool(re.search(
            r"(\b429\b|\b50\d\b|timeout|timed out|connection|temporarily|"
            r"nameresolution|max retries|resolve|dns|ssl|reset by peer)", note, re.I))

        # If discovery found a working feed, the source is alive. Either the
        # configured url flaked (CABAR returned 200 rss+xml, then parsed fine
        # a second later) or the site is rotating paths behind bot protection
        # (Euractiv has offered four different working urls in four runs).
        # Report the suggestion, but do not bench a source that answers.
        if (not ok) and "WORKING URL" in note:
            transient = True
        status[source["id"]] = {
            "ok": ok, "transient": transient,
            "note": note + (" [transient — not benched]" if transient else ""),
            "name": source["name"],
            "regions": source.get("regions", []), "tier": source["tier"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        ok_count += ok
        print(f"{'ok  ' if ok else 'FAIL'} {source['id']:<24} {note}")

    (ROOT / "source_status.json").write_text(json.dumps(status, indent=1))

    fixes = {sid: s["note"].split("WORKING URL -> ")[1].split(" (")[0]
             for sid, s in status.items()
             if not s.get("ok") and "WORKING URL -> " in (s.get("note") or "")}
    if fixes:
        print("\nReplace these urls in sources.yaml:")
        for sid, u in fixes.items():
            print(f"  {sid:<24} {u}")
    total = len(conf["sources"])
    print(f"\n{ok_count}/{total} sources verified")

    failed_regions = {}
    for sid, s in status.items():
        if not s.get("ok"):
            for r in s.get("regions", []):
                failed_regions[r] = failed_regions.get(r, 0) + 1
    if failed_regions:
        print("failed sources by region hint:",
              ", ".join(f"{k}:{v}" for k, v in sorted(failed_regions.items())))
    return 0 if ok_count else 1


if __name__ == "__main__":
    sys.exit(run())
