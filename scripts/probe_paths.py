"""Which Letterboxd paths does *this* machine reach, and by which transport?

Reported behaviour from someone else's IP is not evidence about yours.
Cloudflare decides per-network, and a cluster exit, a residential proxy and a
laptop on home broadband get three different answers. Run this once before
building anything, run it again whenever things start failing, and paste the
table into lbfetch/paths.py with a date.

    python scripts/probe_paths.py --user <letterboxd-user> --film parasite-2019

Add --proxy to route through the residential exit (or set JOBTOOLS_PROXY_URL).
Add --no-browser to see the plain-transport picture on its own.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lbfetch import paths
from lbfetch.fetch import Fetcher


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="any public Letterboxd username")
    ap.add_argument("--film", default="parasite-2019", help="any film slug")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="ignore cached results")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    targets = [
        ("user_rss", paths.user_rss(args.user)),
        ("film", paths.film(args.film)),
        ("film_rating_histogram", paths.film_rating_histogram(args.film)),
        ("film_reviews", paths.film_reviews(args.film)),
        ("user_films", paths.user_films(args.user)),
        ("user_films p2", paths.user_films(args.user, 2)),
        ("user_following", paths.user_following(args.user)),
        ("friends_on_film", paths.friends_on_film(args.user, args.film)),
    ]

    print(f"\nprobing as: {'proxy' if (args.proxy or os.environ.get('JOBTOOLS_PROXY_URL')) else 'direct'}"
          f"  browser={'off' if args.no_browser else 'on'}\n")
    print(f"{'label':24s} {'status':>6s}  {'bytes':>8s}  transport")
    print("-" * 68)

    rows = []
    with Fetcher(proxy_url=args.proxy, allow_browser=not args.no_browser) as f:
        for label, url in targets:
            entry = f.get(url, refresh=args.refresh)
            src = "cache" if entry.from_cache else entry.transport
            print(f"{label:24s} {entry.status:>6d}  {len(entry.body or ''):>8d}  {src}")
            rows.append((label, entry))

    print("\n--- paste into lbfetch/paths.py TRANSPORT_HINTS ---")
    stale = []
    for label, entry in rows:
        # A cache hit records the transport that fetched it, which may have been
        # a forced browser run. That says nothing about the CHEAPEST transport
        # that works now, so never emit a verdict from one.
        if entry.from_cache:
            verdict, note = "?", "  # stale: cached, re-run with --refresh"
            stale.append(label)
        elif entry.transport == "curl_cffi" and entry.ok:
            verdict, note = "http", ""
        elif entry.ok:
            verdict, note = "browser", ""
        else:
            verdict, note = f"BLOCKED({entry.status})", ""
        print(f'    "{label.replace(" ", "_")}": "{verdict}",{note}')
    if stale:
        print(f"\n{len(stale)} row(s) served from cache and cannot be scored: "
              f"{', '.join(stale)}")
        print("Re-run with --refresh for a clean map.")

    blocked = [l for l, e in rows if not e.ok]
    if blocked:
        print(f"\nunreachable here: {', '.join(blocked)}")
        print("If friends_on_film is in that list, fall back to per-friend RSS + a")
        print("local join on tmdb_id -- see the docstring in paths.friends_on_film.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
