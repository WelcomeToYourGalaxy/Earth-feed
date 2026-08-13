"""
Orchestrator. raw_items.json -> feed.json

Order of operations matters:
  extract -> validate (inside extract) -> tag -> match context -> balance -> write
"""

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import harvest
from .extract import Budget, extract
from .tagger import REGION_LABELS, REGION_ORDER, tag

ROOT = Path(__file__).resolve().parent.parent
MAX_ROWS = int(os.environ.get("FEED_MAX_ROWS", "250"))


def load_context_library():
    data = json.loads((ROOT / "context_library.json").read_text())
    entries = data["entries"]
    from .validate_context import check
    usable, rejected = [], []
    for e in entries:
        state, detail = check(e)
        (usable if state == "ok" else rejected).append((e, detail))
    if rejected:
        broken = [f"{e['id']} ({d})" for e, d in rejected
                  if check(e)[0] == "failed"]
        if broken:
            print(f"context: {len(broken)} entries REJECTED by anchor check — "
                  f"{', '.join(broken[:3])}{'...' if len(broken) > 3 else ''}")
    usable = [e for e, _ in usable]
    if not usable:
        print(f"context: 0 of {len(entries)} entries verified — rows will ship "
              f"without context lines until entries are confirmed and marked verified")
    else:
        print(f"context: {len(usable)} of {len(entries)} entries verified and in use")
    return usable


class ContextMatcher:
    """
    Matches a fact to a context entry by topic tag, preferring an entry whose
    region matches the fact's. Rotates among candidates so one entry does not
    appear on every row. No confident match means no context line — a blank
    beats a generic line, which is the same rule the fact gate applies.
    """

    def __init__(self, entries):
        self.by_tag = defaultdict(list)
        for e in entries:
            for t in e.get("tags", []):
                self.by_tag[t].append(e)
        self.used = Counter()
        self._fact = ""

    def match(self, topics, region, fact_text=""):
        self._fact = fact_text
        candidates = []
        for t in topics:
            candidates.extend(self.by_tag.get(t, []))
        if not candidates:
            return None
        seen, unique = set(), []
        for c in candidates:
            if c["id"] not in seen:
                seen.add(c["id"])
                unique.append(c)
        regional = [c for c in unique if c.get("region") == region]
        pool = regional or [c for c in unique if not c.get("region")] or unique

        fact_words = {w for w in re.findall(r"[a-z]{4,}", (self._fact or "").lower())}

        def rank(entry):
            # How well the entry's own priorities line up with the fact's.
            # Without this, an entry tagged [fire, forest] outranked a
            # forest-first entry on a deforestation fact purely by rotation.
            best = 99
            for ti, topic in enumerate(topics):
                if topic in entry.get("tags", []):
                    best = min(best, ti * 4 + entry["tags"].index(topic))
            # Tag rank alone leaves ties — every freshwater-tagged entry looks
            # equal on a freshwater fact, so rotation alone decided, and a lake
            # measurement could draw a glacier context. Prefer the entry whose
            # own wording overlaps the fact. An explicit "prefer" list in the
            # library beats the automatic overlap when hand-tuning is wanted.
            explicit = sum(1 for k in entry.get("prefer", [])
                           if k.lower() in (self._fact or "").lower())
            auto = len(fact_words & {w for w in re.findall(r"[a-z]{4,}", entry["text"].lower())})
            return (best, -explicit * 10 - auto, self.used[entry["id"]], entry["id"])

        pick = min(pool, key=rank)

        # Sharing a coarse tag is not a match. "forest" covers a logging
        # concession and a carbon sink; pairing them produced lines like an
        # Amazon deforestation fact under a pollination context. Require the
        # entry and the fact to share real subject words, not just a bucket.
        # Two shared words is too weak: "emissions" and "carbon" appear in
        # half the library, which is how a Punjab brick-kiln fact drew an
        # ocean-CO2 line. A context line implies the two are connected, so a
        # weak match asserts something the sources do not. Blank is safer.
        entry_words = {w for w in re.findall(r"[a-z]{4,}", pick["text"].lower())}
        shared = fact_words & entry_words
        # Require either a strong overlap, or a shared subject noun specific
        # enough to mean the entry is genuinely about this fact's subject.
        ANCHORS = {"forest", "deforestation", "mangrove", "peat", "soil",
                   "aquifer", "groundwater", "glacier", "river", "coral",
                   "reef", "mercury", "lead", "pfas", "pesticide", "tailings",
                   "mining", "permafrost", "pollinator", "species", "defender",
                   "indigenous", "ozone", "fisheries", "litigation"}
        if len(shared) < 4 and not (shared & ANCHORS):
            return None

        self.used[pick["id"]] += 1
        return pick


def balance(rows, limit):
    """
    Select the rows to publish, one region at a time.

    The previous approach sorted everything by date, kept the most recent
    couple of hundred, then capped each region's share of that slice. It worked
    at 740 documents and collapsed at 1,700: the recent-first cut was filled by
    whichever regions happened to publish most that week, and ten regions with
    real facts never reached the shortlist at all. Capping a shortlist cannot
    fix a shortlist that is already skewed.

    So: group by region, order each group newest first, then take one from each
    region in turn until the page is full. Every region holding a fact appears
    before any region takes a second row. Recency still decides which fact
    represents a region, and the leftovers fill the remainder in date order.
    """
    by_region = defaultdict(list)
    for row in rows:
        by_region[row["region"]].append(row)
    for group in by_region.values():
        group.sort(key=lambda r: r["published"] or "", reverse=True)

    picked, cursor = [], 0
    while len(picked) < limit:
        took = False
        for region in sorted(by_region):
            group = by_region[region]
            if cursor < len(group) and len(picked) < limit:
                picked.append(group[cursor])
                took = True
        if not took:
            break
        cursor += 1

    picked.sort(key=lambda r: r["published"] or "", reverse=True)
    return picked


