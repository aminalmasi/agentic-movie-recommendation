#!/usr/bin/env python3
"""
Reproducible baseline / split / calibration reference experiments on MovieLens-100K.
Run with a python that has numpy + scikit-learn (e.g. /conf/shared-software/anaconda/bin/python3).

Produces the reference numbers used in the Letterboxd evaluation-methodology plan:
  1. baseline ladder (global mean / user mean / item mean / mu+bu+bi / MF) on RMSE AND ranking
  2. random vs global-temporal vs leave-one-out split deltas
  3. per-user label statistics (base rates, entropy, std)
  4. Platt / isotonic / temperature calibration of a rating score into P(like), incl. sample-size curve

Data: https://files.grouplens.org/datasets/movielens/ml-100k.zip
"""
import math, random, zipfile, urllib.request, os, sys
from collections import defaultdict, Counter
import numpy as np

ZIP = os.environ.get("ML100K", "ml-100k.zip")
if not os.path.exists(ZIP):
    urllib.request.urlretrieve("https://files.grouplens.org/datasets/movielens/ml-100k.zip", ZIP)
Z = zipfile.ZipFile(ZIP)
D = [(a, b, float(c), int(d)) for a, b, c, d in
     (l.split('\t') for l in Z.read('ml-100k/u.data').decode().strip().split('\n'))]

L2, L3 = 25, 10          # Koren TKDD'10 shrinkage constants (Netflix values)

def fit_bias(tr):
    mu = sum(r for _, _, r, _ in tr) / len(tr)
    im = defaultdict(list)
    for u, i, r, _ in tr:
        im[i].append(r)
    bi = {k: sum(x - mu for x in v) / (L2 + len(v)) for k, v in im.items()}
    acc, cnt = defaultdict(float), Counter()
    for u, i, r, _ in tr:
        acc[u] += r - mu - bi.get(i, 0.0); cnt[u] += 1
    bu = {k: acc[k] / (L3 + cnt[k]) for k in acc}
    imean = {k: sum(v) / len(v) for k, v in im.items()}
    um = defaultdict(list)
    for u, i, r, _ in tr:
        um[u].append(r)
    umean = {k: sum(v) / len(v) for k, v in um.items()}
    return mu, bu, bi, umean, imean

def fit_mf(tr, mu, bu, bi, F=32, epochs=40, lr=0.007, reg=0.05, seed=0):
    users = sorted({u for u, _, _, _ in tr}); items = sorted({i for _, i, _, _ in tr})
    ui = {u: k for k, u in enumerate(users)}; ii = {i: k for k, i in enumerate(items)}
    rng = np.random.default_rng(seed)
    Pm = rng.normal(0, .05, (len(users), F)); Q = rng.normal(0, .05, (len(items), F))
    idx = [(ui[u], ii[i], r) for u, i, r, _ in tr]
    for _ in range(epochs):
        random.shuffle(idx)
        for a, b, r in idx:
            e = r - (mu + bu[users[a]] + bi[items[b]] + float(Pm[a] @ Q[b]))
            pa = Pm[a].copy()
            Pm[a] += lr * (e * Q[b] - reg * Pm[a]); Q[b] += lr * (e * pa - reg * Q[b])
    def f(u, i):
        base = mu + bu.get(u, 0.) + bi.get(i, 0.)
        return base + (float(Pm[ui[u]] @ Q[ii[i]]) if (u in ui and i in ii) else 0.)
    return f

def dcg(rel): return sum((2 ** r - 1) / math.log2(k + 2) for k, r in enumerate(rel))

def rank_eval(f, te_by_user, thr):
    n5, n10, p5, aucs = [], [], [], []
    for u, v in te_by_user.items():
        lab = [1 if thr(u, r) else 0 for _, _, r, _ in v]
        pr = [f(u, i) for _, i, _, _ in v]
        if sum(lab) in (0, len(lab)):
            continue
        order = sorted(range(len(v)), key=lambda k: (-pr[k], random.random()))
        rel = [lab[k] for k in order]; ideal = sorted(lab, reverse=True)
        n5.append(dcg(rel[:5]) / dcg(ideal[:5])); n10.append(dcg(rel[:10]) / dcg(ideal[:10]))
        p5.append(sum(rel[:5]) / 5)
        pos = [pr[k] for k in range(len(v)) if lab[k]]; neg = [pr[k] for k in range(len(v)) if not lab[k]]
        aucs.append(sum((1 if a > b else .5 if a == b else 0) for a in pos for b in neg) / (len(pos) * len(neg)))
    m = lambda x: sum(x) / len(x)
    return m(n5), m(n10), m(p5), m(aucs), len(n5)

