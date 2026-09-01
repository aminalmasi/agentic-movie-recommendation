"""Warm the cache from a URL list, optionally across several worker processes.

READ THIS BEFORE SETTING -j 8.

Against a single IP, more workers buy you NOTHING. The 3s spacing is a
deliberate bottleneck and the shared rate budget enforces it no matter how many
processes you start -- eight workers against letterboxd.com finish in exactly
the same wall-clock as one, they just spend more of it blocked. A single worker
is already ~93% idle (0.3s of network per 4.5s of sleep); there is no latency
left to hide.

Workers help in exactly two situations:

  1. MIXED HOSTS. The budget is per-host, so letterboxd.com, api.trakt.tv and
     datasets.imdbws.com each get their own. A work list spanning three hosts
     genuinely runs three-wide.
  2. SEVERAL EXITS. Distinct IPs are distinct budgets. That means off-cluster
     runners or proxy exits, not more processes.

Sharding is by sha256(url) so workers own disjoint sets and never duplicate a
fetch. Do NOT shard by user -- users share films, and you would refetch them.

    python scripts/crawl.py urls.txt -j 3            # only sensible if mixed hosts
    python scripts/crawl.py urls.txt --dry-run       # what would be fetched
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import multiprocessing as mp
import os
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lbfetch import Cache
from lbfetch.fetch import Fetcher


def shard_of(url: str, n: int) -> int:
    return int(hashlib.sha256(url.encode()).hexdigest(), 16) % n


def _worker(args):
    urls, shard, cache_dir, proxy, quiet = args
    logging.basicConfig(level=logging.WARNING if quiet else logging.INFO,
                        format=f"[w{shard}] %(levelname)-7s %(message)s")
    done = Counter()
    with Fetcher(cache_dir=cache_dir, proxy_url=proxy) as f:
        for i, url in enumerate(urls, 1):
            entry = f.get(url)
            done["ok" if entry.ok else f"http_{entry.status}"] += 1
            done["from_cache"] += 1 if entry.from_cache else 0
            if i % 25 == 0:
                logging.info("%d/%d", i, len(urls))
    return dict(done)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("urlfile", help="one URL per line; # comments allowed")
    ap.add_argument("-j", "--jobs", type=int, default=1)
    ap.add_argument("--cache-dir", default="cache")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    with open(args.urlfile) as fh:
        urls = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    urls = list(dict.fromkeys(urls))          # de-dup, preserve order

    cache = Cache(args.cache_dir)
    todo = [u for u in urls if cache.get(u) is None]
    hosts = Counter(urlsplit(u).netloc for u in todo)

    print(f"{len(urls)} urls, {len(urls) - len(todo)} already cached, {len(todo)} to fetch")
    for host, n in hosts.most_common():
        print(f"  {host:28s} {n:>6d}")

    if len(hosts) == 1 and args.jobs > 1:
        print(f"\nNOTE: all {len(todo)} urls are on one host. -j {args.jobs} will not "
              f"make this faster -- the shared rate budget is per-host and it is "
              f"already saturated by one worker. Running anyway.")

    # ~4.5s per request per host, hosts proceed in parallel
    eta = max((n * 4.5 for n in hosts.values()), default=0)
    print(f"\nestimated wall clock: {eta/60:.1f} min ({eta/3600:.1f} h)")
    if args.dry_run:
        return 0

    t0 = time.time()
    if args.jobs == 1:
        results = [_worker((todo, 0, args.cache_dir, args.proxy, args.quiet))]
    else:
        shards = [[u for u in todo if shard_of(u, args.jobs) == s]
                  for s in range(args.jobs)]
        with mp.Pool(args.jobs) as pool:
            results = pool.map(_worker, [
                (shards[s], s, args.cache_dir, args.proxy, args.quiet)
                for s in range(args.jobs)])

    total = Counter()
    for r in results:
        total.update(r)
    print(f"\ndone in {(time.time()-t0)/60:.1f} min")
    for k, v in sorted(total.items()):
        print(f"  {k:14s} {v}")
    print(f"  cache now: {cache.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
