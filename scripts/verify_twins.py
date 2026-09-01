"""Stage 2: rank twin candidates by AGREEMENT, not by co-occurrence.

Stage 1 (find_twins.py) answers "who else loved the same rare films?". That is
recruitment, and it is a weak ranker -- sharing two obscure films is thin
evidence and the score ties hundreds of people. Stage 2 answers the real
question: over EVERY film you have both rated, do your opinions track?

METHOD, following Criticker's shipped algorithm (~20 years in production):
compare PERCENTILE RANK within each user's own scale, never raw stars, "because
a rating of 75 can mean something different for each user". Your 3.21 mean and
their 4.2 mean are not comparable numbers; your relative ordering is.

    agreement = Pearson r over co-rated films, each rating replaced by its
                percentile within that user's own distribution

THREE OUTPUTS, because similarity has a sign and a confidence:
  twin        r >= +0.5 with enough overlap -- their opinion predicts yours
  mirror      r <= -0.4 -- they reliably INVERT you, which is real evidence with
              the sign flipped, and most systems throw it away
  unrelated   |r| small, or too few co-rated films to say

SEED EXCLUSION IS NOT OPTIONAL. These people were recruited BECAUSE they loved
your seed films. Scoring agreement on those same films measures the recruitment,
not their taste -- it would inflate every r toward +1. They are dropped here and
must also be dropped from any downstream evaluation.

    python scripts/verify_twins.py amindoalamas --top 300 --out data/twins_verified.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_diary_rss, parse_films_page

log = logging.getLogger("verify")


def percentiles(ratings: dict) -> dict:
    """Map each film -> the user's own percentile rank for that rating.

    Midranks for ties, so a user who gives fifty 4.0s does not get fifty
    different percentiles.
    """
    vals = sorted(ratings.values())
    n = len(vals)
    if n < 2:
        return {}
    # midrank of each distinct value
    rank = {}
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[i]:
            j += 1
        rank[vals[i]] = (i + j) / 2.0
        i = j + 1
    return {f: rank[r] / max(n - 1, 1) for f, r in ratings.items()}


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    return num / (dx * dy) if dx and dy else None


def fetch_history(f: Fetcher, user: str, deep: bool, max_pages: int = 60) -> dict:
    """-> {slug: rating}. RSS first (5.5KB for 50 ratings, the cheapest source
    per rating we measured); paginate /films/ only when asked."""
    out = {}
    e = f.get(paths.user_rss(user))
    if e.ok:
        for d in parse_diary_rss(e.body, user).entries:
            if d.rating is not None and d.slug:
                out[d.slug] = d.rating
    if deep:
        for page in range(1, max_pages + 1):
            e = f.get(paths.user_films(user, page))
            if not e.ok:
                break
            batch = parse_films_page(e.body)
            if not batch:
                break
            for x in batch:
                if x.rating is not None:
                    out.setdefault(x.slug, x.rating)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("user")
    ap.add_argument("--twins", default=None, help="find_twins.py output")
    ap.add_argument("--top", type=int, default=300, help="candidates to verify")
    ap.add_argument("--nseeds-eq", type=int, default=None,
                    help="CONTROL GROUP: verify candidates sharing EXACTLY this many "
                         "seeds, sampled at random. n=1 is the null -- people who "
                         "loved one of your rare films by coincidence. If they yield "
                         "the same twin rate as the n>=4 group, co-occurrence is noise.")
    ap.add_argument("--sample-seed", type=int, default=0, help="RNG seed, for reproducibility")
    ap.add_argument("--min-common", type=int, default=8,
                    help="co-rated films required before reporting an r")
    ap.add_argument("--deep", action="store_true",
                    help="paginate /films/ as well as RSS (5x the requests)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    tw = json.load(open(args.twins or f"data/twins.json"))
    seeds = {s["slug"] for s in tw["seeds"]}
    if args.nseeds_eq is not None:
        import random
        pool = [c for c in tw["candidates"] if c.get("n_seeds", 1) == args.nseeds_eq]
        random.Random(args.sample_seed).shuffle(pool)
        cands = pool[:args.top]
        log.info("CONTROL: %d candidates sampled at random from %d sharing exactly "
                 "%d seed(s)", len(cands), len(pool), args.nseeds_eq)
    else:
        cands = sorted(tw["candidates"],
                       key=lambda c: (-c.get("n_seeds", 1), -c["score"]))[:args.top]
    log.info("%d candidates, excluding %d seed films from every comparison",
             len(cands), len(seeds))

    me_hist = {f["slug"]: f["rating"]
               for f in json.load(open(f"data/{args.user}.json"))["films"]
               if f["rating"] is not None}
    me_pct = percentiles(me_hist)

    rows = []
    with Fetcher() as f:
        for i, c in enumerate(cands, 1):
            u = c["username"]
            # RSS is 50 ratings; a stranger's recent 50 almost never overlaps a
            # 109-film history by enough to fit a correlation. Measured 2026-08-28:
            # RSS-only gave >=6 co-rated films for 1 of 264 candidates. --deep is
            # not an optimisation here, it is the difference between a result and
            # a table of "insufficient".
            hist = fetch_history(f, u, args.deep)
            if not hist:
                continue
            their_pct = percentiles(hist)
            common = [s for s in me_pct.keys() & their_pct.keys() if s not in seeds]
            if len(common) < args.min_common:
                rows.append({"username": u, "n_common": len(common), "r": None,
                             "n_seeds": c.get("n_seeds", 1), "verdict": "insufficient"})
                continue
            r = pearson([me_pct[s] for s in common], [their_pct[s] for s in common])
            verdict = ("twin" if r is not None and r >= 0.5 else
                       "mirror" if r is not None and r <= -0.4 else "unrelated")
            rows.append({"username": u, "n_common": len(common), "r": r,
                         "n_seeds": c.get("n_seeds", 1), "n_rated": len(hist),
                         "verdict": verdict, "common": sorted(common)[:20]})
            if i % 25 == 0:
                log.info("%d/%d verified", i, len(cands))

    scored = [r for r in rows if r["r"] is not None]
    scored.sort(key=lambda r: -r["r"])
    from collections import Counter
    print(f"\n{len(rows)} candidates fetched; {len(scored)} had >= {args.min_common} "
          f"co-rated films (seeds excluded)")
    print("  verdicts:", dict(Counter(r["verdict"] for r in rows)))

    mirrors = [r for r in scored if r["r"] <= -0.4]
    for label, sel in (("TWINS", [r for r in scored if r["r"] >= 0.5][:15]),
                       ("MIRRORS", sorted(mirrors, key=lambda r: r["r"])[:10])):
        if not sel:
            print(f"\n{label}: none")
            continue
        print(f"\n{label}")
        print(f"  {'user':22s} {'r':>6s} {'common':>7s} {'seeds':>6s} {'theirN':>7s}")
        print("  " + "-" * 56)
        for r in sel:
            print(f"  {r['username']:22s} {r['r']:>6.2f} {r['n_common']:>7d} "
                  f"{r['n_seeds']:>6d} {r.get('n_rated',0):>7d}")

    if args.out:
        json.dump({"user": args.user, "min_common": args.min_common,
                   "excluded_seeds": sorted(seeds), "results": rows},
                  open(args.out, "w"), indent=1)
        print(f"\nwrote {args.out}")

    print("\nNOTE: RSS caps at ~50 ratings, so `common` is bounded by THEIR recent 50\n"
          "unless you pass --deep. A low n_common means 'not enough evidence', not\n"
          "'not similar'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
