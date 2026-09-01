"""Generate a seed-picker page from a user's watched log + histograms.

Choosing seeds by hand from a 170-row table is the wrong interface: the cost of
a seed spans four orders of magnitude (4 pages for one film, 206,493 for
another) and nothing on Letterboxd shows you that. The page makes the tradeoff
visible -- pick films, watch the crawl budget move, stop when it hits your
ceiling.

    python scripts/make_picker.py amindoalamas --out picker.html
"""
from __future__ import annotations
import argparse, json, math, os, sys

ap = argparse.ArgumentParser()
ap.add_argument("user")
ap.add_argument("--out", default=None)
ap.add_argument("--seconds-per-page", type=float, default=4.5)
ap.add_argument("--kb-per-page", type=float, default=22.7)
ap.add_argument("--only-usable", action="store_true",
                help="only films whose rater pool is enumerable AND big enough to "
                     "intersect -- the only ones that can be seeds. Cuts the page "
                     "from ~2.6MB to ~0.5MB, which matters: 170 embedded posters is "
                     "more than the artifact viewer wants to parse.")
ap.add_argument("--min-pool", type=int, default=120)
ap.add_argument("--max-pool", type=int, default=60000)
a = ap.parse_args()

src = f"data/{a.user}_films_full.json"
films = json.load(open(src))["films"]
corpus = max(sum(f["lb_total"] for f in films), 1)
for f in films:
    f["idf"] = round(math.log(corpus / max(f["lb_total"], 1)), 2)
if a.only_usable:
    before = len(films)
    films = [f for f in films if a.min_pool <= f["raters_ge4"] <= a.max_pool]
    print(f"only-usable: {len(films)} of {before} films can be seeds")
films.sort(key=lambda f: f["pages_ge4"])

posters = {}
pp = f"data/posters_{a.user}.json"
if os.path.exists(pp):
    posters = json.load(open(pp))
    print(f"embedding {len(posters)} posters ({sum(map(len,posters.values()))/1e6:.1f} MB)")

payload = json.dumps([{
    "slug": f["slug"], "title": f.get("title") or f["slug"], "year": f.get("year"),
    "rating": f.get("rating"), "total": f["lb_total"], "pool": f["raters_ge4"],
    "pages": f["pages_ge4"], "pagesAll": f["pages_all"], "idf": f["idf"],
    "p": posters.get(f["slug"], ""),
} for f in films], separators=(",", ":"))

TPL = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "picker_template.html")).read()
html = (TPL.replace("__USER__", a.user)
           .replace("__FILMS__", payload)
           .replace("__SPP__", str(a.seconds_per_page))
           .replace("__KBPP__", str(a.kb_per_page)))
out = a.out or f"picker_{a.user}.html"
open(out, "w").write(html)
print(f"wrote {out}  ({len(films)} films)")
