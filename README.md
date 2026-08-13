# Frontline Feed

A live strip of environmental facts — not article titles — extracted from
scientific literature, regional journalism, and institutional reporting
worldwide, each tied to what it means at scale.

Runs entirely on free infrastructure: GitHub Actions, GitHub Pages, and the
free tiers of three model providers with a rule-based fallback underneath.

---

## What makes a row

A source document produces a row only if it yields a **fact** — a statement with
a subject and at least one of a quantity, a named actor or place, a stated
mechanism, or a threshold crossed. Roughly half of everything harvested yields
nothing, which is the intended behaviour, not a failure.

Each row carries:

- **the fact**, in plain declarative English
- **a context line** tying it to the larger trend, drawn from a curated library
- **the source sentence** it came from, shown on click
- region, evidence tier, date, and a link to the original

## Anchor binding

Every fact traces to one source sentence, or two adjacent ones where the second
supplies only scope. The validator in `pipeline/extract.py` enforces this
mechanically before anything publishes. It refuses:

| Failure | Example |
|---|---|
| number drift | 38% rendered as "nearly 40%" |
| splice | a figure from one sentence welded to a condition from another |
| invented scope | "across 12 states" when no sentence says so |
| unsupported place | a country name the source never mentions |
| added causation | "highest in maize areas" becoming "maize farming causes" |
| added harm | "exceeded the limit" becoming "contaminating the river" |
| added intensifiers | devastating, alarming, catastrophic, unprecedented |
| added adverbs | "secretly granted", "rapidly declining" |
| added qualifiers | "only 38%", "nearly 40%" |

A fact that fails is discarded and the document falls to the next engine. Run
the suite with `python -m pytest` or the inline cases in the commit history.

## Setup

```bash
pip install -r requirements.txt

# 1. Verify every source before anything else. Feed URLs move and die.
python -m pipeline.verify_sources

# 2. Harvest and build
python -m pipeline.harvest
python -m pipeline.build_feed
```

`verify_sources` writes `source_status.json`. Sources marked `ok: false` are
skipped by the harvester until fixed. Expect a handful of red lines on the first
run — that is information about those sources, not a bug here. Fix the URL in
`sources.yaml` or delete the entry.

### Free-tier keys

Add these as repository secrets. All are optional; the pipeline degrades one tier
at a time and never stops.

| Secret | Provider | Notes |
|---|---|---|
| `GEMINI_API_KEY` | aistudio.google.com | Primary. Best output quality of the free tiers. |
| `GROQ_API_KEY` | console.groq.com | Secondary. Large daily ceiling. |
| `CEREBRAS_API_KEY` | cloud.cerebras.ai | Tertiary. High throughput. |

Three consecutive failures retire a provider for that run. With no keys at all
the rule-based scorer handles everything — rows read like source prose rather
than written copy, and non-English sources are dropped rather than published
untranslated.

Published limits shift often. Check them at signup rather than trusting any
figure here.

### The context library

`context_library.json` ships with 59 entries, all marked `"verified": false`.
**Unverified entries are never attached to a row.** Confirm each claim against
its named source, set `"verified": true`, and record `"verified_on"`. Until then
the feed runs with facts only.

Entries carry a `direction` — `worsening`, `contested`, or `leverage` — which
sets the coloured rule beside the context line. Roughly a fifth are leverage or
contested on purpose. A library where every line points down teaches readers to
stop reading the lines.

Add `"prefer": ["glacier", "meltwater"]` to an entry to bias it toward facts
containing those words. Without it, matching falls back to tag rank plus
automatic word overlap, which is decent but coarse.

## Embedding on Weebly

Weebly's embed element rewrites inline scripts, so serve the strip from Pages
and iframe it:

```html
<iframe src="https://welcometoyourgalaxy.github.io/frontline-feed/"
        style="width:100%;height:620px;border:0" loading="lazy"
        title="Environmental frontline feed"></iframe>
```

To point the strip at a different feed file:

```html
<script src="..." data-feed="sample-feed.json"></script>
```

`sample-feed.json` is a preview build from synthetic documents — useful for
checking the layout before the first real run.

## Regional coverage

The source roster is weighted toward regional desks — InfoNile, Oxpeckers,
Actualite.cd, Agência Pública, ((o))eco, Mongabay's language editions, Frontier
Myanmar, Eurasianet, Vlast.kz, Jubi, Barents Observer — because a roster of
global outlets produces a North Atlantic mirror.

For the places thinnest in journalism, three channels do the work instead:
OpenAlex queried by **place rather than author affiliation**, ReliefWeb, and
World Bank and UNESCO disclosures. These run monthly to annually, so those
regions appear as slower, lumpier rows.

The coverage footer reports what the feed actually reached. Regions at zero stay
visibly at zero. Do not read the counter as a map of where destruction is
happening; read it as a map of where reporting exists.

`FEED_REGION_CAP` (default 0.30) caps any one region's share of the visible rows
by pushing surplus rows down the order. Nothing is deleted and the counter always
reports real totals.

## Known limits

- Facts are not cross-checked against each other. Two sources disagreeing produce two rows.
- The gazetteer in `tagger.py` covers countries and major features. An unrecognised place lands in **No place identified**, deliberately kept separate from Global.
- Context matching is topic-level. It will occasionally pair a lake measurement with a river context. Hand-tune with `prefer`.
- Deduplication uses DOI or a normalised title hash. Two outlets writing very differently about one paper can both survive.
- No completion or retraction tracking. A retracted paper's fact stays until it ages out of the window.
