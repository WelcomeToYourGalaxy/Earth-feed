"""
Extraction. Turns a harvested document into at most a few standalone facts.

Cascade, in order:
    1. Gemini 2.5 Flash   (free tier — best output)
    2. Groq               (free tier — large daily ceiling)
    3. Cerebras           (free tier — high throughput)
    4. Sentence scorer    (rule-based, never fails, no network)

Every model output — regardless of tier — passes the same validator before it
is allowed into the feed. A fact that fails validation is discarded, and the
document falls through to the next tier. If all four are exhausted the document
produces nothing. Producing nothing is a correct outcome.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field

import requests

TIMEOUT = 60

# Words that assert an evaluation rather than report one. Permitted only when
# the source itself used them. This is what keeps a row punchy through
# specificity instead of through intensity.
INTENSIFIERS = {
    "devastating", "alarming", "catastrophic", "unprecedented", "shocking",
    "staggering", "dire", "horrifying", "terrifying", "dramatic", "massive",
    "enormous", "extreme", "severe", "critical", "crisis", "collapse",
    "destroyed", "ravaged", "decimated", "poisoning", "poisoned", "wiped out",
    "irreversible", "catastrophe", "emergency", "existential", "apocalyptic",
}

# Words asserting causation. Permitted only when present in the anchor —
# "concentrations were highest in maize areas" is not "maize farming causes it".
# Verbs asserting harm or consequence. Stem-matched because the inflection
# varies ("contaminating", "contaminated", "contaminates") and the claim does
# not. A fact may say a threshold was exceeded; saying the river was
# contaminated is a further claim the source has to have made itself.
HARM_STEMS = [
    "contaminat", "pollut", "destroy", "destruct", "kill", "threaten",
    "endanger", "harm", "damag", "degrad", "devastat", "poison", "ruin",
    "choke", "strangl", "starv", "displac", "drown", "sicken", "wipe out",
    "decimat", "obliterat", "annihilat", "imperil", "jeopardis", "jeopardiz",
]

# Function words and comparatives that can follow a figure without being the
# thing counted: "29% of what humans emit", "25% higher", "3 times larger".
FUNCTION_WORDS = {
    "what", "who", "whom", "whose", "which", "them", "those", "these", "all",
    "both", "each", "every", "any", "some", "more", "less", "fewer", "higher",
    "lower", "larger", "smaller", "greater", "weaker", "stronger", "faster",
    "slower", "times", "percent", "per", "and", "but", "than", "then", "when",
    "where", "while", "since", "because", "about", "over", "under", "above",
    "below", "between", "during", "within", "across", "into", "onto", "upon",
}

# Words that change what a figure means while leaving the figure intact.
QUALIFIERS = {
    "only", "just", "merely", "barely", "almost", "nearly", "roughly", "about",
    "approximately", "up to", "as many as", "as much as", "at least", "at most",
    "more than", "fewer than", "less than", "over", "under", "some", "many",
}

NON_ADVERB_LY = {
    "family", "supply", "apply", "reply", "assembly", "anomaly", "monopoly",
    "italy", "july", "rally", "ally", "poly", "multiply", "imply", "comply",
}

CAUSAL = {
    "causes", "caused", "causing", "due to", "because", "leads to", "led to",
    "drives", "drove", "driven by", "results in", "resulting in",
    "responsible for", "contributed to", "stems from", "gave rise to",
    "blamed on", "attributable to", "triggers", "triggered",
}

# Academic throat-clearing the scorer strips before a sentence becomes a fact.
PREAMBLE = [
    r"^(here )?we (show|find|report|demonstrate|observe|estimate|document|reveal)( that)?,?\s*",
    r"^our (results?|findings?|data|analysis) (show|suggest|indicate|reveal|demonstrate)( that)?,?\s*",
    r"^(this|the) (study|paper|work|research|analysis) (shows|finds|reports|demonstrates|reveals)( that)?,?\s*",
    r"^(these|the) (results?|findings?) (show|suggest|indicate|reveal)( that)?,?\s*",
    r"^it (was|is) found that\s*",
    r"^(we|researchers) (found|estimated|measured|sampled|analysed|analyzed)( that)?,?\s*",
    r"^(in|according to) (this|the) (study|paper|analysis),?\s*",
    r"^(notably|importantly|significantly|interestingly|crucially),?\s*",
]

FINITE_VERB = r"\b(is|are|was|were|has|have|had|shows?|showed|found|reveal(ed|s)?|report(ed|s)?|record(ed|s)?|exceed(ed|s)?|reach(ed|es)?|remain(ed|s)?|account(ed|s)?|suppl(y|ies|ied)|depend(s|ed)?|cover(s|ed)?|contain(s|ed)?|hold(s)?|rank(s|ed)?|grant(ed|s)?|order(ed|s)?|approv(ed|es)|rul(ed|es)|ban(ned|s)?|fin(ed|es)|process(ed|es)?|var(y|ies|ied)|fall(s|en)?|fell|ros(e|en)|declin(ed|es)|increas(ed|es)|decreas(ed|es)|drop(ped|s)?|grew|shrank|lost|clear(ed|s)|convert(ed|s)|kill(ed|s)|affect(ed|s))\b"

MEASUREMENT_UNITS = r"(%|percent|ha|hectares?|km2|km²|square kilomet|acres?|tonnes?|tons?|kg|mg/l|µg|ppm|ppb|ppt|mm|cm|metres?|meters?|°c|degrees|gt|mt|billion|million|thousand|years?|decades?|species|sites?|wells?|samples?|people|residents?)"


@dataclass
class Fact:
    text: str
    anchor: str
    anchor_index: int
    joined_indices: list = field(default_factory=list)
    fact_type: str = "magnitude"
    scope: str = ""
    engine: str = "scorer"
    validation: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "fact": self.text,
            "anchor": self.anchor,
            "anchor_index": self.anchor_index,
            "joined_indices": self.joined_indices,
            "fact_type": self.fact_type,
            "scope": self.scope,
            "engine": self.engine,
            "validation": self.validation,
        }


# ── Sentence handling ──────────────────────────────────────────────────

ABBREV = (r"(?<!\bDr)(?<!\bMr)(?<!\bMs)(?<!\bSt)(?<!\bFig)(?<!\bNo)(?<!\bvs)"
          r"(?<!\bal)(?<!\be\.g)(?<!\bi\.e)(?<!\bapprox)"
          # Month abbreviations: "Aug. 12" was splitting into two sentences.
          r"(?<!\bJan)(?<!\bFeb)(?<!\bMar)(?<!\bApr)(?<!\bJun)(?<!\bJul)"
          r"(?<!\bAug)(?<!\bSep)(?<!\bSept)(?<!\bOct)(?<!\bNov)(?<!\bDec)"
          r"(?<!\bInc)(?<!\bLtd)(?<!\bCorp)(?<!\bGen)(?<!\bSen)(?<!\bRep)")


def split_sentences(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    # Do not split before a digit: "Aug. 12, Sarawak recorded..." was becoming
    # two sentences, the second beginning "12, Sarawak". A sentence rarely
    # opens with a bare number, and when it does the loss is one row.
    parts = re.split(ABBREV + r"(?<=[.!?])\s+(?=[A-Z“\"'])", text)
    return [p.strip() for p in parts if len(p.strip()) > 25]


def _norm(s):
    s = (s or "").lower()
    s = s.replace("–", "-").replace("—", "-").replace("’", "'")
    s = re.sub(r"[^\w\s%.,°/-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def numbers_in(text):
    """Every numeric token, kept verbatim. 38 must never become 40."""
    return re.findall(r"\d[\d,\.]*", text or "")


def proper_nouns_in(text):
    """
    Capitalized tokens that aren't sentence-initial, plus all-caps acronyms.
    Crude by design — false positives cost us a valid fact, false negatives
    cost us credibility, and we'd rather lose the fact.
    """
    text = text or ""
    # Drop capitals that are capitalised only because they open a sentence.
    # Facts are single sentences so stripping the first word sufficed; context
    # entries run to two or three, and "Clearing" or "Warming" opening the
    # second sentence was being read as a proper noun.
    body = re.sub(r"(^|[.!?—–]\s+|\n)\s*[A-Z][a-zA-Z’'\-]*", " ", text)
    tokens = re.findall(r"\b[A-Z][a-zA-Z’'\-]+\b|\b[A-Z]{2,}\b", body)
    stop = {"The", "This", "These", "Those", "In", "A", "An", "It", "We", "Our", "Their"}
    return [t for t in tokens if t not in stop and len(t) > 2]


# ── Joined-claim rule ──────────────────────────────────────────────────
#
# Two adjacent sentences may be cited together ONLY when the second supplies
# scope — how many, where, when — to a measurement stated in the first.
#
# The distinction the blunt "two measurements" test missed:
#
#   "...38% of 212 wells sampled across the Ogallala Aquifer"
#       imports sample size, place, window        -> permitted
#   "38% of wells IN INTENSIVE MAIZE-GROWING AREAS exceeded..."
#       imports a restrictive condition, asserting the 38% sits inside the
#       maize areas, which neither sentence says  -> refused
#
# So: any content word the fact takes from the second sentence must be a scope
# term, a place, a bare number, or a year. Anything else fails. Conservative on
# purpose — a wrongly-refused fact costs one row, a wrongly-accepted one costs
# the feed's credibility.

SCOPE_VOCAB = {
    "sampled", "sample", "samples", "sampling", "surveyed", "survey", "studied",
    "measured", "monitored", "analysed", "analyzed", "assessed", "examined",
    "collected", "recorded", "tested", "across", "between", "from", "during",
    "throughout", "within", "total", "each", "per", "sites", "site", "wells",
    "well", "plots", "plot", "stations", "station", "locations", "location",
    "participants", "households", "countries", "country", "regions", "region",
    "basins", "basin", "catchments", "watersheds", "years", "year", "months",
    "study", "dataset", "records", "observations", "transects", "quadrats",
    "individuals", "specimens", "and", "of", "the", "in", "at", "to", "a", "an",
    # Geographic feature nouns. These describe WHERE, which is scope. Kept to
    # specific landform and administrative terms — "area" and "zone" are
    # deliberately absent, because "in intensive maize-growing areas" is a
    # condition wearing a location's clothes.
    "floodplain", "basin", "delta", "valley", "watershed", "catchment",
    "aquifer", "estuary", "coast", "coastline", "peninsula", "island",
    "islands", "archipelago", "plateau", "highlands", "lowlands", "range",
    "province", "district", "county", "prefecture", "municipality",
    "territory", "oblast", "canton", "department", "state", "states",
    "reserve", "park", "corridor", "reach", "tributary", "headwaters",
}

STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "with",
    "was", "were", "is", "are", "be", "been", "by", "from", "that", "this",
    "these", "those", "it", "its", "as", "than", "which", "we", "our",
}


def _content_words(text):
    text = _norm(text).replace("-", " ")
    return {w for w in re.findall(r"[a-z]{3,}", text) if w not in STOPWORDS}


def _is_place(word):
    from .tagger import COUNTRY_REGION, FEATURE_REGION
    return any(word in key for key in list(COUNTRY_REGION) + list(FEATURE_REGION))


def _scope_only(fact_text, fact_words, primary, support):
    """
    Everything the fact takes from `support` but not `primary` must be scope.

    Non-initial capitalised tokens are treated as scope because they have
    already cleared the proper-noun rule above — meaning the anchor text
    contains them verbatim. That covers every named river, reserve and district
    without needing them all enumerated in the gazetteer.
    """
    named = {n.lower().strip("'s") for n in proper_nouns_in(fact_text)}
    imported = fact_words & (_content_words(support) - _content_words(primary))
    for word in sorted(imported):
        if word in SCOPE_VOCAB or word in named or _is_place(word):
            continue
        return False, word
    return True, None


RESULT_UNIT_RE = r"(?:%|percent|mg/l|\u00b5g|ug/l|ppm|ppb|ppt|ha\b|hectares?|km2|km\u00b2|acres?|tonnes?|tons?|kg\b|mm\b|cm\b|\u00b0c|degrees|gt\b|mt\b)"


def _assign_roles(fact_text, a, b):
    """Primary = the anchor carrying the fact's result-bearing figure."""
    result_nums = [n for n, _ in re.findall(r"(\d[\d,\.]*)\s*(" + RESULT_UNIT_RE + r")",
                                            fact_text, re.I)]
    for num in result_nums:
        in_a = re.search(r"(?<!\d)" + re.escape(num) + r"(?!\d)", a.replace(",", ""))
        in_b = re.search(r"(?<!\d)" + re.escape(num) + r"(?!\d)", b.replace(",", ""))
        if in_a and not in_b:
            return a, b
        if in_b and not in_a:
            return b, a
    # No result figure to anchor on: primary is whichever shares more with the fact.
    words = _content_words(fact_text)
    return (a, b) if len(words & _content_words(a)) >= len(words & _content_words(b)) else (b, a)


