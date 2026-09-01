"""Who does this user follow (or who follows them), and how much have they watched?

The films-watched count is the reason to run this before anything else: a
friend-agreement weighting needs each friend's rating history, and fetching a
2,400-film account costs 34 pages while a 40-film account costs one and can
never carry a useful weight anyway. Sort by that count and spend requests where
the overlap can actually exist.

    python scripts/dump_following.py amindoalamas --out data/following.json
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
from lbfetch.parse import parse_people_page


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("user")
    ap.add_argument("--followers", action="store_true", help="followers instead of following")
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument("--out", default=None)
    ap.add_argument("--proxy", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    builder = paths.user_followers if args.followers else paths.user_following

    people, page = [], 1
    with Fetcher(proxy_url=args.proxy) as f:
        while page <= args.max_pages:
            entry = f.get(builder(args.user, page))
            if not entry.ok:
                logging.warning("page %d: HTTP %d -- stopping", page, entry.status)
                break
            batch = parse_people_page(entry.body)
            if not batch:
                break
            people.extend(batch)
            logging.info("page %d: %d people (total %d)", page, len(batch), len(people))
            page += 1

    label = "followers" if args.followers else "following"
    people.sort(key=lambda p: p.n_films or 0, reverse=True)
    print(f"\n{args.user} {label}: {len(people)}")
    total = sum(p.n_films or 0 for p in people)
    print(f"  combined films watched: {total:,}   "
          f"(~{sum(-(-(p.n_films or 0) // 72) for p in people)} page requests to fetch them all)")
    print(f"\n  {'username':22s} {'films':>7s} {'followers':>10s}")
    for p in people[:20]:
        print(f"  {p.username:22s} {str(p.n_films or '?'):>7s} {str(p.n_followers or '?'):>10s}")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump({"user": args.user, "relation": label,
                       "people": [asdict(p) for p in people]}, fh, indent=1)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
