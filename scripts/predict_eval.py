"""Experiment 2: do the twins actually predict ratings the model has not seen?

This is the only question that matters. Everything upstream -- the crawl, the
co-occurrence tail, the agreement scores -- is machinery. If neighbour-based
prediction cannot beat a baseline that knows nothing about you personally, the
machinery is not earning its place.

THE BASELINE TO BEAT, measured on live Letterboxd data by an independent
analysis: user_mean + (LB_avg - global_mean), RMSE 0.6362. That predictor uses
only your average and the crowd's average. Note it beats "user mean" alone by a
wide margin -- the crowd carries most of the signal, and any system reporting
~0.65 has added nothing.

PREDICTOR (standard mean-centred user-based CF):

    pred(u,f) = mean_u + SUM_v w_v * (r_v(f) - mean_v) / SUM_v |w_v|

over neighbours v who rated f, with w_v = their Pearson correlation with u
computed on TRAINING films only. Mirrors (w<0) contribute with the sign flipped,
which is the point of keeping them.

SPLIT. Temporal, using the diary dates in the user's RSS feed: the films in
their recent feed are the test set, everything else is training. Correlations,
means and baselines are all fitted on training films only -- a correlation fitted
on a film you then predict is circular.

SEED EXCLUSION. Seed films are dropped from BOTH sides. Candidates were recruited
by loving them, so predicting them would score the recruitment.
"""

from __future__ import annotations

import argparse, json, logging, math, os, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_diary_rss, parse_films_page, parse_rating_histogram

log = logging.getLogger("eval")


def pearson(xs, ys):
    n = len(xs)
    if n < 2: return None
    mx, my = sum(xs)/n, sum(ys)/n
    num = sum((a-mx)*(b-my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a-mx)**2 for a in xs)); dy = math.sqrt(sum((b-my)**2 for b in ys))
    return num/(dx*dy) if dx and dy else None


def rmse(pairs):
    return math.sqrt(sum((p-a)**2 for p, a, _ in pairs)/len(pairs)) if pairs else None