def check_joined(fact_text, sentences, indices):
    i, j = indices
    a, b = sentences[i], sentences[j]
    fact_words = _content_words(fact_text)

    # Which sentence is primary is decided by the data, not by position — the
    # scope sentence is as often first ("we sampled 212 wells...") as second.
    # The primary is whichever supplies the result-bearing figure; everything
    # the fact takes from the other must be scope.
    #
    # Testing BOTH directions and accepting either was too permissive: it let a
    # fact import its finding from one sentence and its agent from the other
    # ("Cattle ranching drove a 14% forest loss"), passing because the leftover
    # imports happened to look like place names.
    primary, support = _assign_roles(fact_text, a, b)
    ok, bad = _scope_only(fact_text, fact_words, primary, support)
    if not ok:
        return False, {"rule": "second_anchor_adds_more_than_scope", "value": bad}

    # Numbers may come from either sentence, but a numeral bound to a result
    # unit in BOTH means two findings were welded into one row.
    result_unit = r"(%|percent|mg/l|µg|ug/l|ppm|ppb|ppt|ha|hectares?|km2|km²|acres?|tonnes?|tons?|kg|mm|cm|°c|degrees|gt|mt)"
    used = numbers_in(fact_text)
    results = [
        s for s in (a, b)
        if any(re.search(re.escape(n) + r"\s*" + result_unit, s, re.I) for n in used)
    ]
    if len(results) == 2:
        return False, {"rule": "merged_two_measurements"}

    return True, {"rule": "joined_ok"}


