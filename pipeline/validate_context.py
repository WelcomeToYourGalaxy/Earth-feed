"""
Anchor-bind the context library, the same way facts are anchor-bound.

The gap this closes: facts are checked mechanically. Every fact carries the
source sentence it came from, and a validator refuses number drift, invented
places, added causation, added harm verbs and added intensifiers before
anything publishes. The context library had none of that. It was hand-written
prose pointing at a URL, trusted because someone said so — which is how a
claim that mangroves are cleared faster than any other forest survived into
it while the cited source says the reverse.

Hand-correcting 59 entries fixes 59 entries. This makes the library subject to
the same rule as everything else, so the 60th cannot repeat the failure.

Each entry gains an `anchor`: the sentence from its source that carries the
claim. Validation then runs the fact validator over it. An entry whose numbers
or proper nouns do not appear in its own anchor cannot be marked verified, and
build_feed will not attach it.

    python -m pipeline.validate_context          # report
    python -m pipeline.validate_context --strict # exit 1 on any failure
"""

import json
import sys
from pathlib import Path

from .extract import proper_nouns_in, validate

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "context_library.json"


def check(entry):
    """Return (state, detail). States: ok | no_anchor | failed | unverified."""
    anchor = (entry.get("anchor") or "").strip()
    if not anchor:
        return "no_anchor", "no anchor sentence recorded from the source"

    # Treat the whole anchor passage as one block of evidence.
    #
    # A fact cites one sentence, or two adjacent ones where the second supplies
    # only scope — that restriction stops a figure from one sentence being
    # welded to a condition from another. A context entry is different: the
    # passage quoted from the source IS the evidence, and drawing on all of it
    # is the point. Splitting it re-imposed the joined-claim rule and rejected
    # entries whose only offence was using two sentences of their own citation.
    ok, detail = validate(entry["text"], [anchor], [0], interpretive=True)
    if not ok:
        return "failed", f"{detail.get('rule')}" + (
            f" [{detail.get('value')}]" if detail.get("value") else "")
    # What the anchor check CANNOT catch: a bare comparative with no numbers
    # and no proper nouns. "Cleared faster than any other forest type" has
    # nothing to bind against, and that is exactly the claim that got through
    # by hand. Flag those for human reading rather than passing them silently.
    import re as _re
    comparative = _re.search(
        r"\b(faster|slower|more|less|greater|higher|lower|worst|largest|"
        r"smallest|fastest|most|least|than any|than all)\b", entry["text"], _re.I)
    has_binding = _re.search(r"\d", entry["text"]) or proper_nouns_in(entry["text"])
    if comparative and not has_binding:
        return "unbindable", ("comparative claim with no figure or name to check "
                              "— read it against the source yourself")

    if not entry.get("verified"):
        return "unverified", "anchor holds; not yet marked verified"
    return "ok", "anchor holds"


def run(strict=False):
    lib = json.loads(LIB.read_text())
    buckets = {"ok": [], "failed": [], "unbindable": [],
               "no_anchor": [], "unverified": []}

    for entry in lib["entries"]:
        state, detail = check(entry)
        buckets[state].append((entry["id"], detail))

    for state, label in [("failed", "FAILED — text not supported by its anchor"),
                         ("unbindable", "UNBINDABLE — needs a human read"),
                         ("no_anchor", "NO ANCHOR — cannot be checked"),
                         ("unverified", "anchor holds, awaiting verified: true"),
                         ("ok", "verified and anchor-bound")]:
        items = buckets[state]
        if not items:
            continue
        print(f"\n{label}  ({len(items)})")
        for eid, detail in items:
            print(f"  {eid:<26} {detail}")

    usable = len(buckets["ok"])
    print(f"\n{usable}/{len(lib['entries'])} entries publishable")
    if strict and (buckets["failed"] or buckets["no_anchor"]
                   or buckets["unbindable"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run(strict="--strict" in sys.argv))
