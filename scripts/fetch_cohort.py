"""Fetch every cohort friend's history, so each target can be evaluated.

Long and boring on purpose: one page every 5 s, cache-first, resumable. The page
cache means an interrupted run costs nothing to restart -- fetched pages are
never re-requested.
"""
from __future__ import annotations
import argparse, json, logging, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_films_page

ap = argparse.ArgumentParser()
ap.add_argument("--cohort", default="data/cohort_final.json")
ap.add_argument("--max-pages", type=int, default=12)
ap.add_argument("--shard", type=int, default=0)
ap.add_argument("--shards", type=int, default=1,
                help="split the friend list across runners. Each GitHub runner "
                     "gets its own proxy exit, so shards are genuinely parallel "
                     "-- unlike processes on one machine, which share one budget.")
a = ap.parse_args()
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("fetch")

coh = json.load(open(a.cohort))["targets"]
todo = sorted({fr for v in coh.values() for fr in v["friends"]})
if a.shards > 1:
    import hashlib
    todo = [u for u in todo
            if int(hashlib.sha256(u.encode()).hexdigest(), 16) % a.shards == a.shard]
    log.info("shard %d/%d -> %d friends", a.shard, a.shards, len(todo))
log.info("%d targets, %d distinct friends to fetch", len(coh), len(todo))

done = 0; t0 = time.time()
with Fetcher() as f:
    for i, u in enumerate(todo, 1):
        f.get(paths.user_rss(u))
        for pg in range(1, a.max_pages + 1):
            e = f.get(paths.user_films(u, pg))
            if not e.ok: break
            if not parse_films_page(e.body): break
            done += 1
        if i % 25 == 0:
            el = time.time() - t0
            log.info("%d/%d friends | %d pages | %.1f min elapsed | eta %.1f h",
                     i, len(todo), done, el/60, (len(todo)-i)*(el/max(i,1))/3600)
print(f"\nfetched histories for {len(todo)} friends across {len(coh)} targets")