# ── Validator ──────────────────────────────────────────────────────────

def validate(fact_text, sentences, anchor_indices, interpretive=False):
    """
    Anchor binding. Returns (ok, detail).

    Rules, in the order they fail:
      1. Anchor indices must exist and be adjacent if there are two. Never more.
      2. Every numeral in the fact must appear verbatim in the anchor text.
      3. Every proper noun in the fact must appear in the anchor text.
      4. A joined fact may import only scope from its second sentence —
         sample size, place, date range. It may not merge two measurements,
         and it may not assert a relation absent from both sentences.
      5. Intensifiers must be present in the anchor.
      6. Causal language must be present in the anchor.
    """
    if not anchor_indices:
        return False, {"rule": "anchor_missing"}
    if len(anchor_indices) > 2:
        return False, {"rule": "too_many_anchors", "n": len(anchor_indices)}
    if any(i < 0 or i >= len(sentences) for i in anchor_indices):
        return False, {"rule": "anchor_out_of_range"}
    if len(anchor_indices) == 2 and abs(anchor_indices[0] - anchor_indices[1]) != 1:
        return False, {"rule": "anchors_not_adjacent"}

    anchor_text = " ".join(sentences[i] for i in sorted(anchor_indices))
    a_norm = _norm(anchor_text)
    f_norm = _norm(fact_text)

    a_flat = a_norm.replace(",", "")
    for num in numbers_in(fact_text):
        bare = num.rstrip(".").replace(",", "")
        if not bare:
            continue
        # Word-boundary match. Substring matching let "12" pass on "212".
        if not re.search(r"(?<!\d)" + re.escape(bare) + r"(?!\d)", a_flat):
            return False, {"rule": "number_not_in_anchor", "value": num}

    # A numeral is only as trustworthy as the thing it counts. "38% of wells"
    # is supported; "38% of wells across 12 states" invents the unit even when
    # both digits appear somewhere in the anchor.
    for num, unit in re.findall(r"(\d[\d,\.]*)\s*%?\s*(?:of\s+)?([a-zA-Z][a-zA-Z\-]{2,})", fact_text):
        u = unit.lower().rstrip("s")
        if u in STOPWORDS or u in FUNCTION_WORDS or len(u) < 3:
            continue
        # A participle following a figure is a clause, not a counted noun:
        # "146 killed, bringing the total to 2,253".
        if u.endswith("ing") or u.endswith("ed"):
            continue
        if u not in a_norm and u + "s" not in a_norm:
            return False, {"rule": "counted_unit_not_in_anchor", "value": unit}

    # Words that reframe a number without changing it.
    # Skipped for context entries: those lines exist to interpret, and "sharply"
    # or "only" is the interpretation doing its job. Facts get no such licence.
    for q in (() if interpretive else QUALIFIERS):
        if re.search(r"\b" + re.escape(q) + r"\b", f_norm) and q not in a_norm:
            return False, {"rule": "qualifier_not_in_source", "value": q}

    for noun in proper_nouns_in(fact_text):
        # Compare the bare name: "Amazon's" and "Amazon" are the same place.
        bare = re.sub(r"[’']s$", "", noun.lower())
        if bare not in a_norm and noun.lower() not in a_norm:
            return False, {"rule": "proper_noun_not_in_anchor", "value": noun}

    if len(anchor_indices) == 2:
        ok, detail = check_joined(fact_text, sentences, sorted(anchor_indices))
        if not ok:
            return False, detail

    for word in INTENSIFIERS:
        if re.search(r"\b" + re.escape(word) + r"\b", f_norm) and word not in a_norm:
            return False, {"rule": "intensifier_not_in_source", "value": word}

    for phrase in CAUSAL:
        if phrase in f_norm and phrase not in a_norm:
            return False, {"rule": "causal_claim_not_in_source", "value": phrase}

    for stem in HARM_STEMS:
        if re.search(r"\b" + stem + r"\w*", f_norm) and not re.search(r"\b" + stem, a_norm):
            return False, {"rule": "harm_claim_not_in_source", "value": stem}

    # Added adverbs are the open-class version of the intensifier problem:
    # "secretly granted", "rapidly declining", "only 38%" all assert something
    # the source did not. If the source used the word, it is in the anchor.
    for adverb in ([] if interpretive else re.findall(r"\b[a-z]{4,}ly\b", f_norm)):
        if adverb in NON_ADVERB_LY:
            continue
        if adverb not in a_norm:
            return False, {"rule": "adverb_not_in_source", "value": adverb}

    return True, {"rule": "pass", "anchors": sorted(anchor_indices)}