def run():
    raw_path = ROOT / "raw_items.json"
    if not raw_path.exists():
        harvest.run()
    docs = json.loads(raw_path.read_text())

    # Free tiers run out. Spend them on documents that produce nothing at all
    # without a model — non-English first, then peer-reviewed — so quota
    # exhaustion costs the least.
    tier_rank = {"peer_reviewed": 0, "institutional": 1, "preprint": 2, "journalism": 3}
    docs.sort(key=lambda d: (str(d.get("lang", "en")) == "en",
                             tier_rank.get(d.get("tier"), 9)))

    matcher = ContextMatcher(load_context_library())
    budget = Budget()
    spent = Budget(limit=0)   # every provider already retired: scorer only
    rows, engines, dropped = [], Counter(), 0

    model_budget = int(os.environ.get("FEED_MODEL_CALLS", "600"))
    # An English document still yields a scorer row without a model; a French
    # or Russian one yields nothing. Free tiers are far too small to cover a
    # whole run, so by default they are spent only on the documents that would
    # otherwise be dropped. Set FEED_MODEL_ALL=1 to use them on everything.
    model_all = os.environ.get("FEED_MODEL_ALL") == "1"
    used = 0

    for i, doc in enumerate(docs, 1):
        wants_model = model_all or doc.get("lang", "en") != "en"
        # any_alive() matters: once every tier has retired there is nothing to
        # spend on. Without this the run burns the rest of the budget calling
        # providers that are already dead — 575 wasted calls last run.
        if wants_model and used < model_budget and budget.any_alive():
            used += 1
            if used % 25 == 0:
                print(f"  model calls {used}/{model_budget} "
                      f"({len(rows)} facts so far)")
            facts = extract(doc, budget)
        else:
            facts = extract(doc, spent)
        if not facts:
            dropped += 1
            continue
        for f in facts:
            # The scorer selects a source sentence verbatim. For a non-English
            # source that means an untranslated row, which is no use to a reader
            # scanning in seconds. Only the model tiers can translate.
            if f.engine == "scorer" and doc.get("lang", "en") != "en":
                continue
            tags = tag(f.text, f"{doc.get('title','')} {doc.get('body','')}")
            ctx = matcher.match(tags["topics"], tags["region"], f.text)
            engines[f.engine] += 1
            rows.append({
                "id": f"{doc['id']}-{f.anchor_index}",
                "fact": f.text,
                "fact_type": f.fact_type,
                "scope": f.scope,
                "context": ctx["text"] if ctx else None,
                "context_source": ctx["source"] if ctx else None,
                "context_url": ctx["source_url"] if ctx else None,
                "context_direction": ctx["direction"] if ctx else None,
                "anchor": f.anchor,
                "region": tags["region"],
                "region_label": tags["region_label"],
                "place": tags["place"],
                "topics": tags["topics"],
                "tier": doc["tier"],
                "source_name": doc["source_name"],
                "url": doc["url"],
                "published": doc["published"],
                "lang": doc.get("lang", "en"),
                "doi": doc.get("doi"),
                "also_reported_by": doc.get("also_reported_by", []),
                "engine": f.engine,
            })
        if i % 50 == 0:
            print(f"  {i}/{len(docs)} documents, {len(rows)} facts")

    # Balance across everything held, not a recent-first slice of it.
    all_rows = rows
    rows = balance(all_rows, MAX_ROWS)

    # The counter reports what was published and what was held back, so a
    # region that produced facts but lost the draw is still visible.
    shown = Counter(r["region"] for r in rows)
    held = Counter(r["region"] for r in all_rows)
    coverage = [
        {"region": k, "label": REGION_LABELS[k],
         "count": shown.get(k, 0), "available": held.get(k, 0)}
        for k in REGION_ORDER
    ]

    feed = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "coverage": coverage,
        "stats": {
            "documents_harvested": len(docs),
            "documents_yielding_nothing": dropped,
            "facts_published": len(rows),
            "by_engine": dict(engines),
            "context_lines_attached": sum(1 for r in rows if r["context"]),
        },
    }
    (ROOT / "feed.json").write_text(json.dumps(feed, indent=1, ensure_ascii=False))

    print(f"\n{len(docs)} documents -> {len(rows)} facts "
          f"({dropped} documents yielded nothing, which is expected)")
    print(f"engines: {dict(engines)} (model calls spent: {used}/{model_budget}"
          f"{'' if budget.any_alive() else '; all providers retired mid-run'})")
    thin = [c["label"] for c in coverage if c["count"] == 0]
    if thin:
        print(f"zero coverage this run: {', '.join(thin)}")
    print(f"regions represented: {sum(1 for c in coverage if c['count'])}"
          f"/{len(coverage)} | facts held back: {len(all_rows) - len(rows)}")
    return feed


if __name__ == "__main__":
    run()
