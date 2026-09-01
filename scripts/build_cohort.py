"""Assemble a cohort of TARGET users so results generalise past n=1.

Every finding so far is about one person. This fetches, for each target: their
own history, their follow list, and their friends' histories -- everything
predict_eval.py needs -- so the friends-CF-vs-baselines comparison can be run
per user and aggregated.

Twin-finding is deliberately NOT part of this. It cost 9 hours per user and the
controls showed it adds nothing; friends are the signal worth measuring at scale.
"""
from __future__ import annotations
import argparse, json, logging, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_people_page, parse_films_page, parse_diary_rss

log = logging.getLogger("cohort")

ap = argparse.ArgumentParser()
ap.add_argument("--sample", default="data/sample_random.json")
ap.add_argument("--min-rated", type=int, default=50)
ap.add_argument("--min-recent", type=int, default=8)
ap.add_argument("--max-friends", type=int, default=15,
                help="per target, keep the N friends with the largest histories -- "
                     "a 40-film friend can never carry a usable correlation")
ap.add_argument("--max-friend-pages", type=int, default=12)
ap.add_argument("--survey-only", action="store_true")
ap.add_argument("--out", default="data/cohort.json")
a = ap.parse_args()
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

targets = [x["username"] for x in json.load(open(a.sample))["candidates"]]
cohort = {}
with Fetcher() as f:
    # 1. which targets qualify (histories are already cached)
    good = []
    for u in targets:
        n, pg = 0, 1
        while pg <= 60:
            e = f.get(paths.user_films(u, pg))
            if not e.ok: break
            b = parse_films_page(e.body)
            if not b: break
            n += sum(1 for x in b if x.rating is not None); pg += 1
        e = f.get(paths.user_rss(u))
        rec = len([d for d in parse_diary_rss(e.body, u).entries
                   if d.rating is not None]) if e.ok else 0
        if n >= a.min_rated and rec >= a.min_recent:
            good.append(u)
    log.info("%d/%d targets qualify", len(good), len(targets))

    # 2. their follow lists
    total_friend_pages = 0
    for i, u in enumerate(good, 1):
        friends = []
        for pg in (1, 2):
            e = f.get(paths.user_following(u, pg))
            if not e.ok: break
            b = parse_people_page(e.body)
            if not b: break
            friends += [(p.username, p.n_films or 0) for p in b if p.username]
        friends.sort(key=lambda x: -x[1])
        keep = friends[:a.max_friends]
        pages = sum(min(-(-n // 72), a.max_friend_pages) for _, n in keep)
        total_friend_pages += pages
        cohort[u] = {"friends": [x[0] for x in keep],
                     "friend_films": [x[1] for x in keep], "friend_pages": pages}
        if i % 10 == 0:
            log.info("%d/%d follow lists, running friend-page total %d",
                     i, len(good), total_friend_pages)

print(f"\ncohort: {len(cohort)} targets")
import statistics as st
fc = [len(v["friends"]) for v in cohort.values()]
print(f"  friends kept per target: median {st.median(fc):.0f}, min {min(fc)}, max {max(fc)}")
print(f"  friend history pages to fetch: {total_friend_pages:,}")
print(f"  = {total_friend_pages*5/3600:.1f} h at 5 s/page")
json.dump({"targets": cohort}, open(a.out, "w"), indent=1)
print(f"\nwrote {a.out}")