# ── Tier 4: rule-based scorer ──────────────────────────────────────────

THRESHOLD_VERBS = r"\b(exceed(ed|s)?|fell below|dropped below|surpass(ed|es)?|breach(ed|es)?|first (recorded|documented|detected)|record (high|low)|below the|above the|violat(ed|es|ion))\b"
MECHANISM_WORDS = r"\b(driven by|because|results? in|leads? to|causes?|caused by|prevent(s|ed)?|blocks?|reduc(es|ed|ing)|increas(es|ed|ing) (the )?risk|allows?|enables?|bind(s)? to|accumulat(es|ed))\b"
ORG_WORDS = r"\b(ministry|court|tribunal|agency|commission|company|corporation|government|parliament|council|authority|regulator|Inc|Ltd|SA|PLC|GmbH)\b"
# Direction-of-change verbs. Most magnitude and rate facts are built on one of
# these, and scoring without them rejected "glacier area declined by 18%".
CHANGE_VERBS = r"\b(declin(ed|es|ing)?|fell|fall(en|s)?|drop(ped|s)?|ros(e|en)|rise|risen|increas(ed|es|ing)?|decreas(ed|es|ing)?|grew|grown|shrank|shrunk|shrinking|lost|los(es|ing)|gain(ed|s)?|expand(ed|s|ing)?|contract(ed|s|ing)?|clear(ed|s|ing)?|convert(ed|s|ing)?|remov(ed|es|ing)|halv(ed|es)|doubl(ed|es|ing)?)\b"
# A bare count of things: "87 new leaching pools", "41 coal blocks".
COUNT_PATTERN = r"\d[\d,\.]*\s+(?:\w+\s+){0,2}[a-z]+s\b"


# Technical apparatus: real in the paper, meaningless in a feed. "delta 2H
# depletion of -73 per mille" is a measurement, not a finding anyone can act on.
JARGON = re.compile(
    r"(isotop|per mille|parts per thousand|\bdelta\b|δ|‰|spectro|chromatograph|"
    r"assay|in vitro|in situ|taxa\b|genera\b|morpholog|phylogen|sequenc|"
    r"correlation coefficient|regression|p\s*[<=]\s*0\.|confidence interval|"
    r"standard deviation|proxy record|paleo|holocene|pleistocene|calibrat|"
    r"stoichiometr|enzyme|biomarker|\bPCA\b|\bANOVA\b)", re.I)

# What the feed is actually about. A sentence with a number but none of this
# is a measurement from an unrelated field.
# A subject the feed is about. Without one, "declines in annual mean high-flow
# extreme" and "transient oxygen depletion after the earthquake" both pass a
# verb test while being about nothing a reader can act on.
SUBJECT = re.compile(
    r"(forest|tree|timber|mangrove|peat|soil|land|farm|crop|harvest|"
    r"water|river|lake|aquifer|groundwater|wetland|glacier|snowpack|"
    r"ocean|sea|reef|coral|fish|mammal|bird|insect|pollinator|species|"
    r"wildlife|habitat|air|emission|carbon|methane|mine|mining|oil|gas|coal|"
    r"waste|plastic|chemical|pesticide|metal|community|resident|people|"
    r"village|town|city|indigenous|worker|farmer|company|ministry|court|"
    r"government|agency|permit|licence|license|concession|reserve|park)", re.I)