def mae(pairs):
    return sum(abs(p-a) for p, a, _ in pairs)/len(pairs) if pairs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("user")
    ap.add_argument("--verified", default="data/twins_verified.json")
    ap.add_argument("--twins", default="data/twins.json")
    ap.add_argument("--min-common-train", type=int, default=5,
                    help="co-rated TRAINING films needed before a neighbour gets a weight")
    ap.add_argument("--min-neighbours", type=int, default=1,
                    help="neighbours who rated a film before we will predict it")
    ap.add_argument("--threshold", type=float, default=4.0,
                    help="liked = rating >= this. 4.0 is the user's stated bar.")
    ap.add_argument("--heart-bump", type=float, default=None,
                    help="Treat a hearted rating >= this as a full 4.0, EVERYWHERE: "
                         "training, the user mean, neighbour correlations and the "
                         "test label. The user's position is that a 3.5 with a heart "
                         "IS a 4 for them, so it is the rating that is wrong, not the "
                         "threshold. Rewriting only the label would leave the model "
                         "fitted on a value the user does not mean.")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    seeds = {s["slug"] for s in json.load(open(args.twins))["seeds"]}

    full = json.load(open(f"data/{args.user}_films_full.json"))["films"]
    mine = {f["slug"]: f["rating"] for f in full if f["rating"] is not None}

    hearted = set()
    if args.heart_bump is not None:
        from lbfetch import Cache
        from lbfetch.parse import parse_films_page
        e = Cache("cache").get(f"https://letterboxd.com/{args.user}/likes/films/")
        if e:
            hearted = {x.slug for x in parse_films_page(e.body)}
        log.info("hearts: %d films", len(hearted))
        bumped = [k for k, v in mine.items()
                  if k in hearted and args.heart_bump <= v < 4.0]
        for k in bumped:
            mine[k] = 4.0
        log.info("heart-bump: %d ratings raised to 4.0 (e.g. %s)",
                 len(bumped), ", ".join(bumped[:4]))

    # crowd average per film, from the cached histograms
    lb_avg = {}
    for f in full:
        h = {float(k): v for k, v in f["hist"].items()}
        n = sum(h.values())
        if n:
            lb_avg[f["slug"]] = sum(s*c for s, c in h.items())/n

    with Fetcher() as fetch:
        # --- temporal split from the diary feed -------------------------------
        e = fetch.get(paths.user_rss(args.user))
        recent = {d.slug for d in parse_diary_rss(e.body, args.user).entries
                  if d.slug and d.rating is not None}
        test = {s: r for s, r in mine.items() if s in recent and s not in seeds}
        train = {s: r for s, r in mine.items() if s not in recent and s not in seeds}
        log.info("split: %d train, %d test (%d seeds excluded from both)",
                 len(train), len(test), len(seeds))
        if len(test) < 5 or len(train) < 10:
            log.error("split too small to evaluate"); return 1

        # --- neighbours -------------------------------------------------------
        ver = json.load(open(args.verified))["results"]
        names = [r["username"] for r in ver]
        log.info("loading %d neighbour histories (cached)", len(names))
        hist = {}
        for u in names:
            h = {}
            e = fetch.get(paths.user_rss(u))
            if e.ok:
                for d in parse_diary_rss(e.body, u).entries:
                    if d.rating is not None and d.slug: h[d.slug] = d.rating
            for pg in range(1, 61):
                e = fetch.get(paths.user_films(u, pg))
                if not e.ok: break
                b = parse_films_page(e.body)
                if not b: break
                for x in b:
                    if x.rating is not None: h.setdefault(x.slug, x.rating)
            if h: hist[u] = h

    mu_train = sum(train.values())/len(train)
    global_mean = sum(lb_avg.values())/len(lb_avg)

    # weights fitted on TRAINING films only
    weights, nmean = {}, {}
    for u, h in hist.items():
        common = [s for s in set(h) & set(train)]
        if len(common) < args.min_common_train: continue
        r = pearson([train[s] for s in common], [h[s] for s in common])
        if r is None or abs(r) < 0.15: continue
        weights[u] = r
        nmean[u] = sum(h.values())/len(h)
    log.info("%d neighbours usable (|r|>=0.15 on >=%d shared training films)",
             len(weights), args.min_common_train)

    # --- predict ------------------------------------------------------------
    models = defaultdict(list)
    covered = 0
    for f, actual in test.items():
        models["global mean"].append((global_mean, actual, f))
        models["user mean"].append((mu_train, actual, f))
        if f in lb_avg:
            models["LB average"].append((lb_avg[f], actual, f))
            models["user_mean + (LB_avg - global)"].append(
                (mu_train + lb_avg[f] - global_mean, actual, f))
        contrib = [(weights[u], hist[u][f] - nmean[u]) for u in weights if f in hist[u]]
        if len(contrib) >= args.min_neighbours:
            denom = sum(abs(w) for w, _ in contrib)
            if denom > 0:
                covered += 1
                p = mu_train + sum(w*d for w, d in contrib)/denom
                models["TWIN CF"].append((max(0.5, min(5.0, p)), actual, f))
                # CF blended onto the crowd baseline
                if f in lb_avg:
                    base = mu_train + lb_avg[f] - global_mean
                    models["baseline + TWIN CF"].append(
                        (max(0.5, min(5.0, 0.5*base + 0.5*p)), actual, f))

    print(f"\ntest films {len(test)} | neighbour coverage {covered}/{len(test)} "
          f"({covered/len(test):.0%})\n")
    print(f"{'model':32s} {'n':>4s} {'RMSE':>7s} {'MAE':>7s}")
    print("-"*54)
    order = ["global mean", "user mean", "LB average",
             "user_mean + (LB_avg - global)", "TWIN CF", "baseline + TWIN CF"]
    res = {}
    for m in order:
        pairs = models.get(m) or []
        if not pairs: continue
        res[m] = {"n": len(pairs), "rmse": rmse(pairs), "mae": mae(pairs)}
        print(f"{m:32s} {len(pairs):>4d} {res[m]['rmse']:>7.4f} {res[m]['mae']:>7.4f}")

    # --- the metrics that match the actual task ------------------------------
    # RMSE scores exact-rating error, which is NOT the question. The question is
    # "will I like this", i.e. a threshold decision, and for a watchlist it is
    # really "which of these should I watch first", i.e. a RANKING. Accuracy on
    # its own is a trap here: the base rate is ~79%, so a model that says
    # "dislike" every time already scores 79%.
    thr = args.threshold
    def is_liked(slug, actual):
        return actual >= thr        # the bump already moved hearted 3.5s to 4.0
    def auc(pairs):
        pos = [p for p, a, sl in pairs if is_liked(sl, a)]
        neg = [p for p, a, sl in pairs if not is_liked(sl, a)]
        if not pos or not neg: return None
        wins = sum((a > b) + 0.5*(a == b) for a in pos for b in neg)
        return wins/(len(pos)*len(neg))
    def prec_at_k(pairs, k):
        top = sorted(pairs, key=lambda x: -x[0])[:k]
        return sum(1 for _, a, sl in top if is_liked(sl, a))/len(top) if top else None

    print(f"\nLIKED = rating >= {thr}   (the decision you actually make)")
    print(f"{'model':32s} {'base':>5s} {'acc':>5s} {'prec':>5s} {'rec':>5s} "
          f"{'F1':>5s} {'AUC':>6s} {'P@5':>5s} {'P@10':>6s}")
    print("-"*84)
    for m in order:
        pairs = models.get(m) or []
        if not pairs: continue
        base = sum(1 for _, a, sl in pairs if is_liked(sl, a))/len(pairs)
        tp = sum(1 for p, a, sl in pairs if p >= thr and is_liked(sl, a))
        fp = sum(1 for p, a, sl in pairs if p >= thr and not is_liked(sl, a))
        fn = sum(1 for p, a, sl in pairs if p < thr and is_liked(sl, a))
        acc = sum(1 for p, a, sl in pairs if (p >= thr) == is_liked(sl, a))/len(pairs)
        pr = tp/(tp+fp) if tp+fp else 0.0
        rc = tp/(tp+fn) if tp+fn else 0.0
        f1 = 2*pr*rc/(pr+rc) if pr+rc else 0.0
        a_ = auc(pairs); p5 = prec_at_k(pairs, 5); p10 = prec_at_k(pairs, 10)
        print(f"{m:32s} {base:>5.0%} {acc:>5.0%} {pr:>5.0%} {rc:>5.0%} {f1:>5.2f} "
              f"{(f'{a_:.3f}' if a_ is not None else '  n/a'):>6s} "
              f"{(f'{p5:.0%}' if p5 is not None else ' n/a'):>5s} "
              f"{(f'{p10:.0%}' if p10 is not None else ' n/a'):>6s}")
    print("\nAUC is the one to read for a watchlist: it is threshold-free and asks "
          "\n'given a liked and a disliked film, does the model rank them correctly?'"
          "\nP@5 / P@10 = of the top 5 / 10 films it recommends, what share you liked.")

    if args.out:
        json.dump({"user": args.user, "n_train": len(train), "n_test": len(test),
                   "coverage": covered, "results": res}, open(args.out, "w"), indent=1)
        print(f"\nwrote {args.out}")
    print("\nNOTE: a small test set gives a wide CI. With ~30 films the RMSE standard\n"
          "error is roughly +/-0.08, so differences under ~0.1 are not conclusive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
