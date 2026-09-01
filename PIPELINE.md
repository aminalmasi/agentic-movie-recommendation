# Twin-finding pipeline — what it does, what it found

Status as of 2026-08-28. The pipeline **works**; the hypothesis it was built to
test **did not survive**. Both facts are recorded here because the machinery is
reusable and the null result is the expensive part to rediscover.

---

## What it does, end to end

Input: a Letterboxd username. Output: ranked lists of "twins".

```
 backfill_histograms.py   every watched film -> exact rater count per star level
        v
 make_picker.py           -> a page for choosing seed films, with live crawl budget
        v
 find_twins.py            STAGE 1  enumerate who else rated/hearted the seeds
        v
 seed_agreement.py        STAGE 1.5  (free) their exact rating on each seed
        v
 verify_twins.py          STAGE 2  agreement over ALL co-rated films
        v
 predict_eval.py          does any of it predict held-out ratings?
```

### Two kinds of twin, and they are different things

| kind | definition | source | what it measures |
|---|---|---|---|
| **coverage / territory twin** | has *seen* the same rare films, any rating | `/film/{slug}/members/` | exposure, discovery path |
| **taste twin** | rated the same films >= 4 | `/film/{slug}/members/rated/{r}/` | preference |
| **mirror twin** | consistent *anti*-correlation | falls out of stage 2 | evidence with the sign flipped |

Hearts (`/film/{slug}/likes/`) are a **third, weaker channel**. Keep them separate
from ratings when selecting neighbours: pooling them was a real bug (see below).

### Seed selection

Seeds are the user's own films used as hooks. Rarity is what makes a co-rating
informative *and* cheap, so the rule is inverse document frequency:

- pool < ~120 raters -> too small to intersect with anything
- pool > 6,400 per star level -> **Letterboxd truncates** (see caps)
- the usable band is everything between

For a 170-film history, only ~34 films qualify. 14 seeds cost 7,283 pages,
9 hours, ~$0.11 of proxy traffic.

---

## Hard limits, all measured

| limit | value | consequence |
|---|---|---|
| member list pagination | **256 pages = 6,400 users** per (film, star level) | bigger pools are truncated; "did NOT rate it" is uninformative there |
| RSS feed | ~50 entries | fine for a light user, useless for a heavy one; carries `watchedDate` + `memberLike`, which `/films/` does not |
| `/films/` grid | 72 per page, no dates | full history but no temporal information |
| bytes per useful fact | ~900 B per (user, rating) | 96x overhead; there is no lighter endpoint (see below) |

**There is no lighter endpoint.** Probed and refused: `/csi/`, `/s/`, `/ajax/`,
`/json/` suffix all 404; `Accept: application/json` and `X-Requested-With` are
ignored; a mobile User-Agent gets the identical page; `accept-ranges: none` and a
`Range:` request returns **206 with zero bytes**. One members page is 126 KB raw
/ 22.9 KB gzipped and contains 393 B of information.

---

## What it found

### Stage 1 works
134,677 candidates from 7,283 pages. Real co-occurrence tail:
`1:128,466  2:5,528  3:548  4:91  5:23  6:11  7:5  8:3  9:1  11:1`.
With only 3 seeds an earlier run found 8 people at 2 shared films and none above,
so **the tail needs many seeds to appear at all**.

### The bug that mattered
Stage 1 counted "rated >= 4" and "hearted" as the same evidence. Two of the top
candidates (`steakncake`, `think_pink00`) had **zero** rating-based matches --
all hearts. Rebuilt on ratings only, the ceiling drops from 11 shared seeds to 6.

### The null result
Deep agreement verification, rating-only neighbours, versus a control of users
sharing exactly **one** seed at random:

| group | twins (r >= 0.5) | rate |
|---|---|---|
| shares >= 3 rated seeds | 6 / 77 | 7.8% |
| **control: shares 1 seed** | 5 / 77 | 6.5% |

**Fisher exact two-sided p = 1.000.** Mean \|r\| 0.229 treatment vs 0.238 control.

Sharing six rare films predicts your taste no better than sharing one by
coincidence. `friedrichschoe` is the emblem: 6 shared rated seeds, r = **-0.58**
on everything else. Co-loving obscure films predicts **exposure**, not agreement.

### Prediction, held-out temporal split (32 films)

| model | RMSE |
|---|---|
| global mean | 1.2158 |
| LB average | 1.2406 |
| user mean | 0.9036 |
| **user_mean + (LB_avg - global)** | **0.8993** |
| TWIN CF (hearts pooled) | 0.9959 |
| TWIN CF (ratings only) | 1.0717 |

Neighbour CF loses to predicting the user's own average. Removing hearts made it
worse, not better. Note the like/dislike accuracy is 78% for *every* model
including the global mean -- that is the majority-class rate, so accuracy is
uninformative here and should not be reported.

---

## Reusing this

The machinery is sound and the crawl is cached permanently, so re-running costs
nothing. If you come back to it:

1. **The remaining confound.** ~6% twins in *both* arms may just be the base rate
   for any two Letterboxd users correlating at r>=0.5 over 6+ films. A third arm
   of users drawn with no reference to the target would separate "co-occurrence
   adds nothing" from "r>=0.5 is easy to hit by chance". ~1 hour.
2. **Territory twins are untested.** Everything above tested *taste* twins. The
   coverage/exposure variant seeds from the ultra-rare films that were discarded
   as too small for taste -- a 176-watcher film is 8 pages and having seen it at
   all is a strong signal. The bands are complementary.
3. **Long runs are fine.** 16h+ is acceptable; the crawler checkpoints every 100
   pages to `<out>.partial`, and the page cache makes any restart free.
4. **Results by email** is not built. `data/*.json` is the handoff.

## Operational notes

- Rate: `LB_MIN_INTERVAL_S=4.0 LB_JITTER_S=2.0` -> 5 s/request, measured. The
  limiter is file-backed and **shared across processes**, so two jobs interleave
  rather than doubling the request rate.
- The proxy is **unreachable from labsrv7** -- GARR blocks commercial proxy
  providers as a destination category (DataImpulse, Bright Data, Oxylabs,
  Smartproxy, PacketStream all refused). Off-cluster runs need GitHub Actions.
- Never `pkill -f` on a pattern that matches your own shell command. It kills the
  shell.
