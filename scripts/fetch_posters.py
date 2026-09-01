"""Download film posters and store them base64, ready to inline.

Two reasons this is a separate step rather than a URL in the page:

  1. The poster is not in the films-grid HTML at all -- every <img> there is an
     `empty-poster` placeholder and the real one is lazy-loaded. It has to come
     from each film page's JSON-LD.
  2. A published artifact runs under a strict CSP that blocks every external
     host, so a remote <img src> renders as nothing. The bytes must be inlined
     as data: URIs.

Posters come from a.ltrbxd.com, a CDN on a different host from letterboxd.com,
so it gets its own rate budget -- hence the separate faster Fetcher.
"""
from __future__ import annotations
import argparse, base64, json, logging, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_poster_url

ap = argparse.ArgumentParser()
ap.add_argument("user")
ap.add_argument("--width", type=int, default=125)
ap.add_argument("--height", type=int, default=187)
a = ap.parse_args()
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

films = json.load(open(f"data/{a.user}_films_full.json"))["films"]

# Incremental: keep what we already have. Letterboxd serves the poster URL in
# two shapes -- /resized/film-poster/... and /resized/sm/upload/... -- and an
# earlier run matched only the first, silently dropping 27% of films. Re-running
# should top up, not start over.
path = f"data/posters_{a.user}.json"
out = json.load(open(path)) if os.path.exists(path) else {}
missing = []
logging.info("already have %d posters", len(out))

from curl_cffi import requests as creq
cdn = creq.Session(impersonate="chrome", timeout=40)

with Fetcher() as site:                       # polite, letterboxd.com
    for i, f in enumerate(films, 1):
        if f["slug"] in out:
            continue
        e = site.get(paths.film(f["slug"]))
        url = parse_poster_url(e.body, a.width, a.height) if e.ok else None
        if not url:
            missing.append(f["slug"]); continue
        try:
            r = cdn.get(url)
            if r.status_code == 200 and r.content:
                out[f["slug"]] = base64.b64encode(r.content).decode()
        except Exception as exc:
            logging.warning("%s: %s", f["slug"], type(exc).__name__)
            missing.append(f["slug"])
        if i % 25 == 0:
            logging.info("%d/%d  (%d posters, %d missing)", i, len(films), len(out), len(missing))

json.dump(out, open(path, "w"))
mb = sum(len(v) for v in out.values())/1e6
print(f"\nwrote {path}: {len(out)} posters, {mb:.1f} MB base64, {len(missing)} missing")
if missing: print("missing:", ", ".join(missing[:8]))
