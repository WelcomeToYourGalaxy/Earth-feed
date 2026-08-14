"""
Harvest layer. Fetches every verified source, normalizes to a common record,
dedupes, and writes raw_items.json.

No interpretation happens here. This layer only decides what text the
extraction layer is allowed to look at.
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
UA = "FrontlineFeed/1.0 (+https://welcometoyourgalaxy.com; environmental monitoring)"
TIMEOUT = 30


BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch_feed_bytes(url):
    """
    Retrieve a feed as bytes before parsing.

    feedparser's own fetcher gets blocked by Cloudflare and by publishers who
    reject non-browser agents, and it then hands the HTML block page to the XML
    parser, producing "not well-formed" for a feed that is perfectly fine.
    Requesting it ourselves with browser headers and following redirects fixes
    most of those.
    """
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "en,fr;q=0.8,es;q=0.8,pt;q=0.8,ru;q=0.7",
    }
    agents = [BROWSER_UA,
              "Feedly/1.0 (+https://feedly.com/fetcher.html; like FeedFetcher-Google)",
              "SimplePie/1.5 (Feed Parser; +http://simplepie.org)",
              "Mozilla/5.0 (compatible; feedparser)"]
    last = None
    for agent in agents:
        headers["User-Agent"] = agent
        r = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return r.content
        last = r
        # A 5xx or 429 is the server having a moment, not a wrong address.
        # ((o))eco returned 500 mid-harvest and lost the source for the run.
        if r.status_code >= 500 or r.status_code == 429:
            time.sleep(2)
            r = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
            if r.status_code == 200:
                return r.content
            last = r
        if r.status_code not in (403, 401, 429) and r.status_code < 500:
            break   # 404 is a wrong URL; no agent or retry fixes that.
    last.raise_for_status()
    return last.content


def _now():
    return datetime.now(timezone.utc)


def _since(days):
    return _now() - timedelta(days=days)


def _clean(html):
    """Strip tags and collapse whitespace. Abstracts arrive full of markup."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    text = re.sub(r"&quot;", '"', text)
    # Numeric entities: &#8217; is an apostrophe, and it was reaching the feed.
    from html import unescape
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _record(source, title, body, url, published, extra=None):
    body = _clean(body)
    return {
        "id": hashlib.sha1((url or title).encode("utf-8")).hexdigest()[:16],
        "source_id": source["id"],
        "source_name": source["name"],
        "tier": source["tier"],
        "lang": str(source.get("lang", "en")),
        "title": _clean(title),
        "body": body,
        "url": url,
        "published": published,
        "harvested_at": _now().isoformat(),
        **(extra or {}),
    }


# ── Adapters ───────────────────────────────────────────────────────────

def _environmentally_relevant(text):
    """
    Gate for general-news feeds.

    Scroll.in's feed is all Indian news, not environmental news, and it supplied
    33 of 120 rows on the first live run — a quarter of the feed from one site
    on unrelated subjects. Section feeds are preferable where they exist; where
    they do not, an item has to at least mention the subject.
    """
    from .tagger import is_environmental
    return is_environmental(text)


def fetch_rss(source, cfg):
    out = []
    parsed = feedparser.parse(fetch_feed_bytes(source["url"]))
    cutoff = _since(source.get("lookback_days", cfg["lookback_days"]))
    rss_cap = source.get("max_items_per_source", cfg["max_items_per_source"])
    for entry in parsed.entries[: (len(parsed.entries) if rss_cap is None else rss_cap)]:
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if entry.get(key):
                published = datetime(*entry[key][:6], tzinfo=timezone.utc)
                break
        if published and published < cutoff:
            continue
        body = ""
        if entry.get("content"):
            body = entry["content"][0].get("value", "")
        body = body or entry.get("summary", "") or entry.get("description", "")
        title = entry.get("title", "")
        # Only gate English sources: the keyword patterns are English, and a
        # non-English item is translated later in the pipeline, not here.
        # Applies in every language now. The English-only condition here is
        # why Malay football and Angolan music reached the wire.
        if source.get("general_news"):
            if not _environmentally_relevant(f"{title} {_clean(body)}"):
                continue
        rec = _record(source, title, body, entry.get("link", ""),
                      published.isoformat() if published else None)
        # A Google News item is a headline and a link. Without body text there
        # is no sentence to anchor a fact to, so keep only those whose headline
        # itself carries a figure — the rest cannot clear the gate regardless.
        if source.get("headlines_only") and not re.search(r"\d", rec["title"]):
            continue
        out.append(rec)
    return out


_openalex_last = [0.0]


