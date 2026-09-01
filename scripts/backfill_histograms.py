"""Fetch the rating histogram for every film in a user's watched list.

The histogram is what makes a seed choosable: it gives the exact rater count at
each star level, which is simultaneously the cost of enumerating that film's
raters and the informativeness of sharing it. Cached forever, and shared across
every user who has seen the same film.
"""
from __future__ import annotations
import argparse, json, logging, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_rating_histogram

ap = argparse.ArgumentParser()
ap.add_argument("user")
ap.add_argument("--out", default=None)
a = ap.parse_args()
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

films = json.load(open(f"data/{a.user}.json"))["films"]
out = []
with Fetcher() as f:
    for i, x in enumerate(films, 1):
        e = f.get(paths.film_rating_histogram(x["slug"]))
        h = parse_rating_histogram(e.body) if e.ok else {}
        x = dict(x)
        x["lb_total"] = sum(h.values())
        x["hist"] = {str(k): v for k, v in sorted(h.items())}
        x["raters_ge4"] = sum(n for s, n in h.items() if s >= 4.0)
        x["pages_ge4"] = -(-x["raters_ge4"] // 25)
        x["pages_all"] = -(-x["lb_total"] // 25)
        out.append(x)
        if i % 20 == 0:
            logging.info("%d/%d", i, len(films))

path = a.out or f"data/{a.user}_films_full.json"
json.dump({"user": a.user, "films": out}, open(path, "w"), indent=1)
print(f"wrote {path}  ({len(out)} films)")
