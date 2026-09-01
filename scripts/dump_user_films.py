"""Pull a user's complete watched-films list with ratings, and write it out.

`/{user}/films/` is paginated at 72 films per page and, unlike the RSS feed,
is not capped -- this is the route to a full history rather than a recent
slice. What it does NOT carry: TMDB ids, watch dates, and hearts. Those come
from the RSS feed (recent tail) or the account CSV export (everything), and the
slug -> TMDB bridge comes from each film page, cached once and reused across
every user you ever process.

    python scripts/dump_user_films.py amindoalamas --out data/amindoalamas.json

Pagination stops when a page yields no films. `--max-pages` is a seatbelt, not
a target: it exists so a markup change that breaks the parser cannot turn into
an unbounded crawl.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_films_page, rating_profile


def fetch_all_films(f: Fetcher, user: str, max_pages: int = 60, refresh: bool = False):
    films, page = [], 1
    while page <= max_pages:
        entry = f.get(paths.user_films(user, page), refresh=refresh)
        if not entry.ok:
            logging.warning("page %d: HTTP %d via %s -- stopping",
                            page, entry.status, entry.transport)
            break
        batch = parse_films_page(entry.body)
        if not batch:
            break
        films.extend(batch)
        logging.info("page %d: %d films (%d rated), running total %d",
                     page, len(batch), sum(1 for b in batch if b.rating is not None),
                     len(films))
        page += 1
    else:
        logging.warning("hit --max-pages=%d; history may be truncated", max_pages)
    return films


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("user")
    ap.add_argument("--out", default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    with Fetcher(proxy_url=args.proxy) as f:
        films = fetch_all_films(f, args.user, args.max_pages, args.refresh)

    rated = [x for x in films if x.rating is not None]
    prof = rating_profile(films)

    print(f"\n{args.user}: {len(films)} films watched, {len(rated)} rated "
          f"({len(rated) / max(1, len(films)):.0%})")
    print(f"  mean {prof.get('mean', 0):.2f}   sd {prof.get('sd', 0):.2f}   "
          f"modal share {prof.get('modal_share', 0):.0%}   "
          f"entropy {prof.get('entropy_norm', 0):.2f}   "
          f"half-stars {'yes' if prof.get('uses_half_stars') else 'no'}")
    print(f"  usable for personalisation: "
          f"{'yes' if prof['usable'] else 'NO -- ' + prof['reason']}")

    if rated:
        from collections import Counter
        hist = Counter(x.rating for x in rated)
        print("\n  rating histogram")
        for stars in [i / 2 for i in range(1, 11)]:
            n = hist.get(stars, 0)
            bar = "#" * round(40 * n / max(hist.values()))
            print(f"    {stars:>3.1f}  {n:>4d}  {bar}")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump({"user": args.user, "profile": prof,
                       "films": [asdict(x) for x in films]}, fh, indent=1)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
