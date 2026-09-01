"""Parsers for the Letterboxd payloads we can actually reach.

Everything here is defensive on purpose. The RSS field set is not documented by
Letterboxd, it is whatever their template happens to emit, and it has changed
before. So each parser returns what it found and reports what it did not, rather
than raising -- a missing <letterboxd:memberLike> should degrade one feature,
not kill an ingest run.

Run `scripts/probe_rss.py` against a real account to see the true field set from
your own fetch before relying on any single field.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

NS = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
}

# "Parasite, 2019 - ★★★★½"  ->  stars, when memberRating is absent.
_STAR_RE = re.compile(r"(★+)(½)?\s*$")
_TMDB_ID_RE = re.compile(r'data-tmdb-id="(\d+)"')
_TMDB_TYPE_RE = re.compile(r'data-tmdb-type="(\w+)"')
_IMDB_RE = re.compile(r'imdb\.com/title/(tt\d+)')
# A diary entry's guid looks like `letterboxd-watch-<id>`; lists and plain
# reviews share the feed and must be filtered out or they become phantom films.
_WATCH_GUID_RE = re.compile(r"letterboxd-(watch|review)-(\d+)")


@dataclass
class DiaryEntry:
    film_title: Optional[str] = None
    film_year: Optional[int] = None
    tmdb_id: Optional[int] = None
    rating: Optional[float] = None       # 0.5 .. 5.0
    watched_date: Optional[str] = None   # ISO date, as Letterboxd reports it
    rewatch: bool = False
    liked: bool = False                  # the heart; orthogonal to the rating
    link: Optional[str] = None
    slug: Optional[str] = None
    review_text: Optional[str] = None


@dataclass
class DiaryFeed:
    user: str
    entries: list = field(default_factory=list)
    field_coverage: dict = field(default_factory=dict)
    skipped: int = 0

    def __repr__(self) -> str:
        return (f"<DiaryFeed {self.user} n={len(self.entries)} "
                f"skipped={self.skipped}>")


def _text(node, path: str) -> Optional[str]:
    found = node.find(path, NS)
    if found is None or found.text is None:
        return None
    value = found.text.strip()
    return value or None


def _stars_from_title(title: str) -> Optional[float]:
    m = _STAR_RE.search(title or "")
    if not m:
        return None
    return len(m.group(1)) + (0.5 if m.group(2) else 0.0)


def _slug_from_link(link: str) -> Optional[str]:
    # https://letterboxd.com/<user>/film/<slug>/  (diary entry)
    # https://letterboxd.com/film/<slug>/         (film page)
    m = re.search(r"/film/([^/]+)/", link or "")
    return m.group(1) if m else None


def parse_diary_rss(xml_text: str, user: str = "") -> DiaryFeed:
    """Parse a `/{user}/rss/` feed into diary entries.

    Capped at ~100 items by Letterboxd, newest first. Entries without a rating
    are kept -- a watch with no rating is still evidence about what the user
    chooses to see, and the 'does this user rate meaningfully' check needs to
    know how often they skip it.
    """
    feed = DiaryFeed(user=user)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.error("RSS parse failed for %s: %s", user or "?", exc)
        return feed

    coverage: Counter = Counter()

    for item in root.iter("item"):
        guid = _text(item, "guid") or ""
        if not _WATCH_GUID_RE.search(guid):
            feed.skipped += 1          # a list, or a non-film activity item
            continue

        title = _text(item, "title") or ""
        link = _text(item, "link")
        entry = DiaryEntry(link=link, slug=_slug_from_link(link or ""))

        entry.film_title = _text(item, "letterboxd:filmTitle")
        year = _text(item, "letterboxd:filmYear")
        entry.film_year = int(year) if year and year.isdigit() else None

        tmdb = _text(item, "tmdb:movieId")
        entry.tmdb_id = int(tmdb) if tmdb and tmdb.isdigit() else None

        rating = _text(item, "letterboxd:memberRating")
        if rating:
            try:
                entry.rating = float(rating)
            except ValueError:
                entry.rating = None
        if entry.rating is None:
            # Fall back to the stars glyphs in <title>, which have been there
            # far longer than the memberRating element.
            entry.rating = _stars_from_title(title)

        entry.watched_date = _text(item, "letterboxd:watchedDate")
        entry.rewatch = (_text(item, "letterboxd:rewatch") or "").lower() in ("yes", "true")
        entry.liked = (_text(item, "letterboxd:memberLike") or "").lower() in ("yes", "true")
        entry.review_text = _text(item, "description")

        for name in ("film_title", "film_year", "tmdb_id", "rating",
                     "watched_date", "review_text"):
            if getattr(entry, name) is not None:
                coverage[name] += 1
        if entry.rewatch:
            coverage["rewatch_true"] += 1
        if entry.liked:
            coverage["liked_true"] += 1

        feed.entries.append(entry)

    n = len(feed.entries) or 1
    feed.field_coverage = {k: round(v / n, 3) for k, v in coverage.items()}

    # memberLike is the field most likely to be silently absent, and a heart is
    # a genuinely separate signal from a rating -- losing it without noticing
    # would quietly remove a feature rather than break anything.
    if coverage.get("liked_true", 0) == 0 and feed.entries:
        log.info("no <letterboxd:memberLike> seen for %s -- either this user "
                 "hearts nothing, or the field is not in the feed. Check "
                 "against an account you know uses hearts.", user or "?")
    return feed


def parse_film_ids(html: str) -> dict:
    """Pull the id bridges out of a film page.

    `data-tmdb-id` is the documented-by-observation route from a Letterboxd slug
    to TMDB, and the page also links IMDb, which is what you actually want:
    IMDb's tconst keys the free bulk datasets and Trakt's ratings endpoint.
    """
    out = {"tmdb_id": None, "tmdb_type": None, "imdb_id": None}
    m = _TMDB_ID_RE.search(html or "")
    if m:
        out["tmdb_id"] = int(m.group(1))
    m = _TMDB_TYPE_RE.search(html or "")
    if m:
        out["tmdb_type"] = m.group(1)
    m = _IMDB_RE.search(html or "")
    if m:
        out["imdb_id"] = m.group(1)
    return out


def rating_profile(entries) -> dict:
    """Does this user rate meaningfully, or are they just logging?

    The first gate in the whole pipeline. A user whose ratings carry no
    information cannot be personalised to, and predicting for them is worse
    than useless because the evaluation will look fine while the model is
    fitting noise. Reported, not enforced -- the caller decides the threshold.
    """
    rated = [e.rating for e in entries if e.rating is not None]
    n_total, n_rated = len(entries), len(rated)
    if not rated:
        return {"n_total": n_total, "n_rated": 0, "rate_fraction": 0.0,
                "usable": False, "reason": "no ratings at all"}

    mean = sum(rated) / n_rated
    var = sum((r - mean) ** 2 for r in rated) / n_rated
    sd = var ** 0.5

    hist = Counter(rated)
    modal_share = max(hist.values()) / n_rated
    # Shannon entropy over the 10 half-star buckets, normalised to [0, 1].
    import math
    ent = -sum((c / n_rated) * math.log(c / n_rated, 2) for c in hist.values())
    ent_norm = ent / math.log(10, 2)
    uses_half_stars = any(abs(r * 2 - round(r * 2)) < 1e-9 and (r * 2) % 2 == 1
                          for r in rated)

    reasons = []
    if sd < 0.5:
        reasons.append(f"sd={sd:.2f} too low")
    if modal_share > 0.5:
        reasons.append(f"{modal_share:.0%} of ratings are a single value")
    if n_rated < 30:
        reasons.append(f"only {n_rated} ratings")

    return {
        "n_total": n_total,
        "n_rated": n_rated,
        "rate_fraction": round(n_rated / n_total, 3) if n_total else 0.0,
        "mean": round(mean, 3),
        "sd": round(sd, 3),
        "modal_share": round(modal_share, 3),
        "entropy_norm": round(ent_norm, 3),
        "uses_half_stars": uses_half_stars,
        "usable": not reasons,
        "reason": "; ".join(reasons) or "ok",
    }


# --- /{user}/films/ poster grid ------------------------------------------
#
# Letterboxd renamed these attributes at some point: it is `data-item-slug`
# now, not the `data-film-slug` that every scraper tutorial online still uses.
# Verified against live markup 2026-08-26.
#
# One <li class="griditem"> per film:
#   data-item-slug="one-battle-after-another"
#   data-item-name="One Battle After Another (2025)"
#   data-postered-identifier='{... "uid":"film:951277" ...}'
#   <p class="poster-viewingdata" data-item-uid="film:951277">
#       <span class="rating -micro -darker rated-7">★★★½</span>   <- rated-N, N/2 stars
#   </p>
# An empty <p> means watched-but-unrated, which is itself a signal and is kept.
# Letterboxd uses at least two container classes for the same poster payload:
# `griditem` on /{u}/films/ and `posteritem` on /{u}/likes/films/ and list
# pages. Matching only the first silently returns zero films from the others,
# which reads as "this user has liked nothing" rather than as a parser bug.
_GRIDITEM_RE = re.compile(r'<li class="(?:griditem|posteritem)"[^>]*>(.*?)</li>', re.S)
_ITEM_SLUG_RE = re.compile(r'data-item-slug="([^"]+)"')
_ITEM_NAME_RE = re.compile(r'data-item-name="([^"]*)"')
_ITEM_UID_RE = re.compile(r'film:(\d+)')
_RATED_RE = re.compile(r'\brated-(\d{1,2})\b')
_NAME_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")


@dataclass
class FilmEntry:
    slug: Optional[str] = None
    title: Optional[str] = None
    year: Optional[int] = None
    lb_film_id: Optional[int] = None    # Letterboxd's own id, not TMDB
    rating: Optional[float] = None      # 0.5 .. 5.0, None = watched unrated


def parse_films_page(html: str) -> list:
    """Parse one page of `/{user}/films/`. 72 films per page."""
    out = []
    for block in _GRIDITEM_RE.finditer(html or ""):
        chunk = block.group(1)
        m = _ITEM_SLUG_RE.search(chunk)
        if not m:
            continue
        entry = FilmEntry(slug=m.group(1))

        m = _ITEM_NAME_RE.search(chunk)
        if m:
            name = m.group(1)
            ym = _NAME_YEAR_RE.match(name)
            if ym:
                entry.title, entry.year = ym.group(1), int(ym.group(2))
            else:
                entry.title = name

        m = _ITEM_UID_RE.search(chunk)
        if m:
            entry.lb_film_id = int(m.group(1))

        # Take the rating from the viewingdata paragraph only. `rated-` also
        # appears in the page's sort/filter navigation, so a document-wide
        # search over-counts badly.
        vd = chunk.find("poster-viewingdata")
        if vd >= 0:
            m = _RATED_RE.search(chunk, vd)
            if m:
                entry.rating = int(m.group(1)) / 2.0

        out.append(entry)
    return out


def films_profile(entries) -> dict:
    """rating_profile() over FilmEntry objects from the films grid."""
    return rating_profile(entries)


# --- /{user}/following/ and /followers/ ----------------------------------
#
# One <td class="col-member table-person"> per person, plus a films-watched
# count in the adjacent stats cell. That count is the useful part: it tells you
# how much history a person has BEFORE you spend requests fetching them, so a
# friend-agreement weighting can skip accounts too thin to weight.
_PERSON_RE = re.compile(r'<div class="person-summary">(.*?)</div>', re.S)
_PERSON_ROW_RE = re.compile(r'<td class="col-member table-person">(.*?)</tr>', re.S)
_PERSON_NAME_RE = re.compile(r'<a href="/([^/"]+)/" class="name">\s*(.*?)\s*</a>', re.S)
_PERSON_FOLLOWERS_RE = re.compile(r'([\d,]+)\s*(?:&nbsp;|\s)followers')
_PERSON_FILMS_RE = re.compile(r'icon-watched" href="/[^/"]+/films/">([\d,]+)</a>')


@dataclass
class Person:
    username: Optional[str] = None
    display_name: Optional[str] = None
    n_films: Optional[int] = None
    n_followers: Optional[int] = None


def _int(text) -> Optional[int]:
    if not text:
        return None
    try:
        return int(text.replace(",", "").replace("&nbsp;", "").strip())
    except ValueError:
        return None


def parse_people_page(html: str) -> list:
    """Parse one page of `/{user}/following/` or `/{user}/followers/`."""
    out = []
    for row in _PERSON_ROW_RE.finditer(html or ""):
        chunk = row.group(1)
        m = _PERSON_NAME_RE.search(chunk)
        if not m:
            continue
        p = Person(username=m.group(1), display_name=_strip_tags(m.group(2)))
        m = _PERSON_FOLLOWERS_RE.search(chunk)
        if m:
            p.n_followers = _int(m.group(1))
        m = _PERSON_FILMS_RE.search(chunk)
        if m:
            p.n_films = _int(m.group(1))
        out.append(p)
    return out


# --- /film/{slug}/reviews/ ------------------------------------------------
#
# 12 reviews per page, one per <article class="production-viewing -viewing">.
# NB it is an <article>, not the <li class="listitem"> you would guess from the
# surrounding grid markup, and the article carries no closing-tag-safe nesting
# -- so segment by splitting on the opening tag rather than matching a pair.
# The rating is NOT a
# `rated-N` class here as it is on the poster grids -- it is inline SVG with the
# star glyphs in an aria-label. Review text is TRUNCATED in the listing with a
# `data-full-text-url` pointing at /s/full-text/viewing:<id>/; fetch that only
# for reviews you actually need, it is one request each.
_REVIEW_SPLIT_RE = re.compile(r'<article class="production-viewing[^>]*>')
_REVIEW_USER_RE = re.compile(r'<a href="/([^/"]+)/film/[^"]*"\s+class="context"')
_REVIEW_DISPLAY_RE = re.compile(r'<strong class="displayname">(.*?)</strong>', re.S)
_REVIEW_STARS_RE = re.compile(r'class="glyph -rating"[^>]*aria-label="([★½]+)"')
_REVIEW_DATE_RE = re.compile(r'<time class="timestamp" datetime="([^"]+)"')
_REVIEW_FULLTEXT_RE = re.compile(r'data-full-text-url="([^"]+)"')
_REVIEW_VIEWING_RE = re.compile(r'viewing:(\d+)')
_REVIEW_COMMENTS_RE = re.compile(r'#comments"[^>]*>.*?<span class="label">([\d,]+)</span>', re.S)
# The body is whatever sits between the js-review-body div and the actions
# block. `collapsed-text` only wraps SPOILER-hidden reviews, so keying on it
# silently returns text for those and nothing for everyone else -- which reads
# as "most reviews have no text" rather than as a bug.
_REVIEW_BODY_RE = re.compile(
    r'class="[^"]*js-review-body[^"]*"[^>]*>(.*?)<div class="viewing-actions"', re.S)
# Review like count, off the LikeComponent that follows every review.
_REVIEW_LIKES_RE = re.compile(r'data-component-class="LikeComponent".*?data-count="([\d,]+)"', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Review:
    username: Optional[str] = None
    display_name: Optional[str] = None
    rating: Optional[float] = None
    liked: bool = False
    spoiler: bool = False
    date: Optional[str] = None
    text: Optional[str] = None          # truncated as shown in the listing
    truncated: bool = False
    full_text_url: Optional[str] = None
    viewing_id: Optional[int] = None
    n_comments: Optional[int] = None
    n_likes: Optional[int] = None


def _strip_tags(html: str) -> str:
    import html as _html
    text = _TAG_RE.sub(" ", html or "")
    return " ".join(_html.unescape(text).split())


def _stars_to_rating(glyphs: str) -> Optional[float]:
    if not glyphs:
        return None
    return glyphs.count("★") + (0.5 if "½" in glyphs else 0.0)


def parse_reviews_page(html: str) -> list:
    """Parse one page of `/film/{slug}/reviews/by/{sort}/`."""
    out = []
    for chunk in _REVIEW_SPLIT_RE.split(html or "")[1:]:
        m = _REVIEW_USER_RE.search(chunk)
        if not m:
            continue
        r = Review(username=m.group(1))

        m = _REVIEW_DISPLAY_RE.search(chunk)
        if m:
            r.display_name = _strip_tags(m.group(1))
        m = _REVIEW_STARS_RE.search(chunk)
        if m:
            r.rating = _stars_to_rating(m.group(1))
        m = _REVIEW_DATE_RE.search(chunk)
        if m:
            r.date = m.group(1)
        m = _REVIEW_FULLTEXT_RE.search(chunk)
        if m:
            r.full_text_url = m.group(1)
            vm = _REVIEW_VIEWING_RE.search(m.group(1))
            if vm:
                r.viewing_id = int(vm.group(1))
        m = _REVIEW_COMMENTS_RE.search(chunk)
        if m:
            r.n_comments = _int(m.group(1))
        m = _REVIEW_BODY_RE.search(chunk)
        if m:
            r.text = _strip_tags(m.group(1))
            r.truncated = r.text.endswith("…")
        m = _REVIEW_LIKES_RE.search(chunk)
        if m:
            r.n_likes = _int(m.group(1))

        r.liked = "inline-liked" in chunk or "<title>Liked</title>" in chunk
        r.spoiler = "js-spoiler-container" in chunk or "may contain spoilers" in chunk
        out.append(r)
    return out


# --- /csi/film/{slug}/rating-histogram/ -----------------------------------
#
# Counts live in the bar tooltips: title="5,454 half-★ ratings (0%)".
# These are what decide whether enumerating a film's raters is feasible at all:
# `/film/{slug}/members/rated/{r}/` pages 25 at a time, so a 700k-rater level is
# 28,000 requests and a 400-rater level is 16.
_HIST_RE = re.compile(r'title="([\d,]+)\s+([^"]*?)\s+ratings')
_STAR_TOKEN = {"half-★": 0.5}


def parse_rating_histogram(html: str) -> dict:
    """-> {stars: count}. Missing levels mean zero ratings at that level."""
    out = {}
    for m in _HIST_RE.finditer(html or ""):
        n = _int(m.group(1))
        token = m.group(2).strip()
        if token in _STAR_TOKEN:
            stars = _STAR_TOKEN[token]
        else:
            stars = token.count("★") + (0.5 if "½" in token else 0.0)
        if stars and n is not None:
            out[stars] = n
    return out


def pages_to_enumerate(hist: dict, min_stars: float = 4.5, per_page: int = 25) -> int:
    """How many requests to list every rater at or above `min_stars`."""
    return sum(-(-n // per_page) for s, n in hist.items() if s >= min_stars)


# The poster is NOT in the /{user}/films/ grid markup -- those <img> tags all
# carry `empty-poster-70-*.png` and the real image is lazy-loaded by JS. The
# film page's JSON-LD carries it, and the resizer encodes the dimensions in the
# path, so any size can be requested by rewriting them.
# Two poster path shapes coexist: older uploads use `/resized/film-poster/<id
# digits>/<id>-<slug>-...`, newer ones a hashed `/resized/sm/upload/xx/yy/...`.
# Matching only the first silently loses every recent film -- Dune, Tenet,
# Dunkirk all missed on the first pass.
_POSTER_RE = re.compile(r'"image"\s*:\s*"(https://a\.ltrbxd\.com/resized/[^"]+)"')
_POSTER_DIMS_RE = re.compile(r'-(\d+)-(\d+)-(\d+)-(\d+)-crop\.jpg')


def parse_poster_url(html: str, width: int = 125, height: int = 187):
    m = _POSTER_RE.search(html or "")
    if not m:
        return None
    url = m.group(1).replace("\\/", "/")
    return _POSTER_DIMS_RE.sub(f"-0-{width}-0-{height}-crop.jpg", url, count=1)
