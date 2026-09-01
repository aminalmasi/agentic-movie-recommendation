"""What does a real Letterboxd RSS feed actually contain, and is this user usable?

The RSS field set is undocumented -- it is whatever Letterboxd's template emits,
and it has changed before. Before building features on <letterboxd:memberLike>
or <letterboxd:watchedDate>, confirm they are there, on accounts you did not
pick for convenience.

    python scripts/probe_rss.py someuser anotheruser

Prints per-field coverage and the rating-distribution gate. A user who fails the
gate is not a bug -- plenty of people log films without rating them, and the
system has to detect that rather than quietly fit noise to it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_diary_rss, rating_profile


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("users", nargs="+")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    all_fields: set = set()
    with Fetcher(proxy_url=args.proxy) as f:
        results = []
        for user in args.users:
            entry = f.get(paths.user_rss(user), refresh=args.refresh)
            if not entry.ok:
                print(f"\n{user}: HTTP {entry.status} via {entry.transport} -- skipped")
                continue
            feed = parse_diary_rss(entry.body, user=user)
            prof = rating_profile(feed.entries)
            all_fields |= set(feed.field_coverage)
            results.append((user, feed, prof, entry))

    if not results:
        print("\nnothing fetched. Run scripts/probe_paths.py first.")
        return 1

    print(f"\n{'user':18s} {'items':>5s} {'skip':>4s} {'rated':>5s} "
          f"{'mean':>5s} {'sd':>5s} {'modal':>5s} {'H':>5s} {'½':>2s}  usable")
    print("-" * 78)
    for user, feed, prof, _ in results:
        print(f"{user:18s} {len(feed.entries):>5d} {feed.skipped:>4d} "
              f"{prof['n_rated']:>5d} {prof.get('mean', 0):>5.2f} "
              f"{prof.get('sd', 0):>5.2f} {prof.get('modal_share', 0):>5.2f} "
              f"{prof.get('entropy_norm', 0):>5.2f} "
              f"{'y' if prof.get('uses_half_stars') else 'n':>2s}  "
              f"{'yes' if prof['usable'] else 'NO -- ' + prof['reason']}")

    print(f"\n{'field':18s} " + " ".join(f"{u[:10]:>10s}" for u, _, _, _ in results))
    print("-" * (19 + 11 * len(results)))
    for fname in sorted(all_fields):
        row = " ".join(f"{feed.field_coverage.get(fname, 0.0):>10.2f}"
                       for _, feed, _, _ in results)
        print(f"{fname:18s} {row}")

    missing = [f for f in ("rating", "watched_date", "tmdb_id")
               if all(feed.field_coverage.get(f, 0) < 0.5 for _, feed, _, _ in results)]
    if missing:
        print(f"\nWARNING: {', '.join(missing)} present in <50% of items across every "
              f"account probed. The feed shape may have changed -- check lbfetch/parse.py "
              f"against the raw XML before trusting an ingest.")

    print("\nNB: RSS caps at ~100 items. These numbers describe a recent slice, not a "
          "full history -- 'usable' here is a first look, not the final gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
