# Exact stack, endpoints and parse anchors — and how to parallelise this

Everything below is what is actually running as of 2026-08-26, verified from
`labsrv7.math.unipd.it` (147.162.22.60, AS137 GARR). Not a plan — an inventory.

---

## 1. Environment

| | |
|---|---|
| Python | 3.10.19, venv at `.venv`, base `/home/malmasik/.venvs/tf-portable` |
| Public identity | `147.162.22.60` / `labsrv7.math.unipd.it` / AS137 Consortium GARR / Rovigo, IT |
| Project root | `/extra/malmasik/letterboxd` (NFS4 — matters, see §5) |

```bash
/home/malmasik/.venvs/tf-portable/bin/python -m venv .venv   # ~4 min on NFS
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium                        # ~115MB
```

### Dependencies — three, and only one is load-bearing

| package | version | role |
|---|---|---|
| `curl_cffi` | 0.16.2 | **the whole unlock.** `Session(impersonate="chrome")` |
| `playwright` | 1.62.0 | escalation path. Used 0 times from this IP |
| `requests` | — | fallback only, warns when it takes over |

Browsers on disk: `chromium-1228`, `chromium-1234`, `chromium_headless_shell-*`.

**No lxml. No BeautifulSoup.** All parsing is stdlib `re` + `xml.etree.ElementTree`.
They were installed, never imported, and have been removed. The fragile part of
scraping Letterboxd is the anchor strings, not the parser.

---

## 2. The four techniques that actually mattered

**1. TLS impersonation.** Plain `urllib`/`requests` → `403` on every Letterboxd
URL from this network. `curl_cffi` with `impersonate="chrome"` → `200` on every
one. Python's JA3 fingerprint does not match the Chrome User-Agent being sent,
and Cloudflare blocks the mismatch. One line, and it is the difference between
the project existing and not.

**2. Persistent browser context on LOCAL disk.** `launch_persistent_context(user_data_dir=…)`
so `cf_clearance` survives across runs instead of re-solving every request.
Chromium's profile is sqlite/leveldb and NFS does not honour its locks:

```
profile on /extra (NFS4)  →  page.goto TimeoutError after 30.0s
profile on /tmp  (ext4)   →  HTTP 200 in 1.4s
```

Default is `/tmp/lb-browser-profile-$USER`; `Fetcher` warns if pointed at
`/extra` or `/home`. Cluster hosts also need `--no-sandbox --disable-dev-shm-usage`.

**3. Two different challenge detectors.** Cloudflare injects its bot-management
beacon from `/cdn-cgi/challenge-platform/` into **every** page it fronts,
including successful ones. So:
- raw body (curl_cffi): substring markers, and **only** when status is 403/503/429
- rendered DOM (Playwright): page **title** + widget selector, never a substring scan

Getting this backwards produces false *positives* — good pages discarded as
blocked, which fails silently and looks exactly like a block.

**4. Permanent content-addressed cache.** 200s cached forever, failures 6h TTL.
`sha256(url)` → `cache/<host>/<xx>/<sha>.json.gz`, written temp-then-`rename()`.
This is the politeness mechanism, not a speed optimisation.

---

## 3. Endpoints — all verified 200 via plain curl_cffi, no browser, no proxy

| path | gives | page size |
|---|---|---|
| `/{user}/rss/` | last ~50–100 diary entries **with `tmdb_id`, `watchedDate`, `memberLike`, `rewatch`** | 50 items |
| `/{user}/films/` `…/page/N/` | **complete** watched list + ratings (no dates, no tmdb) | 72/page |
| `/{user}/following/` `/followers/` | username, display name, **films-watched count**, follower count | 14/page |
| `/film/{slug}/` | `data-tmdb-id`, `data-tmdb-type`, IMDb `tt…` link | — |
| `/film/{slug}/reviews/by/{sort}/` `…page/N/` | reviewer, rating, text, likes, comments count | 12/page |
| `/csi/film/{slug}/rating-histogram/` | rating distribution | 5.8KB |
| `/{user}/friends/film/{slug}/` | which followed accounts rated a film | — |
| `/s/full-text/viewing:{id}/` | untruncated review body | 1 req each |

`sort` ∈ `activity | added | rating | entry-rating`.

