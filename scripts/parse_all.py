"""Parse every cached page in parallel. This is where -j actually pays.

Fetching is rate-limited and cannot be parallelised against one IP. Parsing is
pure CPU over local files and scales with cores. Once the cache is warm, this is
the part worth throwing processors at -- and it is safe to re-run at any time,
since it never touches the network.

    python scripts/parse_all.py -j 8 --out data/parsed.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import multiprocessing as mp
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lbfetch.parse import (parse_films_page, parse_people_page,
                           parse_reviews_page, parse_diary_rss, parse_film_ids)


def classify_and_parse(path: str):
    """Route one cached entry to the right parser by its URL shape."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception:
        return ("unreadable", None, 0)
    url, body = rec.get("url", ""), rec.get("body") or ""
    if not (200 <= rec.get("status", 0) < 300):
        return ("non200", url, 0)

    if "/rss/" in url:
        return ("rss", url, len(parse_diary_rss(body).entries))
    if "/reviews/" in url:
        return ("reviews", url, len(parse_reviews_page(body)))
    if "/following/" in url or "/followers/" in url:
        return ("people", url, len(parse_people_page(body)))
    if "/films/" in url:
        return ("films", url, len(parse_films_page(body)))
    if "/film/" in url:
        ids = parse_film_ids(body)
        return ("film_ids", url, 1 if ids.get("tmdb_id") else 0)
    return ("other", url, 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--cache-dir", default="cache")
    args = ap.parse_args()

    paths = []
    for root, _, files in os.walk(args.cache_dir):
        paths.extend(os.path.join(root, f) for f in files if f.endswith(".json.gz"))
    print(f"{len(paths)} cached pages, {args.jobs} workers")

    import time
    t0 = time.time()
    with mp.Pool(args.jobs) as pool:
        results = pool.map(classify_and_parse, paths, chunksize=8)

    kinds, items = Counter(), Counter()
    for kind, _url, n in results:
        kinds[kind] += 1
        items[kind] += n
    print(f"parsed in {time.time()-t0:.1f}s\n")
    print(f"  {'kind':12s} {'pages':>7s} {'records':>9s}")
    for kind in sorted(kinds):
        print(f"  {kind:12s} {kinds[kind]:>7d} {items[kind]:>9d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