CONSEQUENCE = re.compile(
    r"(pollut|contaminat|deforest|logging|mining|spill|emission|degrad|"
    r"extinct|endangered|habitat loss|overfish|toxic|pesticid|waste|"
    r"drought|flood|wildfire|erosion|deplet|decline|loss|lost|destroy|"
    r"threaten|exceed|violat|banned|fine[sd]?\b|permit|concession|"
    r"displac|evict|land rights|indigenous|protected area|conservation)", re.I)


def score_sentence(sentence):
    s = 0
    # A finding with no bearing on environmental harm does not belong in the
    # feed however well it scores on numbers.
    if not (CONSEQUENCE.search(sentence) and SUBJECT.search(sentence)):
        s -= 8
    if JARGON.search(sentence):
        s -= 7
    if re.search(r"\d[\d,\.]*\s*" + MEASUREMENT_UNITS, sentence, re.I):
        s += 4
    elif re.search(r"\d[\d,\.]*", sentence):
        s += 1
    if re.search(THRESHOLD_VERBS, sentence, re.I):
        s += 3
    if re.search(CHANGE_VERBS, sentence, re.I):
        s += 2
    if re.search(COUNT_PATTERN, sentence):
        s += 2
    if re.search(MECHANISM_WORDS, sentence, re.I):
        s += 2
    if re.search(ORG_WORDS, sentence, re.I):
        s += 2
    # No bonus for consequence-plus-subject: tried, and it admitted far more
    # weak sentences than good ones. It stays a hard gate only.
    if proper_nouns_in(sentence):
        s += 2
    # Feed boilerplate: section headers, "Recommended", bylines, promos.
    if re.match(r"^\s*(Recommended|Related|Read more|Advertisement|Share this|"
                r"Sign up|Subscribe|Follow us|Photo|Image|Caption)\b", sentence, re.I):
        s -= 8
    # A sentence that is mostly a person's name and title is a byline.
    if re.match(r"^[A-Z][a-z]+ [A-Z][a-z]+( [A-Z][a-z]+)?,\s+(a|an|the|who)\b",
                sentence):
        s -= 5
    # Methods description rather than result.
    if re.match(r"^\W*(\w+\s+){0,3}(measurements?|samples?|data|observations?|records?|surveys?|specimens?)\s+(from|of|across|were|are)\b", sentence, re.I):
        s -= 5
    # Penalise sentences that describe the study rather than its finding.
    if re.search(r"\b(we (aim|sought|set out)|this (study|paper) (aims?|examines?|investigat)|future (work|research)|further (study|research) is needed|little is known)\b", sentence, re.I):
        s -= 6
    if len(sentence) > 420:
        s -= 2
    return s


def strip_preamble(sentence):
    out = sentence
    for pattern in PREAMBLE:
        out = re.sub(pattern, "", out, flags=re.I)
    out = out.strip()
    return out[0].upper() + out[1:] if out else out


# Tiers the sentence scorer may handle.
#
# Journalism and institutional prose is written to be read by non-specialists,
# so a well-chosen sentence stands alone. A research abstract is not: its
# sentences are written for people who already know the field, and selecting
# one yields "delta-2H depletion down to -73 per mille" or "2,014 individuals
# in 46 species, 25 genera". No amount of filtering turns that into a fact a
# reader can use, because the usable sentence is not in the document.
#
# So peer-reviewed and preprint documents produce a row only when a model
# handles them. Where quota does not reach, they produce nothing — which is
# honest, and better than filling the feed with apparatus.
SCORER_TIERS = {"journalism", "institutional"}


def scorer_extract(doc, sentences):
    """Tier 4. Selects rather than writes, so anchor binding holds by construction."""
    if doc.get("tier") not in SCORER_TIERS:
        return []
    from .tagger import detect_topics
    # Article-level relevance is not sentence-level relevance. Require the
    # chosen sentence to carry an environmental topic on its own.
    require_topic = True
    if not sentences:
        return []
    ranked = sorted(((score_sentence(s), i, s) for i, s in enumerate(sentences)),
                    reverse=True)
    facts = []
    for score, idx, sentence in ranked[:3]:
        if score < 5:
            break
        text = strip_preamble(sentence)
        if len(text) < 30:
            continue
        if require_topic and not detect_topics(text):
            continue
        # Hard gate, not a penalty. A corporate results line, a senator's
        # remarks, a staffing shortage — all carry topic words and figures
        # without being about environmental harm.
        if not (CONSEQUENCE.search(text) and SUBJECT.search(text)):
            continue
        # Financial reporting reads as environmental when the company is a
        # miner. Revenue, share price and profit are not the subject here.
        # Plurals matter here: \brevenue\b cannot match "revenues", which is
        # how "mining company said revenues rose 46.7 percent" kept shipping.
        if re.search(r"\b(revenue|profit|earning|turnover|dividend|"
                     r"share price|market cap|sales|creditor|debt|"
                     r"shareholder|investor|valuation|IPO|acquisition)s?\b|"
                     r"\bshares? (rose|fell|gained|dropped)\b", text, re.I):
            continue
        # Sentences that are chiefly a named person speaking. A senator listing
        # hardships is a quote, not a finding.
        if re.search(r"^[A-Z][\w.'-]+ ([A-Z][\w.'-]+ )?[A-Z][\w.'-]+ "
                     r"(said|says|told|listed|added|noted|argued|claimed|urged)\b",
                     text):
            continue
        # A sentence starting mid-number or lowercase is a split artefact:
        # "12, Sarawak's Tebedu district" is the tail of "Aug. 12".
        if re.match(r"^[\d,;:)\]]|^[a-z]", text):
            continue
        # Stripping preamble can leave a verbless noun phrase. A fact states
        # that something is the case; a noun phrase only names a subject.
        if not (re.search(FINITE_VERB, text, re.I)
                or re.search(r"\b\w{3,}(ed|ing|s)\b\s+(the|a|an|to|into|from|by|"
                             r"in|on|at|with|over|under|\d)", text, re.I)):
            continue
        # Skip a sentence that mostly restates one already taken.
        if any(len(set(text.lower().split()) & set(f.text.lower().split())) >
               0.6 * len(text.split()) for f in facts):
            continue
        facts.append(Fact(text=text, anchor=sentence, anchor_index=idx,
                          fact_type=classify(text), engine="scorer",
                          validation={"rule": "selection", "score": score}))
        if len(facts) == 2:
            break
    return facts