def main():
    # ---- 1 + 3: baseline ladder, per-user 10-item holdout -------------------
    byu = defaultdict(list)
    for rec in D: byu[rec[0]].append(rec)
    random.seed(7); tr, te = [], {}
    for u, v in byu.items():
        vv = v[:]; random.shuffle(vv)
        if len(vv) < 25: tr.extend(vv); continue
        te[u] = vv[:10]; tr.extend(vv[10:])
    mu, bu, bi, umean, imean = fit_bias(tr)
    mf = fit_mf(tr, mu, bu, bi)
    models = {'global mean': lambda u, i: mu,
              'user mean': lambda u, i: umean.get(u, mu),
              'item mean': lambda u, i: imean.get(i, mu),
              'bias mu+bu+bi': lambda u, i: mu + bu.get(u, 0.) + bi.get(i, 0.),
              'MF f=32 + biases': mf}
    def rmse(f): 
        s = [(r - min(5, max(1, f(u, i)))) ** 2 for u, v in te.items() for _, i, r, _ in v]
        return math.sqrt(sum(s) / len(s))
    print("=== baseline ladder (ML-100K, per-user 10 held-out, liked = rating>=4) ===")
    print("%-19s %7s %8s %8s %7s %10s" % ('model', 'RMSE', 'NDCG@5', 'NDCG@10', 'P@5', 'perUserAUC'))
    for n, f in models.items():
        random.seed(3)
        a, b, c, d, k = rank_eval(f, te, lambda u, r: r >= 4)
        print("%-19s %7.4f %8.4f %8.4f %7.4f %10.4f (n=%d)" % (n, rmse(f), a, b, c, d, k))

    # ---- 2: split strategy ---------------------------------------------------
    print("\n=== split strategy (same bias model, ML-100K) ===")
    def ev(trn, tst, name):
        m, U, I, _, _ = fit_bias(trn)
        f = lambda u, i: min(5, max(1, m + U.get(u, 0.) + I.get(i, 0.)))
        e = [(r - f(u, i)) ** 2 for u, i, r, _ in tst]
        print("%-36s n_test=%6d  RMSE=%.4f" % (name, len(tst), math.sqrt(sum(e) / len(e))))
    Ds = D[:]; random.seed(0); random.shuffle(Ds); c = int(.9 * len(Ds))
    ev(Ds[:c], Ds[c:], 'random 90/10')
    Dt = sorted(D, key=lambda x: x[3]); c = int(.9 * len(Dt))
    ev(Dt[:c], Dt[c:], 'GLOBAL temporal (last 10% by ts)')
    a, b = [], []
    for u, v in byu.items():
        v2 = sorted(v, key=lambda x: x[3]); b.append(v2[-1]); a.extend(v2[:-1])
    ev(a, b, 'per-user leave-LAST-out')
    a, b = [], []; random.seed(1)
    for u, v in byu.items():
        j = random.randrange(len(v)); b.append(v[j]); a.extend(v[:j] + v[j + 1:])
    ev(a, b, 'per-user RANDOM leave-one-out (leaky)')

    # ---- 3: per-user label statistics ---------------------------------------
    uall = defaultdict(list)
    for u, i, r, _ in D: uall[u].append(r)
    def ent(v):
        c = Counter(v); n = len(v); return -sum((k / n) * math.log2(k / n) for k in c.values())
    rates = sorted(sum(1 for x in v if x >= 4) / len(v) for v in uall.values() if len(v) >= 20)
    ents = sorted(ent(v) for v in uall.values() if len(v) >= 20)
    stds = sorted(math.sqrt(sum((x - sum(v) / len(v)) ** 2 for x in v) / len(v))
                  for v in uall.values() if len(v) >= 20)
    q = lambda a, p: a[int(p * len(a))]
    print("\n=== per-user label statistics (users with >=20 ratings, n=%d) ===" % len(rates))
    print("P(rating>=4): min=%.3f p10=%.3f median=%.3f p90=%.3f max=%.3f" %
          (rates[0], q(rates, .1), q(rates, .5), q(rates, .9), rates[-1]))
    print("rating entropy (bits, max=log2(5)=2.322): min=%.3f p10=%.3f median=%.3f" %
          (ents[0], q(ents, .1), q(ents, .5)))
    print("per-user rating std: p10=%.3f median=%.3f p90=%.3f ; frac<0.6=%.3f" %
          (q(stds, .1), q(stds, .5), q(stds, .9), sum(1 for s in stds if s < .6) / len(stds)))

    # ---- 4: calibration ------------------------------------------------------
    from sklearn.linear_model import LogisticRegression
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import brier_score_loss, roc_auc_score, log_loss
    from scipy.optimize import minimize_scalar
    Dc = D[:]; random.seed(5); random.shuffle(Dc); n = len(Dc)
    trn, cal, tst = Dc[:int(.7 * n)], Dc[int(.7 * n):int(.85 * n)], Dc[int(.85 * n):]
    m, U, I, _, _ = fit_bias(trn)
    s = lambda u, i: m + U.get(u, 0.) + I.get(i, 0.)
    X = lambda S: np.array([[s(u, i)] for u, i, _, _ in S])
    Y = lambda S: np.array([1 if r >= 4 else 0 for _, _, r, _ in S])
    Xc, yc, Xt, yt = X(cal), Y(cal), X(tst), Y(tst)
    def ece(p, y, B=10):
        e, ed = 0., np.linspace(0, 1, B + 1)
        for k in range(B):
            msk = (p >= ed[k]) & (p <= 1.0 if k == B - 1 else p < ed[k + 1])
            if msk.sum(): e += msk.mean() * abs(p[msk].mean() - y[msk].mean())
        return e
    def rep(name, p):
        p = np.clip(p, 1e-6, 1 - 1e-6)
        print("%-32s ECE=%.4f Brier=%.4f logloss=%.4f AUC=%.4f" %
              (name, ece(p, yt), brier_score_loss(yt, p), log_loss(yt, p), roc_auc_score(yt, p)))
    print("\n=== calibrating a rating score into P(like), base rate=%.4f ===" % yt.mean())
    rep('naive (pred-1)/4', (Xt[:, 0] - 1) / 4)
    rep('naive sigmoid(pred-3.5)', 1 / (1 + np.exp(-(Xt[:, 0] - 3.5))))
    rep('Platt (2-param logistic)', LogisticRegression().fit(Xc, yc).predict_proba(Xt)[:, 1])
    rep('Isotonic', IsotonicRegression(out_of_bounds='clip').fit(Xc[:, 0], yc).predict(Xt[:, 0]))
    lz = Xc[:, 0] - 3.5
    T = minimize_scalar(lambda t: log_loss(yc, 1 / (1 + np.exp(-lz / max(t, 1e-3)))),
                        bounds=(.05, 20), method='bounded').x
    rep('Temperature scaling (T=%.3f)' % T, 1 / (1 + np.exp(-(Xt[:, 0] - 3.5) / T)))
    print("\n--- calibration-set size sweep ---")
    for k in [50, 100, 200, 500, 1000, 5000]:
        idx = np.random.RandomState(0).choice(len(yc), k, replace=False)
        p1 = LogisticRegression().fit(Xc[idx], yc[idx]).predict_proba(Xt)[:, 1]
        p2 = np.clip(IsotonicRegression(out_of_bounds='clip').fit(Xc[idx, 0], yc[idx]).predict(Xt[:, 0]), 1e-6, 1 - 1e-6)
        print(" n=%5d  Platt ECE=%.4f Brier=%.4f | Isotonic ECE=%.4f Brier=%.4f" %
              (k, ece(p1, yt), brier_score_loss(yt, p1), ece(p2, yt), brier_score_loss(yt, p2)))

if __name__ == "__main__":
    main()
