# agentic-movie-recommendation

Predicting whether a Letterboxd user will like a film they have not seen.

## What is here

- `lbfetch/` — cache-first fetcher (curl_cffi + Chrome TLS impersonation), parsers, shared rate limiter
- `scripts/` — crawl, twin-finding, agreement scoring, evaluation
- `PIPELINE.md` — the twin-finding pipeline and what it found
- `STACK.md` — endpoints, parse anchors, measured limits

## Findings so far

- Friends predict a user's ratings; co-occurrence on rare films does not
  (7.8% twin rate vs 6.5% for a 1-film control, Fisher p = 1.000).
- Friends-CF: AUC 0.875 vs 0.704 for the crowd average and 0.500 for the user mean.
- Letterboxd caps member listings at 256 pages = 6,400 users per rating level.

## Running

Crawling runs on GitHub Actions through a residential proxy, never from a
university network. `PROXY_URL` and `COHORT_JSON` are repo secrets.