def classify(text):
    low = text.lower()
    if re.search(r"\b(per year|annually|since \d{4}|between \d{4}|over the (past|last)|rate of|a day|per day)\b", low):
        return "rate"
    if re.search(THRESHOLD_VERBS, low):
        return "threshold"
    if re.search(r"\b(will|expected to|projected|by 20\d\d|under.{0,20}scenario|if .{0,40}continues?)\b", low):
        return "projection"
    if re.search(ORG_WORDS, text) and re.search(r"\b(granted|approved|ordered|suspended|ruled|fined|banned|revoked|signed|rejected)\b", low):
        return "actor"
    if re.search(MECHANISM_WORDS, low):
        return "attribution"
    if re.search(r"\b(per year|annually|since \d{4}|between \d{4}|over the (past|last)|rate of)\b", low):
        return "rate"
    return "magnitude"


# ── Tiers 1–3: free-tier model providers ───────────────────────────────

# ── Model discovery and pacing ─────────────────────────────────────────
#
# Two lessons from the first live run with keys.
#
# 1. Hard-coded model names rot. gemini-2.5-flash returned "no longer
#    available" and llama-3.3-70b "does not exist or you do not have access".
#    Both were correct names when written. Ask each provider what it serves
#    now instead of guessing, so a retirement costs nothing.
#
# 2. Free tiers cap requests per MINUTE, not just per day. Firing 746
#    documents as fast as the network allows hit Groq's limit almost
#    immediately. Each provider now gets a minimum interval between calls.

# Starting intervals. These adapt at runtime: every 429 doubles the interval
# for that provider, every twenty clean calls eases it back down. A generous
# tier runs fast; a tight one throttles itself without a code change.
MIN_INTERVAL = {
    "gemini": float(os.environ.get("GEMINI_INTERVAL", "1.5")),
    "groq": float(os.environ.get("GROQ_INTERVAL", "2")),
    "mistral": float(os.environ.get("MISTRAL_INTERVAL", "1")),
    "github": float(os.environ.get("GITHUB_INTERVAL", "2")),
    "cerebras": float(os.environ.get("CEREBRAS_INTERVAL", "2")),
}
MAX_INTERVAL = 30.0
_clean_run = {}


def slow_down(name):
    MIN_INTERVAL[name] = min(MAX_INTERVAL, MIN_INTERVAL.get(name, 2) * 2)
    _clean_run[name] = 0
    print(f"  [{name}] backing off to {MIN_INTERVAL[name]:.0f}s between calls")


def speed_up(name):
    _clean_run[name] = _clean_run.get(name, 0) + 1
    if _clean_run[name] >= 20 and MIN_INTERVAL.get(name, 2) > 1:
        MIN_INTERVAL[name] = max(1.0, MIN_INTERVAL[name] / 1.5)
        _clean_run[name] = 0
_last_call = {}
_model_cache = {}

# Preference order within whatever each provider currently offers. Small, fast
# models are the right choice here: the task is pulling one sentence out of an
# abstract, not reasoning.
MODEL_PREFERENCE = {
    "gemini":   ["flash-lite", "flash"],
    "groq":     ["8b-instant", "8b", "70b-versatile", "70b"],
    "mistral":  ["ministral-3b", "ministral-8b", "mistral-small", "小"],
    "github":   ["4o-mini", "mini", "8b", "small"],
    "cerebras": ["llama.*8b", "llama.*70b", "llama"],
}