**The two halves are asymmetric and that is the key scaling fact.** Film-level
paths are user-independent → fetch once, reuse for every user forever. Only
`/{user}/…` paths cost per user.

---

## 4. Parse anchors — the fragile part, verified 2026-08-26

**Films grid** `/{user}/films/`
```
<li class="griditem">                       segment
data-item-slug="one-battle-after-another"   NOT data-film-slug (renamed; every
data-item-name="One Battle After Another (2025)"   tutorial online is stale)
"uid":"film:951277"                         Letterboxd film id, not TMDB
<p class="poster-viewingdata"> … rated-7    rating = N/2; anchor the search to
                                            poster-viewingdata, `rated-` also
                                            appears in the sort/filter nav
```

**People** `/{user}/following/`
```
<td class="col-member table-person">        segment (to </tr>)
<a href="/{username}/" class="name">Name</a>
icon-watched" href="/{u}/films/">453</a>    films watched
([\d,]+)\s*(?:&nbsp;|\s)followers
```

**Reviews** `/film/{slug}/reviews/…`
```
<article class="production-viewing …>       segment — an <article>, NOT the
                                            <li class="listitem"> nearby
<a href="/{user}/film/…" class="context">   username
<strong class="displayname">                display name
class="glyph -rating" … aria-label="★★★½"   rating — inline SVG here, NOT
                                            rated-N like the poster grids
<time class="timestamp" datetime="…">       date
js-review-body …> … <div class="viewing-actions">   body lives BETWEEN these.
                                            `collapsed-text` wraps ONLY
                                            spoiler-hidden reviews — keying on
                                            it returns text for 2/12 and reads
                                            as "most reviews have no text"
LikeComponent … data-count="42489"          review likes
#comments"…<span class="label">247</span>   comment count
data-full-text-url="/s/full-text/viewing:N/"  untruncated body
inline-liked / <title>Liked</title>         heart
```

**RSS** — namespaces `letterboxd="https://letterboxd.com"`, `tmdb="https://themoviedb.org"`.
Filter guids on `letterboxd-(watch|review)-\d+` or lists become phantom films.
Star glyphs in `<title>` are a fallback when `memberRating` is absent.

**Film page** — `data-tmdb-id="(\d+)"`, `data-tmdb-type`, `imdb\.com/title/(tt\d+)`.

---

## 5. Config

| env var | default | |
|---|---|---|
| `LB_PROXY_URL` | falls back to `JOBTOOLS_PROXY_URL` | both transports |
| `LB_MIN_INTERVAL_S` | `3.0` | per-host spacing |
| `LB_JITTER_S` | `1.5` | uniform jitter on top |
| `LB_PROFILE_DIR` | `/tmp/lb-browser-profile-$USER` | must be local disk |
| `LB_HEADLESS` | `1` | |

---

## 6. Parallelising this — read before you fan out

### The blocker: the rate limiter is per-process and in-memory

`RateLimiter` keeps `{host: last_time}` in a dict behind a `threading.Lock`.
Threads inside one process share it. **N processes do not.** Launch 8 workers
and you are making 8× the agreed request rate at a shared university IP while
every worker believes it is being polite. This is the single thing to fix first.

The fix is a shared token bucket — a small file under `flock`, holding the next
allowed timestamp per host:

```python
with open(lockfile, "r+") as fh:
    fcntl.flock(fh, fcntl.LOCK_EX)
    next_at = float(fh.read() or 0)
    now = time.time()
    slot = max(now, next_at)
    fh.seek(0); fh.truncate(); fh.write(str(slot + interval + jitter))
    fcntl.flock(fh, fcntl.LOCK_UN)
time.sleep(max(0, slot - now))          # sleep OUTSIDE the lock
```

**Hold the lock only to claim a slot; never across the network call or the
sleep.** That is the same failure that caused the job-monitor death spiral —
a single-writer lock held across slow work serialises everything behind it and
then looks like a hang rather than contention.

### What is already safe

- **The cache.** Writes are temp-then-`rename()`, which is atomic on both ext4
  and NFS. Two workers fetching the same URL race harmlessly to an identical
  result. Concurrent reads are fine.
- **The parsers.** Pure functions over a string, no shared state.

### What is genuinely parallel — and what only looks like it

