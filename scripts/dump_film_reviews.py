"""Pull the reviews for a film: who wrote them, their rating, and the text.

12 reviews per page. A popular film has thousands, so `--pages` is mandatory
thinking, not a default to ignore: Parasite alone would be several hundred
requests to exhaust. For the aspect-card use case you want the top few pages by
activity, not the whole tail -- the signal saturates fast and the cost does not.

Review text in the listing is TRUNCATED for long reviews (`truncated: true`).
The full text needs one extra request each to `data-full-text-url`; --full
does that, and it is off by default because it multiplies request count by up
to 12x per page.

    python scripts/dump_film_reviews.py parasite-2019 --pages 3 --out data/reviews/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lbfetch import paths
from lbfetch.fetch import Fetcher
from lbfetch.parse import parse_reviews_page, _strip_tags

BASE = "https://letterboxd.com"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--sort", default="activity",
                    help="activity | added | rating (high first) | entry-rating")
    ap.add_argument("--full", action="store_true",
                    help="fetch untruncated text (1 extra request per truncated review)")
    ap.add_argument("--out", default=None, help="directory; writes <slug>.json")
    ap.add_argument("--proxy", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    reviews = []
    with Fetcher(proxy_url=args.proxy) as f:
        for page in range(1, args.pages + 1):
            url = paths.film_reviews(args.slug, args.sort)
            if page > 1:
                url = f"{url}page/{page}/"
            entry = f.get(url)
            if not entry.ok:
                logging.warning("page %d: HTTP %d -- stopping", page, entry.status)
                break
            batch = parse_reviews_page(entry.body)
            if not batch:
                break
            reviews.extend(batch)
            logging.info("page %d: %d reviews (total %d)", page, len(batch), len(reviews))

        if args.full:
            todo = [r for r in reviews if r.truncated and r.full_text_url]
            logging.info("fetching %d untruncated bodies", len(todo))
            for r in todo:
                e = f.get(BASE + r.full_text_url)
                if e.ok:
                    r.text = _strip_tags(e.body)
                    r.truncated = False

    rated = [r for r in reviews if r.rating is not None]
    print(f"\n{args.slug}: {len(reviews)} reviews, {len(rated)} with a rating")
    if rated:
        print(f"  mean reviewer rating {sum(r.rating for r in rated)/len(rated):.2f}")
    print(f"  with text {sum(1 for r in reviews if r.text)}  "
          f"still truncated {sum(1 for r in reviews if r.truncated)}  "
          f"spoiler-flagged {sum(1 for r in reviews if r.spoiler)}")

    for r in reviews[:5]:
        print(f"\n  {r.username} — {r.rating} — {r.n_likes} likes, {r.n_comments} comments")
        print(f"    {(r.text or '')[:160]}")

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        path = os.path.join(args.out, f"{args.slug}.json")
        with open(path, "w") as fh:
            json.dump({"slug": args.slug, "sort": args.sort,
                       "reviews": [asdict(r) for r in reviews]}, fh, indent=1)
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