# OpenAI-compatible providers. Gemini has its own shape and is handled apart.
OPENAI_COMPATIBLE = {
    "groq":     ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    # Mistral's free tier allows a very large monthly token budget, which suits
    # this workload better than the per-day request caps elsewhere.
    "mistral":  ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    # GitHub Models runs on the token Actions already provides, so the
    # scheduled run needs no extra secret at all.
    "github":   ("https://models.github.ai/inference", "GITHUB_TOKEN"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
}


def _pace(name):
    wait = MIN_INTERVAL.get(name, 2.0) - (time.time() - _last_call.get(name, 0))
    if wait > 0:
        time.sleep(wait)
    _last_call[name] = time.time()


def _version(name):
    """Highest version number in the name — 'gemini-3.0-flash' beats 2.0."""
    nums = [float(m) for m in re.findall(r"(\d+\.\d+)", name)]
    return max(nums) if nums else 0.0


def _pick(names, provider):
    for want in MODEL_PREFERENCE.get(provider, []):
        matches = [n for n in names if re.search(want, n.lower())]
        if matches:
            # Newest generation first: a legacy flash-lite still appears in the
            # list long after its free allowance is withdrawn.
            return sorted(matches, key=_version, reverse=True)[0]
    # Nothing matched the preference list. Take what is offered rather than
    # retiring a provider that works — Cerebras serves no llama model and was
    # being written off entirely. A paid-only model then fails fast with 402,
    # which is treated as permanent.
    return names[0] if names else None


def discover_model(provider):
    """Ask the provider what it currently serves. Cached for the run."""
    if provider in _model_cache:
        return _model_cache[provider]

    override = os.environ.get(f"{provider.upper()}_MODEL")
    if override:
        _model_cache[provider] = override
        return override

    names = []
    try:
        if provider == "gemini":
            r = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")},
                timeout=TIMEOUT)
            if r.status_code == 200:
                names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                         if "generateContent" in m.get("supportedGenerationMethods", [])]
        else:
            base, key_env = OPENAI_COMPATIBLE[provider]
            key = os.environ.get(key_env, "")
            cat = ("https://models.github.ai/catalog/models" if provider == "github"
                   else f"{base}/models")
            r = requests.get(cat, headers={"Authorization": f"Bearer {key}"},
                             timeout=TIMEOUT)
            if r.status_code == 200:
                body = r.json()
                items = body.get("data") if isinstance(body, dict) else body
                names = [m.get("id") or m.get("name") for m in (items or [])
                         if m.get("id") or m.get("name")]
    except Exception:
        names = []

    # Exclude anything that is not a general text model.
    names = [n for n in names if not re.search(
        r"embed|tts|whisper|guard|vision|image|audio|rerank", n, re.I)]
    if not names and provider == "github":
        # Catalogue lookup is unreliable; these are GitHub Models' documented
        # small models. A wrong name fails fast with 404 and retires the tier.
        names = ["openai/gpt-4o-mini", "openai/gpt-4.1-mini",
                 "mistral-ai/ministral-3b"]
    picked = _pick(names, provider)
    _model_cache[provider] = picked
    if picked:
        print(f"  [{provider}] using {picked}")
    return picked


PROMPT = """You extract standalone facts from environmental source documents for a public feed.

You will receive numbered sentences from one document. Return the 1-3 most important standalone facts, or an empty list if the document contains none.

A fact qualifies ONLY if it has a subject AND at least one of: a quantity, a named actor or place, a stated mechanism, or a threshold crossed. A statement that names only a topic does not qualify. These do NOT qualify: "biodiversity loss is accelerating", "a new study examines microplastics", "the report calls for urgent action", "groups raised concerns", "deforestation increased last year".

HARD RULES — a fact violating any of these is worthless to us:
- Every number in your fact must appear EXACTLY as written in the anchor sentence. Never round. 38 must not become 40 or "nearly 40".
- Every place, organisation or proper name in your fact must appear in the anchor sentence.
- You may cite one sentence, or two ADJACENT sentences. With two, the second may supply only scope (sample size, place, date range). Never merge two separate measurements, and never assert a relationship that neither sentence states.
- Do not add evaluative words the source did not use: devastating, alarming, catastrophic, unprecedented, severe, poisoning, collapse.
- Do not add causation the source did not state. "Concentrations were highest in maize areas" is not "maize farming causes contamination".
- If the source names no specific place, say the actual scope instead ("across 84 countries", "in every sample tested", "worldwide"). Generic is acceptable; vague is not.

STYLE: plain declarative English. Strip academic preamble ("we show that", "our results indicate"). Lead with what is happening, not who studied it. One fact per entry — if it needs a subordinate clause, give it one; if it needs two facts, split it. No length target: end when the fact is complete.

fact_type is one of: magnitude, rate, attribution, threshold, actor, projection.

{language_note}

Return ONLY a JSON array. No prose, no markdown fences:
[{{"fact": "...", "anchors": [2], "fact_type": "magnitude", "scope": "..."}}]

SENTENCES:
{sentences}"""


# Function words that appear in almost any English sentence of usable length.
_EN_MARKERS = re.compile(
    r"\b(the|and|of|to|in|a|is|was|were|has|have|that|for|with|from|by|"
    r"at|on|as|are|been|which|its|their)\b", re.I)


def _looks_english(text):
    """Cheap check that a returned fact is English, not the source language."""
    hits = len(_EN_MARKERS.findall(text))
    words = max(1, len(text.split()))
    return hits / words >= 0.12


def build_prompt(sentences, lang):
    numbered = "\n".join(f"[{i}] {s}" for i, s in enumerate(sentences))
    note = ("The sentences are not in English. Write the fact in English; "
            "the anchor stays in the original language, so numbers and proper "
            "names must still match the original exactly."
            if lang != "en" else "")
    return PROMPT.format(sentences=numbered, language_note=note)


def _parse_json(raw):
    """
    Recover a JSON array from a model reply.

    Small models are loose about format: they wrap the array in prose, fence it,
    use single quotes, or emit one bare object instead of a list. Mistral's 3B
    produced 25 consecutive unusable replies before this.
    """
    raw = (raw or "").strip()
    raw = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()

    for candidate in (re.search(r"\[.*\]", raw, re.S),
                      re.search(r"\{.*\}", raw, re.S)):
        if not candidate:
            continue
        text = candidate.group(0)
        for attempt in (text,
                        re.sub(r"(?<![\w\\])'([^']*)'(?=\s*[:,\]}])", r'"\1"', text),
                        re.sub(r",\s*([\]}])", r"\1", text)):
            try:
                parsed = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            return parsed if isinstance(parsed, list) else [parsed]
    return []


def call_gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    model = discover_model("gemini")
    if not model:
        raise RuntimeError("gemini: no usable model returned by the API")
    _pace("gemini")
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.1, "maxOutputTokens": 900}},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"gemini {r.status_code}: {r.text[:160]}")
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _openai_compatible(prompt, base, key_env, provider):
    key = os.environ.get(key_env)
    if not key:
        return None
    model = discover_model(provider)
    if not model:
        raise RuntimeError(f"{provider}: no usable model returned by the API")
    _pace(provider)
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model,
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.1, "max_tokens": 900},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{base} {r.status_code}: {r.text[:160]}")
    return r.json()["choices"][0]["message"]["content"]


