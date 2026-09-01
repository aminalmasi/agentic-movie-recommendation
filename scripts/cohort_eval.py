"""Run the friends-CF evaluation for every target, then aggregate.

n=1 proves nothing. This repeats the identical protocol across the cohort and
reports the DISTRIBUTION -- median and quartiles per model, plus how often
friends-CF beats each baseline head to head on the same user. A per-user paired
comparison is the right test here: users differ enormously in how predictable
they are, so an unpaired average across users is dominated by that variance
rather than by the model difference.
"""
from __future__ import annotations
import argparse, json, logging, math, os, statistics as st, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_films_page, parse_diary_rss, parse_rating_histogram

log = logging.getLogger("cohort-eval")


def pearson(xs, ys):
    n = len(xs)
    if n < 2: return None
    mx, my = sum(xs)/n, sum(ys)/n
    num = sum((a-mx)*(b-my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a-mx)**2 for a in xs)); dy = math.sqrt(sum((b-my)**2 for b in ys))
    return num/(dx*dy) if dx and dy else None


def auc(pairs, thr):
    pos = [p for p, a in pairs if a >= thr]; neg = [p for p, a in pairs if a < thr]
    if not pos or not neg: return None
    return sum((x > y) + 0.5*(x == y) for x in pos for y in neg)/(len(pos)*len(neg))


def history(f, u, max_pages=12):
    h = {}
    e = f.get(paths.user_rss(u))
    if e.ok:
        for d in parse_diary_rss(e.body, u).entries:
            if d.rating is not None and d.slug: h[d.slug] = d.rating
    for pg in range(1, max_pages+1):
        e = f.get(paths.user_films(u, pg))
        if not e.ok: break
        b = parse_films_page(e.body)
        if not b: break
        for x in b:
            if x.rating is not None: h.setdefault(x.slug, x.rating)
    return h


def recent(f, u):
    e = f.get(paths.user_rss(u))
    if not e.ok: return set()
    return {d.slug for d in parse_diary_rss(e.body, u).entries
            if d.slug and d.rating is not None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", default="data/cohort_final.json")
    ap.add_argument("--threshold", type=float, default=4.0)
    ap.add_argument("--min-test", type=int, default=8)
    ap.add_argument("--out", default="data/cohort_eval.json")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    coh = json.load(open(a.cohort))["targets"]
    rows = []
    with Fetcher() as f:
        for i, (u, meta) in enumerate(coh.items(), 1):
            mine = history(f, u)
            rec = recent(f, u)
            test = {s: r for s, r in mine.items() if s in rec}
            train = {s: r for s, r in mine.items() if s not in rec}
            if len(test) < a.min_test or len(train) < 20:
                continue
            mu = sum(train.values())/len(train)

            w, nm = {}, {}
            for fr in meta["friends"]:
                h = history(f, fr)
                common = list(set(h) & set(train))
                if len(common) < 5: continue
                r = pearson([train[s] for s in common], [h[s] for s in common])
                if r is None or abs(r) < 0.15: continue
                w[fr] = r; nm[fr] = sum(h.values())/len(h); coh[u].setdefault("_h", {})[fr] = h

            cf, um = [], []
            for s, actual in test.items():
                um.append((mu, actual))
                contrib = [(w[v], coh[u]["_h"][v][s] - nm[v])
                           for v in w if s in coh[u]["_h"][v]]
                if contrib:
                    d = sum(abs(x) for x, _ in contrib)
                    if d > 0:
                        cf.append((max(0.5, min(5.0, mu + sum(x*y for x, y in contrib)/d)), actual))
            coh[u].pop("_h", None)
            if len(cf) < a.min_test: continue
            rows.append({"user": u, "n_train": len(train), "n_test": len(test),
                         "n_scored": len(cf), "n_neighbours": len(w),
                         "coverage": len(cf)/len(test),
                         "auc_cf": auc(cf, a.threshold), "auc_user": auc(um, a.threshold),
                         "rmse_cf": math.sqrt(sum((p-x)**2 for p, x in cf)/len(cf)),
                         "rmse_user": math.sqrt(sum((p-x)**2 for p, x in um)/len(um))})
            log.info("%d/%d %s: AUC_cf=%s cov=%.0f%% nb=%d", i, len(coh), u,
                     f"{rows[-1]['auc_cf']:.3f}" if rows[-1]['auc_cf'] else "n/a",
                     100*rows[-1]['coverage'], len(w))

    ok = [r for r in rows if r["auc_cf"] is not None and r["auc_user"] is not None]
    print(f"\n{len(rows)} targets evaluated, {len(ok)} with a computable AUC\n")
    if ok:
        def q(k):
            v = sorted(r[k] for r in ok)
            return st.median(v), v[len(v)//4], v[3*len(v)//4]
        for k, label in (("auc_cf", "AUC friends-CF"), ("auc_user", "AUC user-mean"),
                         ("coverage", "coverage"), ("n_neighbours", "usable neighbours")):
            m, lo, hi = q(k)
            print(f"  {label:22s} median {m:>6.3f}   IQR [{lo:.3f}, {hi:.3f}]")
        wins = sum(1 for r in ok if r["auc_cf"] > r["auc_user"])
        print(f"\n  friends-CF beats user-mean AUC on {wins}/{len(ok)} targets "
              f"({wins/len(ok):.0%})")
        better = sum(1 for r in ok if r["auc_cf"] > 0.7)
        print(f"  targets where friends-CF AUC > 0.70: {better}/{len(ok)} ({better/len(ok):.0%})")
    json.dump({"rows": rows}, open(a.out, "w"), indent=1)
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    raise SystemExit(main())
