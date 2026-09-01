"""Find a user's twins by enumerating who else loved (or merely saw) their films.

THREE KINDS OF TWIN, because "similar taste" is not one thing:

  taste twin      rated the same films >=4 (or hearted them). Agreement on
                  PREFERENCE. Needs mid-rare seeds -- a pool of ~1k-30k, large
                  enough that two people can plausibly intersect.

  territory twin  merely SAW the same films, whatever they thought. Agreement on
                  EXPOSURE -- having watched an obscure Iranian documentary means
                  you seek out obscure Iranian documentaries. Seeds from the
                  ULTRA-rare band, the films too small to intersect on taste:
                  the-tibetan-book-of-the-dead has 76 raters >=4 (useless for
                  taste) but 176 total watchers at 8 pages (excellent territory).
                  The bands are complementary; what one type discards, the other
                  wants.

  mirror twin     consistent ANTI-correlation. Falls out of the same stage-2
                  agreement computation for free, and is real evidence with the
                  sign flipped. Most systems throw it away.

SEEDS. Pass an explicit set (--seeds / --seed-file, e.g. the 8 films a user
picked in the picker page) or let the script choose from their whole watched log
(--auto). The explicit path is the point: a user asking "find people like me
*about these eight films*" is a sharper question than "about everything I ever
watched", and it is the only way to find twins for a mood rather than a person.

NEVER PARTIALLY ENUMERATE A SEED. A truncated pool makes a candidate's ABSENCE
uninformative, which silently breaks the intersection. Whole seed or skip it.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_rating_histogram

log = logging.getLogger("twins")
BASE = "https://letterboxd.com"
SECONDS_PER_PAGE = 4.5          # measured: 3.0s interval + 1.5s mean jitter
NAME_RE = re.compile(r'href="/([a-z0-9_]+)/" class="name"')

# Letterboxd renders half-stars in these URLs as a percent-encoded ½.
_RATING_TOKEN = {0.5: "%C2%BD", 1.0: "1", 1.5: "1%C2%BD", 2.0: "2", 2.5: "2%C2%BD",
                 3.0: "3", 3.5: "3%C2%BD", 4.0: "4", 4.5: "4%C2%BD", 5.0: "5"}


def rated_url(slug, stars, page=1):
    sfx = "" if page == 1 else f"page/{page}/"
    return f"{BASE}/film/{slug}/members/rated/{_RATING_TOKEN[stars]}/{sfx}"


def members_url(slug, page=1):
    sfx = "" if page == 1 else f"page/{page}/"
    return f"{BASE}/film/{slug}/members/{sfx}"


def likes_url(slug, page=1):
    sfx = "" if page == 1 else f"page/{page}/"
    return f"{BASE}/film/{slug}/likes/{sfx}"


def plan_seed(hist, total, mode, min_rating, include_likes):
    """-> (list of (url_builder, n_pages), pool_size) for one seed."""
    series, pool = [], 0
    if mode in ("taste", "both"):
        for stars in [s for s in _RATING_TOKEN if s >= min_rating]:
            n = hist.get(stars, 0)
            if n:
                series.append((lambda p, sl=None, st=stars: rated_url(sl, st, p),
                               -(-n // 25), f"rated{st_fmt(stars)}"))
                pool += n
        if include_likes:
            # Heart count is not in the histogram; assume the >=4 pool as a
            # ceiling and let the fetch loop stop on the first empty page.
            series.append((lambda p, sl=None: likes_url(sl, p), 0, "likes"))
    if mode in ("territory", "both"):
        series.append((lambda p, sl=None: members_url(sl, p), -(-total // 25), "seen"))
        pool = max(pool, total)
    return series, pool


def st_fmt(s):
    return str(int(s)) if s == int(s) else str(s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("user")
    ap.add_argument("--seeds", default=None, help="comma-separated film slugs")
    ap.add_argument("--seed-file", default=None,
                    help="one slug per line, or the JSON the picker page exports")
    ap.add_argument("--auto", action="store_true",
                    help="choose seeds from the user's whole watched log")
    ap.add_argument("--mode", choices=["taste", "territory", "both"], default="taste")
    ap.add_argument("--min-rating", type=float, default=4.0)
    ap.add_argument("--include-likes", action="store_true")
    ap.add_argument("--max-hours", type=float, default=10.0, help="hard ceiling")
    ap.add_argument("--max-pages-per-seed", type=int, default=2000)
    ap.add_argument("--min-pool", type=int, default=120,
                    help="taste mode: skip pools too small to intersect")
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    budget = int(args.max_hours * 3600 / SECONDS_PER_PAGE)
    log.info("ceiling %.1f h -> %d pages", args.max_hours, budget)

    # --- resolve the seed set ------------------------------------------------
    full_path = f"data/{args.user}_films_full.json"
    if os.path.exists(full_path):
        catalog = {f["slug"]: f for f in json.load(open(full_path))["films"]}
    else:
        catalog = {f["slug"]: f for f in json.load(open(f"data/{args.user}.json"))["films"]}

    if args.seed_file:
        raw = open(args.seed_file).read().strip()
        if raw.startswith("{") or raw.startswith("["):
            obj = json.loads(raw)
            wanted = obj.get("seeds", obj) if isinstance(obj, dict) else obj
            wanted = [w["slug"] if isinstance(w, dict) else w for w in wanted]
        else:
            wanted = [l.strip() for l in raw.splitlines() if l.strip()]
    elif args.seeds:
        wanted = [s.strip() for s in args.seeds.split(",") if s.strip()]
    elif args.auto:
        wanted = [s for s, f in catalog.items()
                  if (f.get("rating") or 0) >= args.min_rating]
        log.info("auto mode: %d films rated >= %.1f", len(wanted), args.min_rating)
    else:
        return ap.error("give --seeds, --seed-file, or --auto")

    missing = [w for w in wanted if w not in catalog]
    if missing:
        log.warning("%d seeds not in the watched log (kept anyway): %s",
                    len(missing), ", ".join(missing[:5]))

    with Fetcher() as fetch:
        # --- cost each seed --------------------------------------------------
        plans = []
        for slug in wanted:
            f = catalog.get(slug, {"slug": slug})
            if "hist" in f:
                hist = {float(k): v for k, v in f["hist"].items()}
                total = f["lb_total"]
            else:
                e = fetch.get(paths.film_rating_histogram(slug))
                hist = parse_rating_histogram(e.body) if e.ok else {}
                total = sum(hist.values())
            series, pool = plan_seed(hist, total, args.mode, args.min_rating,
                                     args.include_likes)
            pages = sum(n for _, n, _ in series)
            plans.append({"slug": slug, "total": total, "pool": pool,
                          "pages": pages, "series": series,
                          "rating": f.get("rating")})

        corpus = max(sum(p["total"] for p in plans), 1)
        for p in plans:
            p["idf"] = math.log(corpus / max(p["total"], 1))

        eligible = [p for p in plans
                    if p["pages"] <= args.max_pages_per_seed
                    and (args.mode == "territory" or p["pool"] >= args.min_pool)]
        eligible.sort(key=lambda p: p["pages"])

        chosen, spent = [], 0
        for p in eligible:
            if spent + p["pages"] > budget:
                continue                       # whole seed or nothing
            chosen.append(p); spent += p["pages"]

        print(f"\nmode={args.mode}  min_rating={args.min_rating}  "
              f"seeds requested={len(wanted)}\n")
        print(f"{'seed':34s} {'you':>4s} {'lb_total':>10s} {'pool':>9s} {'pages':>7s} {'idf':>5s}  use")
        print("-" * 84)
        for p in plans:
            use = "yes" if p in chosen else (
                "too big" if p["pages"] > args.max_pages_per_seed else
                "pool<min" if args.mode != "territory" and p["pool"] < args.min_pool
                else "budget")
            print(f"{p['slug'][:34]:34s} {str(p['rating'] or '-'):>4s} {p['total']:>10,} "
                  f"{p['pool']:>9,} {p['pages']:>7,} {p['idf']:>5.2f}  {use}")
        print(f"\n{len(chosen)} seeds fully enumerated in {spent:,} pages "
              f"= {spent*SECONDS_PER_PAGE/3600:.1f} h  "
              f"(~{spent*22.7/1024:.0f} MB over the wire)")
        if args.dry_run:
            return 0
        if not chosen:
            print("\nNothing fits the budget. Raise --max-hours or pick rarer seeds.")
            return 1

        # --- enumerate -------------------------------------------------------
        score, matched, seen_in = defaultdict(float), defaultdict(set), defaultdict(set)
        UNBOUNDED_CAP = 120      # likes has no count in the histogram; page until
                                 # empty, but never forever -- an unbounded loop on
                                 # an endpoint that always returns names would run
                                 # all night and look like progress. Measured
                                 # 2026-08-27: the-canyons has ~92 likes pages against
                                 # 49 rated pages, so heart pools run ~2x the >=4
                                 # rating pools and a 400 cap nearly triples the run.
        done_pages = 0
        import time as _t
        t_start = _t.time()

        def _checkpoint():
            if not args.out:
                return
            try:
                r = sorted(score.items(), key=lambda kv: (-len(matched[kv[0]]), -kv[1]))
                json.dump({"user": args.user, "mode": args.mode, "partial": True,
                           "pages_done": done_pages,
                           "seeds": [{"slug": q["slug"], "idf": q["idf"]} for q in chosen],
                           "candidates": [{"username": n, "score": sc,
                                           "n_seeds": len(matched[n]),
                                           "seeds": sorted(matched[n])}
                                          for n, sc in r]},
                          open(args.out + ".partial", "w"))
            except Exception as exc:
                log.warning("checkpoint failed: %s", exc)

        work = ([(p, b, n, k) for p in chosen for b, n, k in p["series"] if k != "likes"]
                + [(p, b, n, k) for p in chosen for b, n, k in p["series"] if k == "likes"])
        for p, builder, npages, kind in work:
            if True:
                page = 1
                limit = npages if npages else UNBOUNDED_CAP
                while page <= limit:
                    e = fetch.get(builder(page, p["slug"]) if _takes_slug(builder)
                                  else builder(page))
                    if not e.ok:
                        break
                    names = set(NAME_RE.findall(e.body))
                    if not names:
                        break
                    for n in names:
                        if n == args.user:
                            continue
                        if p["slug"] not in matched[n]:
                            score[n] += p["idf"]
                            matched[n].add(p["slug"])
                        seen_in[n].add(kind)
                    page += 1
                    done_pages += 1
                    if done_pages % 100 == 0:
                        multi = sum(1 for v in matched.values() if len(v) > 1)
                        rate = done_pages / max(_t.time() - t_start, 1)
                        log.info("%d pages | %s p%d | %d candidates, %d multi-seed "
                                 "| %.2f pg/s | eta %.1f h",
                                 done_pages, p["slug"][:22], page, len(score), multi,
                                 rate, (spent - done_pages) / max(rate, .01) / 3600)
                        _checkpoint()

    ranked = sorted(score.items(), key=lambda kv: (-len(matched[kv[0]]), -kv[1]))
    dist = Counter(len(v) for v in matched.values())
    print(f"\n{len(score):,} distinct candidates")
    print("  shared seeds -> candidates:",
          "  ".join(f"{k}:{dist[k]:,}" for k in sorted(dist)))
    print(f"\n{'candidate':22s} {'n':>2s} {'score':>6s}  seeds in common")
    print("-" * 84)
    for name, sc in ranked[:30]:
        print(f"{name:22s} {len(matched[name]):>2d} {sc:>6.2f}  "
              f"{', '.join(sorted(matched[name])[:3])}")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        json.dump({"user": args.user, "mode": args.mode,
                   "min_rating": args.min_rating, "pages_spent": spent,
                   "seeds": [{"slug": p["slug"], "total": p["total"],
                              "pool": p["pool"], "idf": p["idf"]} for p in chosen],
                   "candidates": [{"username": n, "score": s,
                                   "n_seeds": len(matched[n]),
                                   "seeds": sorted(matched[n]),
                                   "via": sorted(seen_in[n])}
                                  for n, s in ranked]},
                  open(args.out, "w"), indent=1)
        print(f"\nwrote {args.out}")

    print("\nSTAGE 2 (not done here): fetch each shortlisted candidate's full history and\n"
          "score agreement over ALL co-rated films, percentile-ranked within each user's\n"
          "own scale. EXCLUDE these seeds from that score and from any evaluation -- you\n"
          "recruited these people with them, so scoring on them is circular.")
    return 0


def _takes_slug(fn):
    import inspect
    return "sl" in inspect.signature(fn).parameters


if __name__ == "__main__":
    raise SystemExit(main())
