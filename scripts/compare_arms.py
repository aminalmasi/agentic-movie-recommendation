"""Compare every sampled population on the same scale.

The whole point of the arms is that a twin rate means nothing without a
denominator. Each arm is a different way of being connected to the target:

  friends   a real social tie
  fof       one edge further out
  treatment shares >= 3 of the target's rare films (the hypothesis)
  rare      shares exactly 1 rare film (the co-occurrence null)
  random    shares 1 mainstream film (the "any two cinephiles" null)

If treatment does not clear random and rare, co-occurrence adds nothing. If
friends clears everything, social distance is the signal and the whole
rare-film approach was the wrong instrument.
"""
from __future__ import annotations
import json, math, os, sys
from math import comb

ARMS = [("friends",   "data/verified_friends.json", "real social tie"),
        ("fof",       "data/verified_fof.json",     "friend-of-friend"),
        ("treatment", "data/twins_verified_rated.json", ">=3 rated rare seeds"),
        ("rare ctrl", "data/control_nseeds1.json",  "1 rare film, random"),
        ("random",    "data/verified_random.json",  "1 mainstream film")]


def load(path):
    if not os.path.exists(path):
        return None
    r = json.load(open(path))["results"]
    return [x for x in r if x.get("r") is not None]


def fisher(a, b, c, d):
    def p(a, b, c, d):
        return comb(a+b, a)*comb(c+d, c)/comb(a+b+c+d, a+c)
    obs = p(a, b, c, d); tot = a+c; s = 0.0
    for i in range(tot+1):
        j = tot-i
        if i <= a+b and j <= c+d:
            q = p(i, a+b-i, j, c+d-j)
            if q <= obs+1e-12: s += q
    return s


def main():
    rows = []
    for name, path, desc in ARMS:
        s = load(path)
        if not s:
            rows.append((name, desc, None)); continue
        n = len(s)
        tw = sum(1 for x in s if x["r"] >= 0.5)
        mi = sum(1 for x in s if x["r"] <= -0.4)
        # Scorable rate is itself a finding: real social ties actually co-watch,
        # strangers mostly do not.
        allr = json.load(open(path))["results"]
        hi = [x for x in s if x["n_common"] >= 15]
        rows.append((name, desc, {
            "n": n, "n_attempted": len(allr), "scorable": n/max(len(allr),1),
            "hi_n": len(hi),
            "hi_mean_r": (sum(x["r"] for x in hi)/len(hi)) if hi else None,
            "hi_twins": sum(1 for x in hi if x["r"] >= 0.5),
            "twins": tw, "mirrors": mi,
            "twin_rate": tw/n, "mean_r": sum(x["r"] for x in s)/n,
            "mean_abs": sum(abs(x["r"]) for x in s)/n,
            "median_common": sorted(x["n_common"] for x in s)[n//2],
        }))

    print("ALL SCORABLE PAIRS (>=6 co-rated films)")
    print(f"{'arm':12s} {'what it is':22s} {'tried':>6s} {'scorable':>9s} {'twins':>6s} "
          f"{'rate':>7s} {'mean r':>8s} {'med common':>11s}")
    print("-"*88)
    for name, desc, s in rows:
        if not s:
            print(f"{name:12s} {desc:22s}  (not finished)"); continue
        print(f"{name:12s} {desc:22s} {s['n_attempted']:>6d} "
              f"{s['n']:>4d}/{s['scorable']:>4.0%} {s['twins']:>6d} "
              f"{s['twin_rate']:>6.1%} {s['mean_r']:>+8.3f} {s['median_common']:>11d}")

    print("\nRESTRICTED TO >=15 CO-RATED FILMS -- correlations on 6-9 films are noise,")
    print("and a small overlap inflates |r| by chance. This is the honest comparison.")
    print(f"{'arm':12s} {'n':>4s} {'twins':>6s} {'rate':>7s} {'mean r':>8s}")
    print("-"*44)
    for name, desc, s in rows:
        if not s or not s["hi_n"]:
            print(f"{name:12s}  (none with >=15 co-rated)"); continue
        print(f"{name:12s} {s['hi_n']:>4d} {s['hi_twins']:>6d} "
              f"{s['hi_twins']/s['hi_n']:>6.1%} {s['hi_mean_r']:>+8.3f}")

    base = dict((n, s) for n, _, s in rows if s)
    if "random" in base:
        print(f"\nFisher exact vs the `random` arm (twin rate):")
        r = base["random"]
        for name in ("friends", "fof", "treatment", "rare ctrl"):
            if name not in base: continue
            a = base[name]
            p = fisher(a["twins"], a["n"]-a["twins"], r["twins"], r["n"]-r["twins"])
            mark = "  <-- significant" if p < 0.05 else ""
            print(f"  {name:12s} p = {p:.3f}{mark}")
    print("\nCaveat: n~100 per arm. A twin rate difference under ~8 points will not "
          "reach\nsignificance, so a null here means 'no large effect', not 'no effect'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