| work | parallel? | why |
|---|---|---|
| parsing cached bodies | **yes, fully** | CPU-bound, no network. `multiprocessing.Pool` over the cache |
| different hosts (Trakt / IMDb dumps / Wikidata) | **yes** | separate rate budgets; run alongside Letterboxd |
| Letterboxd fetches, same IP | **no** | one shared budget no matter how many workers |
| Letterboxd fetches, N distinct proxy exits | **yes, ~N×** | genuinely independent IPs — the only real speedup |

So the honest answer: **against one IP there is no speedup available.** The
3s spacing is the deliberate bottleneck. Parallelism buys you throughput only
by adding exits, and it buys you a lot on the parse/analysis side, which is
where the CPU time actually goes once the cache is warm.

### Order the work to exploit the asymmetry

Film-level artefacts are user-independent. Fetch them **once**, before any
per-user fan-out, and every subsequent user is nearly free:

```
1. film pages + reviews + histograms   → cache, shared across all users forever
2. per-user /films/ + /rss/ + /following/
3. friends' /films/  (sort by n_films; skip thin accounts, they cannot carry weight)
```

Shard by `sha256(url)` so workers own disjoint URL sets and never duplicate a
fetch. Do not shard by user — users share films, and you would refetch them.

### The proxy is NOT reachable from the cluster — measured 2026-08-26

`labsrv7` cannot reach **any** commercial proxy provider. Not a DataImpulse
problem, not an account problem — a category block on the university network:

```
gw.dataimpulse.com:823        ConnectionRefused    brd.superproxy.io:22225   ConnectionRefused
gw.dataimpulse.com:443        ConnectionRefused    pr.oxylabs.io:7777        ConnectionRefused
gw.dataimpulse.com:80         ConnectionRefused    gate.smartproxy.com:7000  ConnectionRefused
gw.dataimpulse.com:10000      ConnectionRefused    proxy.packetstream.io     ConnectionRefused
github.com:22   OK            pypi.org:443  OK     ← ordinary hosts are fine
```

Instant TCP RST on every port, while `github.com:22` connects — so it is a
destination-category reject, not a port policy and not the provider refusing us.
Independently recorded in `vinted/PROJECT_BRIEF.md` line 7 on 2026-07-28.

**Consequence: `LB_PROXY_URL` cannot be used from `labsrv7`.** Anything that
needs a non-university IP has to run off-cluster.

Unlike Vinted, this project does not *need* the proxy — every Letterboxd
endpoint returns 200 direct from the university IP. The proxy is only about
blast radius. So:

| work | where | why |
|---|---|---|
| per-user `/films/`, `/rss/`, `/following/` | **cluster, direct** | seconds to minutes per user; volume is trivially defensible |
| bulk film-level (reviews for thousands of films) | **GitHub Actions + DataImpulse** | the established vinted pattern; residential Italy exit, not the university |
| population-scale ratings | **neither — Kaggle dump** | 11k users / 18M ratings, redistributable, and required anyway to avoid fitting global coefficients on your eval users |

Caveat worth probing before committing: GitHub Actions runners are **datacenter**
IPs, and the 403 map that this project originally worked from came from a
datacenter exit. A runner may get challenged on paths the university IP sails
through. Run `scripts/probe_paths.py` on the runner, both direct and through the
proxy, before moving any bulk job there.

### Before any bulk run

- [ ] **429 backoff — missing.** `Fetcher` escalates on a challenge but does not
      back off on rate-limit. Add exponential backoff + `Retry-After` honouring.
- [ ] Shared token bucket (above) if more than one process.
- [ ] Route through the residential proxy with a **sticky session**
      (`…__cr.it;sid.<random>`) — `cf_clearance` is bound to IP *and* UA, so a
      rotating exit invalidates it every request and is worse than direct.
- [ ] `.gitignore` for `data/` and `cache/`.

### Cost arithmetic

At 4.5s average, one IP = **~800 requests/hour**.

| job | requests | wall clock |
|---|---|---|
| one user's full history | ~3–34 | seconds |
| all 14 of amindoalamas' friends' histories | 228 | ~17 min |
| 3 pages of reviews for 1,000 films | 3,000 | ~3.7 h |
| 50 users + their friends | ~10,000 | ~12 h |

Days of continuous traffic from a university IP is where I would stop and use
the proxy or the public Kaggle dump instead.
