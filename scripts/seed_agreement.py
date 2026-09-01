"""Stage 1.5: how much does each candidate agree with YOU on the seed films?

Free. Costs zero requests. When stage 1 enumerated `/members/rated/4/`,
`/rated/4%C2%BD/` and `/rated/5/` as separate paginated series, it learned each
candidate's EXACT rating on every seed they appeared in -- the rating is encoded
in which URL series the page came from. find_twins.py recorded only WHICH seeds
matched, not the value. This re-derives the values straight from the page cache.

WHAT IT CAN AND CANNOT MEASURE. Everyone in these pools rated the seed >= 4.0 by
construction, so this is not "do they like the same films" -- that was the
selection criterion. It measures INTENSITY agreement: given that you both liked
it, did you like it to the same degree? You rated these seeds 3.5-4.5; a
candidate who gives 5.0 to a film you gave 3.5 is enthusiastic in a different
direction from one who gives exactly 4.0.

Two scores per candidate:
  mean_gap    mean(their_rating - your_rating) over shared seeds. Signed, so it
              separates "rates everything higher than me" from real disagreement.
  agree_rate  fraction of shared seeds within 0.5 stars of your rating.

Both are restricted to seeds you actually RATED -- two of the fourteen got in on
a heart alone, and there is no number to compare against there.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NAME_RE = re.compile(r'href="/([a-z0-9_]+)/" class="name"')
URL_RE = re.compile(r'/film/([^/]+)/members/rated/([^/]+)/')
# URL token -> stars. %C2%BD is the percent-encoded half-star glyph.
TOKEN = {"4": 4.0, "4%C2%BD": 4.5, "5": 5.0, "3%C2%BD": 3.5, "5%C2%BD": 5.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("user")
    ap.add_argument("--twins", default="data/twins.json")
    ap.add_argument("--cache-dir", default="cache")
    ap.add_argument("--min-shared", type=int, default=2)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tw = json.load(open(args.twins))
    seeds = {s["slug"] for s in tw["seeds"]}

    mine = {f["slug"]: f["rating"]
            for f in json.load(open(f"data/{args.user}.json"))["films"]
            if f["rating"] is not None}
    rated_seeds = {s: mine[s] for s in seeds if s in mine}
    print(f"{len(seeds)} seeds, {len(rated_seeds)} of them you actually rated "
          f"(the rest got in on a heart)\n")

    # (candidate, seed) -> their rating, straight from the cached pages
    ratings = defaultdict(dict)
    pages = 0
    for root, _, files in os.walk(args.cache_dir):
        for fn in files:
            if not fn.endswith(".json.gz"):
                continue
            path = os.path.join(root, fn)
            try:
                with gzip.open(path, "rt") as fh:
                    rec = json.load(fh)
            except Exception:
                continue
            m = URL_RE.search(rec.get("url", ""))
            if not m or m.group(1) not in seeds:
                continue
            stars = TOKEN.get(m.group(2))
            if stars is None:
                continue
            pages += 1
            for name in set(NAME_RE.findall(rec.get("body") or "")):
                if name != args.user:
                    ratings[name][m.group(1)] = stars

    print(f"re-derived {sum(len(v) for v in ratings.values()):,} (user, film, rating) "
          f"triples from {pages:,} cached pages -- zero new requests\n")

    rows = []
    for user, rs in ratings.items():
        shared = [(s, r) for s, r in rs.items() if s in rated_seeds]
        if len(shared) < args.min_shared:
            continue
        gaps = [r - rated_seeds[s] for s, r in shared]
        rows.append({
            "username": user,
            "n_shared": len(shared),
            "mean_gap": round(sum(gaps) / len(gaps), 3),
            "agree_rate": round(sum(1 for g in gaps if abs(g) <= 0.5) / len(gaps), 3),
            "their_mean": round(sum(r for _, r in shared) / len(shared), 3),
            "seeds": {s: r for s, r in sorted(shared)},
        })

    rows.sort(key=lambda r: (-r["n_shared"], -r["agree_rate"], abs(r["mean_gap"])))
    print(f"{len(rows):,} candidates share >= {args.min_shared} RATED seeds with you\n")
    print(f"{'candidate':22s} {'shared':>6s} {'agree':>6s} {'mean gap':>9s} {'their mean':>11s}")
    print("-" * 62)
    for r in rows[:30]:
        print(f"{r['username']:22s} {r['n_shared']:>6d} {r['agree_rate']:>6.0%} "
              f"{r['mean_gap']:>+9.2f} {r['their_mean']:>11.2f}")

    import statistics as st
    if rows:
        print(f"\npopulation: mean gap {st.mean(r['mean_gap'] for r in rows):+.2f}, "
              f"mean agree-rate {st.mean(r['agree_rate'] for r in rows):.0%}")
        print("A positive gap across the board means these pools skew more "
              "enthusiastic\nthan you, which is expected: they were selected for "
              "rating these films >= 4.")

    if args.out:
        json.dump({"user": args.user, "your_seed_ratings": rated_seeds,
                   "candidates": rows}, open(args.out, "w"), indent=1)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
