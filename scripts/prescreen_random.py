"""Sample a large random population and cheaply screen it for eligible targets.

Eligibility needs: >=5 friends, >=50 rated films, >=8 recently-dated ratings.
Checking those naively costs ~10 pages per candidate. Checking them in order of
cost-per-unit-of-elimination costs ~2:

  1. /following/  ONE page, kills ~70% (most Letterboxd users follow nobody)
  2. /rss/        ONE page, gives the dated-recent count AND a rating sample
  3. /films/ p1   ONE page, 72 films, enough to see if >=50 are rated

The frame is deliberately broad: members of many films across decades and
genres, rather than one taste cluster. It is still not a uniform sample of
Letterboxd -- no such thing is obtainable -- but it is much wider than sampling
from one user's neighbourhood.
"""
from __future__ import annotations
import argparse, json, logging, os, random, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_people_page, parse_films_page, parse_diary_rss

log = logging.getLogger("prescreen")
BASE = "https://letterboxd.com"
NAME = re.compile(r'href="/([a-z0-9_]+)/" class="name"')

# Deliberately spread across era, language and genre so the frame is not one
# taste cluster. All are films with large enough crowds to page through.
FRAME = ["parasite-2019", "the-godfather", "spirited-away", "mad-max-fury-road",
         "lady-bird", "get-out-2017", "la-la-land", "whiplash-2014",
         "the-grand-budapest-hotel", "interstellar-2014", "moonlight-2016",
         "portrait-of-a-lady-on-fire", "everything-everywhere-all-at-once",
         "the-shining", "alien", "before-sunrise", "amelie", "oldboy",
         "in-the-mood-for-love", "seven-samurai"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--want", type=int, default=100, help="eligible targets wanted")
    ap.add_argument("--pool-pages", type=int, default=3, help="pages per frame film")
    ap.add_argument("--min-friends", type=int, default=5)
    ap.add_argument("--min-rated", type=int, default=50)
    ap.add_argument("--min-recent", type=int, default=8)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", default="data/random100.json")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    rng = random.Random(a.seed)

    eligible, seen, screened = {}, set(), 0
    with Fetcher() as f:
        pool = []
        for slug in FRAME:
            for stars in ("4", "4%C2%BD", "5"):
                for pg in range(1, a.pool_pages + 1):
                    sfx = "" if pg == 1 else f"page/{pg}/"
                    e = f.get(f"{BASE}/film/{slug}/members/rated/{stars}/{sfx}")
                    if not e.ok: break
                    names = set(NAME.findall(e.body))
                    if not names: break
                    pool += [n for n in names if n not in seen]
                    seen |= names
            log.info("frame %-34s pool=%d", slug, len(pool))
        rng.shuffle(pool)
        log.info("candidate pool: %d distinct users", len(pool))

        for u in pool:
            if len(eligible) >= a.want: break
            screened += 1
            # 1. cheapest, most selective
            e = f.get(paths.user_following(u))
            if not e.ok: continue
            friends = [(p.username, p.n_films or 0) for p in parse_people_page(e.body)
                       if p.username]
            if len(friends) < a.min_friends: continue
            # 2. dated recents
            e = f.get(paths.user_rss(u))
            if not e.ok: continue
            rec = len([d for d in parse_diary_rss(e.body, u).entries if d.rating is not None])
            if rec < a.min_recent: continue
            # 3. rated volume
            e = f.get(paths.user_films(u, 1))
            if not e.ok: continue
            b = parse_films_page(e.body)
            n_rated_p1 = sum(1 for x in b if x.rating is not None)
            if n_rated_p1 < min(a.min_rated, 50): continue
            friends.sort(key=lambda x: -x[1])
            keep = friends[:15]
            eligible[u] = {"friends": [x[0] for x in keep],
                           "friend_films": [x[1] for x in keep],
                           "friend_pages": sum(min(-(-n // 72), 8) for _, n in keep)}
            if len(eligible) % 10 == 0:
                log.info("%d eligible from %d screened (%.0f%%)",
                         len(eligible), screened, 100*len(eligible)/screened)

    pages = sum(v["friend_pages"] for v in eligible.values())
    print(f"\n{len(eligible)} eligible from {screened} screened "
          f"({100*len(eligible)/max(screened,1):.0f}% rate)")
    print(f"  friend history pages needed: {pages:,} = {pages*5/3600:.1f} h")
    json.dump({"targets": eligible}, open(a.out, "w"), indent=1)
    print(f"  wrote {a.out}")


if __name__ == "__main__":
    raise SystemExit(main())
