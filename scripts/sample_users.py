"""Sample comparison populations, so hypotheses have a denominator.

Every result so far is n=1 user. To claim anything we need to compare the target
against populations drawn on a KNOWN basis. Three arms, increasing in how much
they should resemble the target if the social/taste hypothesis is true:

  random    people who rated a POPULAR film the target also rated. As close to
            "a general Letterboxd cinephile" as is obtainable -- there is no
            uniform sample of Letterboxd users, and this is the honest
            approximation. Shares exactly one MAINSTREAM film with the target.
  fof       the target's friends, and their friends (one edge further out).
            Real social ties, which is the one signal measured to work
            (friends: corr +0.483 on this user).
  rare      shares one OBSCURE film -- the existing n_seeds==1 control.

Compared against the treatment (shares many rare films), these give the gradient
that answers the open question: is a 6% twin rate evidence of anything, or just
the base rate at which any two Letterboxd users correlate?

Output is in find_twins.py's schema so it drops straight into verify_twins.py.
"""

from __future__ import annotations

import argparse, json, logging, os, random, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_people_page

log = logging.getLogger("sample")
BASE = "https://letterboxd.com"
NAME_RE = re.compile(r'href="/([a-z0-9_]+)/" class="name"')
_TOKEN = {4.0: "4", 4.5: "4%C2%BD", 5.0: "5"}


def sample_random(f: Fetcher, films, n, pages_per_film, rng):
    """Users who rated a popular film >= 4. Broad, and honestly biased: they
    are people active enough to rate, on films popular enough to have crowds."""
    pool = set()
    for slug in films:
        for stars in (4.0, 4.5, 5.0):
            for pg in range(1, pages_per_film + 1):
                sfx = "" if pg == 1 else f"page/{pg}/"
                e = f.get(f"{BASE}/film/{slug}/members/rated/{_TOKEN[stars]}/{sfx}")
                if not e.ok:
                    break
                names = set(NAME_RE.findall(e.body))
                if not names:
                    break
                pool |= names
        log.info("%s -> pool %d", slug, len(pool))
    pool = sorted(pool)
    rng.shuffle(pool)
    return pool[:n]


def sample_fof(f: Fetcher, user, n, rng, max_friend_pages=3):
    """Friends, then friends-of-friends. One edge out."""
    friends = []
    for pg in range(1, max_friend_pages + 1):
        e = f.get(paths.user_following(user, pg))
        if not e.ok:
            break
        batch = parse_people_page(e.body)
        if not batch:
            break
        friends += [p.username for p in batch if p.username]
    log.info("%d direct friends", len(friends))

    fof = set()
    for fr in friends:
        for pg in range(1, 3):
            e = f.get(paths.user_following(fr, pg))
            if not e.ok:
                break
            batch = parse_people_page(e.body)
            if not batch:
                break
            fof |= {p.username for p in batch if p.username}
        log.info("  via %s -> %d fof so far", fr, len(fof))
    fof -= set(friends) | {user}
    fof = sorted(fof)
    rng.shuffle(fof)
    return friends, fof[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("user")
    ap.add_argument("--mode", choices=["random", "fof"], required=True)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--films", default=None,
                    help="random mode: comma-separated popular slugs the user rated")
    ap.add_argument("--pages-per-film", type=int, default=2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    rng = random.Random(args.seed)

    with Fetcher() as f:
        if args.mode == "random":
            if not args.films:
                # default: the user's own most-rated films, i.e. maximally mainstream
                full = json.load(open(f"data/{args.user}_films_full.json"))["films"]
                pop = sorted([x for x in full if x.get("rating")],
                             key=lambda x: -x["lb_total"])[:8]
                films = [x["slug"] for x in pop]
                log.info("using the user's 8 most popular rated films as the frame: %s",
                         ", ".join(films))
            else:
                films = [s.strip() for s in args.films.split(",")]
            users = sample_random(f, films, args.n, args.pages_per_film, rng)
            meta = {"frame": films}
        else:
            friends, users = sample_fof(f, args.user, args.n, rng)
            meta = {"direct_friends": friends}

    json.dump({"user": args.user, "mode": args.mode, "sample_meta": meta,
               "seeds": [],          # no seeds: nothing is excluded for these arms
               "candidates": [{"username": u, "score": 0.0, "n_seeds": 0, "seeds": []}
                              for u in users]},
              open(args.out, "w"), indent=1)
    print(f"\n{args.mode}: sampled {len(users)} users -> {args.out}")
    if args.mode == "fof":
        print(f"  ({len(meta['direct_friends'])} direct friends recorded separately)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