class OpenAlexKeyMissing(RuntimeError):
    pass


def openalex_get(params, attempts=4):
    """
    OpenAlex with an API key and backoff.

    OpenAlex made keys mandatory on 13 February 2026 and retired the polite
    pool and the mailto parameter with it. Unauthenticated callers get a small
    one-off credit allowance and then 429s that no amount of waiting clears —
    which is exactly what a mailto-only client looks like from the outside.
    Limits are now credit-based per key, with a free daily allowance.
    """
    key = os.environ.get("OPENALEX_API_KEY")
    if not key:
        raise OpenAlexKeyMissing(
            "OPENALEX_API_KEY is not set. Keys are mandatory since Feb 2026 — "
            "create a free account at openalex.org and copy the key from "
            "openalex.org/settings/api")
    params = dict(params, api_key=key)

    # Space out consecutive calls: eight place queries in a row is what trips
    # the limit, not the total volume.
    gap = 1.5 - (time.time() - _openalex_last[0])
    if gap > 0:
        time.sleep(gap)

    delay = 4.0
    for i in range(attempts):
        r = requests.get("https://api.openalex.org/works", params=params,
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        _openalex_last[0] = time.time()
        if r.status_code != 429:
            return r
        if i < attempts - 1:
            time.sleep(delay)
            delay *= 2          # 4, 8, 16 — enough to clear a short window
    return r


def fetch_openalex(source, cfg):
    """
    Place-keyed literature search. This is the main instrument against
    geographic bias: it finds work ABOUT a region regardless of author affiliation.
    """
    out = []
    since = _since(cfg["lookback_days"]).date().isoformat()
    cap = source.get("max_items_per_source", cfg["max_items_per_source"])
    terms = "|".join(_phrase(t) for t in source["query"].split(" OR "))
    # Two search filters AND together in OpenAlex, so this reads as
    # "about this place" AND "about the environment".
    params = {
        # type:article drops the supplementary-material and abstract-only
        # component records that were eating the per-source budget: the Congo
        # Basin source returned 40 items that were four papers, duplicated.
        "filter": (f"title_and_abstract.search:{terms},"
                   f"title_and_abstract.search:{OPENALEX_SUBJECT},"
                   f"type:article,from_publication_date:{since},"
                   f"has_abstract:true"),
        "per-page": 200 if cap is None else min(cap, 200),
        "cursor": "*",
    }
    while True:
      r = openalex_get(params)
      if r.status_code == 429:
        # Credit exhaustion and per-second limits share a status code. The
        # headers tell them apart: no credits remaining means waiting is
        # pointless until the daily reset.
        remaining = r.headers.get("X-RateLimit-Remaining")
        reason = ("daily credits exhausted" if remaining in ("0", 0)
                  else "rate-limited after backoff")
        print(f"  {source['id']}: {reason} — stopping at {len(out)} this run")
        return out
      r.raise_for_status()
      payload = r.json()
      for work in payload.get("results", []):
        title = work.get("title") or ""
        if PARATEXT.match(title.strip().lstrip('"')):
            continue
        abstract = _decode_inverted_abstract(work.get("abstract_inverted_index"))
        if not abstract or len(abstract) < 120:
            continue

        out.append(_record(
            source, title, abstract,
            work.get("doi") or work.get("id"),
            work.get("publication_date"),
            {"doi": work.get("doi")},
        ))
      nxt = (payload.get("meta") or {}).get("next_cursor")
      if not nxt or not payload.get("results"):
          break
      if cap is not None and len(out) >= cap:
          break
      params["cursor"] = nxt
    return out[:cap] if cap is not None else out


# Component and correction records masquerading as findings.
PARATEXT = re.compile(
    r"^(supplementary|supporting information|abstract from|data from|"
    r"correction to|corrigendum|erratum|retraction|editorial|"
    r"table of contents|front matter|back matter|index to volume)",
    re.I)


# Subject constraint for the place-keyed literature queries. Without it,
# "Horn of Africa" returned corporate governance and telemedicine papers —
# correctly matching the place and having nothing to do with the feed.
OPENALEX_SUBJECT = (
    'deforestation|pollution|contamination|biodiversity|ecosystem|ecology|'
    'conservation|"climate change"|drought|flooding|wildfire|erosion|'
    '"land use"|"water quality"|groundwater|aquifer|fisheries|overfishing|'
    'emissions|carbon|methane|mining|tailings|pesticide|"heavy metals"|'
    'microplastic|"habitat loss"|extinction|"protected area"|wetland|'
    'mangrove|peatland|glacier|permafrost|"soil degradation"|salinization|'
    'eutrophication|"air quality"|deforested|logging|"land degradation"'
)


def _phrase(term):
    """Quote multi-word terms. Unquoted, OpenAlex matches the words loosely."""
    term = term.strip()
    return f'"{term}"' if " " in term else term


def _decode_inverted_abstract(inverted):
    """OpenAlex ships abstracts as {word: [positions]}. Rebuild the text."""
    if not inverted:
        return ""
    positions = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def fetch_europepmc(source, cfg):
    out = []
    query = source["query"].format(
        since=_since(cfg["lookback_days"]).date().isoformat(),
        now=_now().date().isoformat(),
    )
    r = requests.get(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params={"query": query, "format": "json", "resultType": "core",
                "pageSize": cfg["max_items_per_source"]},
        headers={"User-Agent": UA}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    for res in r.json().get("resultList", {}).get("result", []):
        if not res.get("abstractText"):
            continue
        doi = res.get("doi")
        out.append(_record(
            source, res.get("title", ""), res["abstractText"],
            f"https://doi.org/{doi}" if doi else res.get("fullTextUrlList", {}).get("url"),
            res.get("firstPublicationDate"), {"doi": doi},
        ))
    return out


def fetch_crossref(source, cfg):
    out = []
    since = _since(cfg["lookback_days"]).date().isoformat()
    for issn in source["issn"]:
        try:
            r = requests.get(
                "https://api.crossref.org/works",
                params={"filter": f"issn:{issn},from-pub-date:{since},has-abstract:true",
                        "rows": 20, "mailto": "feed@welcometoyourgalaxy.com"},
                headers={"User-Agent": UA}, timeout=TIMEOUT,
            )
            r.raise_for_status()
            for item in r.json().get("message", {}).get("items", []):
                abstract = _clean(item.get("abstract", ""))
                if not abstract:
                    continue
                title = (item.get("title") or [""])[0]
                out.append(_record(
                    source, title, abstract,
                    item.get("URL"), _crossref_date(item), {"doi": item.get("DOI")},
                ))
            time.sleep(0.5)
        except Exception as exc:  # one dead ISSN must not kill the run
            print(f"  crossref {issn}: {exc}")
    return out


def _crossref_date(item):
    parts = (item.get("issued") or {}).get("date-parts") or [[]]
    if not parts[0]:
        return None
    y, m, d = (list(parts[0]) + [1, 1])[:3]
    return f"{y:04d}-{m:02d}-{d:02d}"


def fetch_biorxiv(source, cfg):
    out = []
    since = _since(cfg["lookback_days"]).date().isoformat()
    now = _now().date().isoformat()
    r = requests.get(
        f"https://api.biorxiv.org/details/{source['server']}/{since}/{now}",
        headers={"User-Agent": UA}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    subject = source.get("subject", "").lower()
    for item in r.json().get("collection", [])[:400]:
        if subject and subject not in (item.get("category") or "").lower():
            continue
        if not item.get("abstract"):
            continue
        out.append(_record(
            source, item.get("title", ""), item["abstract"],
            f"https://doi.org/{item.get('doi')}", item.get("date"),
            {"doi": item.get("doi")},
        ))
        cap = source.get("max_items_per_source", cfg["max_items_per_source"])
        if cap is not None and len(out) >= cap:
            break
    return out


def fetch_reliefweb(source, cfg):
    """Free, no key, and unusually strong on regions newsrooms don't staff."""
    out = []
    since = _since(cfg["lookback_days"]).date().isoformat()
    payload = {
        "appname": os.environ.get("RELIEFWEB_APPNAME", "welcometoyourgalaxy-frontline"),
        "query": {"value": source["query"], "operator": "OR"},
        "filter": {"field": "date.created", "value": {"from": f"{since}T00:00:00+00:00"}},
        "fields": {"include": ["title", "body", "url", "date.created", "country.name"]},
        "limit": cfg["max_items_per_source"],
        "sort": ["date.created:desc"],
    }
    r = requests.post("https://api.reliefweb.int/v2/reports",
                      json=payload, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    for item in r.json().get("data", []):
        f = item.get("fields", {})
        countries = ", ".join(c.get("name", "") for c in f.get("country", []) or [])
        out.append(_record(
            source, f.get("title", ""), (f.get("body") or "")[:6000],
            f.get("url"), (f.get("date") or {}).get("created"),
            {"countries": countries},
        ))
    return out


def fetch_worldbank(source, cfg):
    out = []
    since = _since(cfg["lookback_days"]).date().isoformat()
    r = requests.get(
        "https://search.worldbank.org/api/v2/projects",
        params={"format": "json", "rows": cfg["max_items_per_source"],
                "os": 0, "fl": "id,project_name,pdo,countryshortname,boardapprovaldate,url",
                "strdate": since},
        headers={"User-Agent": UA}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    projects = r.json().get("projects", {})
    for pid, p in projects.items():
        body = p.get("pdo") or ""
        if not body:
            continue
        out.append(_record(
            source, p.get("project_name", ""),
            f"{body} Country: {p.get('countryshortname', '')}.",
            p.get("url") or f"https://projects.worldbank.org/en/projects-operations/project-detail/{pid}",
            p.get("boardapprovaldate"),
        ))
    return out


ADAPTERS = {
    "rss": fetch_rss,
    "openalex": fetch_openalex,
    "europepmc": fetch_europepmc,
    "crossref": fetch_crossref,
    "biorxiv": fetch_biorxiv,
    "reliefweb": fetch_reliefweb,
    "worldbank": fetch_worldbank,
}


# ── Dedup ──────────────────────────────────────────────────────────────

def _title_key(title):
    """
    Normalized title hash. Forty outlets covering one paper should not become
    forty rows — and the count of outlets is itself a signal worth keeping.
    """
    low = re.sub(r"[^a-z0-9 ]", "", (title or "").lower())
    tokens = [t for t in low.split() if len(t) > 3]
    return hashlib.sha1(" ".join(sorted(tokens)[:12]).encode()).hexdigest()[:12]


def dedupe(items):
    by_key = {}
    for item in items:
        key = item.get("doi") or _title_key(item["title"])
        if key in by_key:
            existing = by_key[key]
            existing["also_reported_by"] = existing.get("also_reported_by", [])
            if item["source_name"] not in existing["also_reported_by"]:
                existing["also_reported_by"].append(item["source_name"])
            # Prefer the strongest evidence tier as the canonical record.
            rank = {"peer_reviewed": 0, "institutional": 1, "preprint": 2, "journalism": 3}
            if rank.get(item["tier"], 9) < rank.get(existing["tier"], 9):
                item["also_reported_by"] = existing["also_reported_by"]
                by_key[key] = item
        else:
            by_key[key] = item
    return list(by_key.values())


# ── Entry point ────────────────────────────────────────────────────────

def load_config():
    with open(ROOT / "sources.yaml") as f:
        return yaml.safe_load(f)


def load_status():
    path = ROOT / "source_status.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def run():
    conf = load_config()
    cfg = conf["defaults"]
    status = load_status()
    items, report = [], []

    for source in conf["sources"]:
        sid = source["id"]
        st = status.get(sid, {})
        if st.get("ok") is False and not st.get("transient"):
            report.append({"source": sid, "count": 0, "note": "skipped — failed verification"})
            print(f"skip {sid}: failed verification")
            continue
        adapter = ADAPTERS.get(source["kind"])
        if not adapter:
            report.append({"source": sid, "count": 0, "note": f"no adapter for {source['kind']}"})
            continue
        try:
            got = adapter(source, cfg)
            items.extend(got)
            report.append({"source": sid, "count": len(got), "note": None})
            print(f"ok   {sid}: {len(got)}")
        except Exception as exc:
            report.append({"source": sid, "count": 0, "note": str(exc)[:200]})
            print(f"FAIL {sid}: {exc}")

    before = len(items)
    items = dedupe(items)
    print(f"\n{before} raw -> {len(items)} after dedupe")

    (ROOT / "raw_items.json").write_text(json.dumps(items, indent=1, ensure_ascii=False))

    # A browser-sized copy of the same intake. raw_items.json carries full
    # article bodies and runs to ~19 MB, which no embed should download; this
    # keeps every item but trims the body to a readable snippet.
    wire = [{
        "title": it["title"],
        "body": (it.get("body") or "")[:300],
        "url": it.get("url"),
        "source_name": it["source_name"],
        "tier": it["tier"],
        "lang": it.get("lang", "en"),
        "published": it.get("published"),
    } for it in items]
    (ROOT / "wire.json").write_text(json.dumps(wire, ensure_ascii=False,
                                               separators=(",", ":")))
    print(f"wire.json: {len(wire)} items, "
          f"{(ROOT / 'wire.json').stat().st_size / 1e6:.1f} MB")
    (ROOT / "harvest_report.json").write_text(json.dumps(
        {"run_at": _now().isoformat(), "raw": before, "deduped": len(items),
         "sources": report}, indent=1))
    return items


if __name__ == "__main__":
    run()