def _make_caller(provider):
    base, key_env = OPENAI_COMPATIBLE[provider]
    return lambda prompt: _openai_compatible(prompt, base, key_env, provider)


# Order matters: best output first, then whichever free tier has room.
# Cerebras is last because its free tier does not cover a usable model on
# every account — it returns 402 and retires itself at a cost of one call.
# GitHub Models answered 410 "github_models_retirement_brownout": the service
# is being retired, so it is out of the cascade rather than failing every run.
_TIERS = ["groq", "mistral"]
if os.environ.get("ENABLE_GITHUB_MODELS") == "1":
    _TIERS.append("github")
if os.environ.get("ENABLE_CEREBRAS") == "1":
    # Free tier does not cover a usable model on every account; it answers 402
    # and retires itself, so it stays off unless asked for.
    _TIERS.append("cerebras")

CASCADE = [("gemini", call_gemini)] + [
    (name, _make_caller(name)) for name in _TIERS
]


class Budget:
    """
    Tracks failures per provider, and productivity.

    A tier that returns 200 OK with unusable output never trips the failure
    count, so it can silently consume the entire budget — which is what
    Mistral did across 250 calls for four facts.
    """

    """
    Tracks failures per provider. Three consecutive failures (quota, outage,
    revoked terms) retires that tier for the run and the cascade moves on.
    The feed never goes dark because a free tier changed its mind.
    """

    def __init__(self, limit=3):
        self.limit = limit
        self.failures = {}
        self.barren = {}          # consecutive calls returning nothing usable
        self.BARREN_LIMIT = 25

    def alive(self, name):
        return (self.failures.get(name, 0) < self.limit
                and self.barren.get(name, 0) < self.BARREN_LIMIT)

    def yielded(self, name, got):
        """Record whether a successful call actually produced a usable fact."""
        if got:
            self.barren[name] = 0
            return
        self.barren[name] = self.barren.get(name, 0) + 1
        if self.barren[name] == self.BARREN_LIMIT:
            print(f"  [{name}] retired for this run: responded {self.BARREN_LIMIT}"
                  f" times without a usable extraction")

    def any_alive(self):
        """True while some provider can still be tried."""
        return any(self.alive(n) for n, _ in CASCADE)

    def fail(self, name, reason):
        permanent = re.search(
            r"\b(401|402|403|invalid|not valid|unauthori|wrong api key|payment)\b",
            reason, re.I)
        # Network trouble is not the provider's verdict. Count it at a
        # fraction of a strike so a bad minute cannot bench a working tier for
        # a whole run, while a genuinely unreachable host still retires.
        network = re.search(
            r"(timeout|timed out|connection|read timed|reset by peer|"
            r"nameresolution|max retries|ssl)", reason, re.I)
        if permanent:
            self.failures[name] = self.limit
        else:
            self.failures[name] = self.failures.get(name, 0) + (0.34 if network else 1)
        if self.failures[name] >= self.limit:
            kind = ("bad key, or the model requires payment" if permanent
                    else "repeated network failures" if network
                    else "quota or repeated errors")
            print(f"  [{name}] retired for this run ({kind}): {reason[:110]}")


def extract(doc, budget):
    """Run the cascade for one document. Returns a list of validated Facts."""
    text = f"{doc.get('title', '')}. {doc.get('body', '')}"
    sentences = split_sentences(text)
    if len(sentences) < 2:
        return []

    prompt = build_prompt(sentences[:24], doc.get("lang", "en"))

    for name, call in CASCADE:
        if not budget.alive(name):
            continue
        try:
            raw = call(prompt)
        except Exception as exc:
            msg = str(exc)
            timed_out = re.search(r"(timeout|timed out|read timed)", msg, re.I)
            if timed_out and budget.alive(name):
                time.sleep(3)
                try:
                    raw = call(prompt)
                except Exception as exc2:
                    budget.fail(name, str(exc2)[:120])
                    continue
            elif "429" in msg and budget.alive(name):
                slow_down(name)
                time.sleep(8)
                try:
                    raw = call(prompt)
                except Exception as exc2:
                    budget.fail(name, str(exc2)[:120])
                    continue
            else:
                budget.fail(name, msg[:120])
                continue
        if raw is None:
            continue

        facts = []
        for cand in _parse_json(raw)[:3]:
            fact_text = (cand.get("fact") or "").strip()
            anchors = cand.get("anchors") or []
            if not fact_text or not anchors:
                continue
            ok, detail = validate(fact_text, sentences, anchors)
            if not ok:
                continue
            # A non-English document must come back translated. Reporterre rows
            # shipped as "Les incendies dans les monts d'Arrée ont été éteints".
            if doc.get("lang", "en") != "en" and not _looks_english(fact_text):
                continue
            facts.append(Fact(
                text=fact_text,
                anchor=" ".join(sentences[i] for i in sorted(anchors)),
                anchor_index=sorted(anchors)[0],
                joined_indices=sorted(anchors) if len(anchors) > 1 else [],
                fact_type=cand.get("fact_type") or classify(fact_text),
                scope=cand.get("scope", ""),
                engine=name,
                validation=detail,
            ))
        budget.yielded(name, bool(facts))
        if facts:
            speed_up(name)
            return facts
        # Model responded but produced nothing valid — try the next tier.

    return scorer_extract(doc, sentences)
