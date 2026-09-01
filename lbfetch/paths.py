"""Letterboxd URL builders, and what we know about reaching each one.

TRANSPORT is a claim about Cloudflare's behaviour, and Cloudflare's behaviour
depends on the IP asking. Everything in the table below was reported from a
datacenter exit that is not yours, so treat it as a hypothesis and re-derive it
with `scripts/probe_paths.py` before designing around it. The whole reason the
probe script exists is that this table goes stale.

  "http"     -- plain transport was enough (cheap; prefer these)
  "browser"  -- served a Cloudflare interstitial, needs the persistent context
  "?"        -- not yet probed from this machine
"""

from __future__ import annotations

BASE = "https://letterboxd.com"

# --- user's own data -------------------------------------------------------

def user_rss(user: str) -> str:
    """Most recent ~100 diary entries. Published feed, no auth.

    Carries <tmdb:movieId>, <letterboxd:memberRating>, <letterboxd:watchedDate>,
    <letterboxd:rewatch> and <letterboxd:memberLike>. This is the whole diary
    signal for a light user and the recent tail for a heavy one -- the 100-item
    cap is hard, so full histories come from the account CSV export instead.
    """
    return f"{BASE}/{user}/rss/"


def user_films(user: str, page: int = 1) -> str:
    """Watched-films grid. Ratings are in the poster markup."""
    suffix = "" if page == 1 else f"page/{page}/"
    return f"{BASE}/{user}/films/{suffix}"


def user_following(user: str, page: int = 1) -> str:
    suffix = "" if page == 1 else f"page/{page}/"
    return f"{BASE}/{user}/following/{suffix}"


def user_followers(user: str, page: int = 1) -> str:
    suffix = "" if page == 1 else f"page/{page}/"
    return f"{BASE}/{user}/followers/{suffix}"


# --- film-level, user-independent (cache once, reuse for every user) -------

def film(slug: str) -> str:
    """Film page. Carries data-tmdb-id, data-tmdb-type and the IMDb link --
    this is the slug -> TMDB/IMDb bridge."""
    return f"{BASE}/film/{slug}/"


def film_reviews(slug: str, sort: str = "activity") -> str:
    return f"{BASE}/film/{slug}/reviews/by/{sort}/"


def film_rating_histogram(slug: str) -> str:
    """Internal component endpoint behind the rating bars on a film page."""
    return f"{BASE}/csi/film/{slug}/rating-histogram/"


# --- the social signal -----------------------------------------------------

def friends_on_film(user: str, slug: str) -> str:
    """Which accounts `user` follows have logged `slug`, and what they gave it.

    The highest-value single source in the whole design and, predictably, the
    one most likely to sit behind the interstitial. If this is browser-only,
    the cheap substitute is to pull each followed account's RSS and join on
    tmdb_id locally -- fewer films covered, but no challenge to solve.
    """
    return f"{BASE}/{user}/friends/film/{slug}/"


# Measured, not assumed. Update with `scripts/probe_paths.py` output + a date.
#
# 2026-08-26, dellsrv cluster exit, DIRECT (no proxy), curl_cffi chrome
# impersonation. EVERY path below returned 200 on the plain transport -- the
# browser was never needed. Plain urllib gets 403 on the same URLs, so the
# block is the TLS fingerprint, not the IP.
#
# This flatly contradicts the 403 map reported from a datacenter exit elsewhere
# (user_films, friends_on_film and film_rating_histogram were all said to be
# challenged). Cloudflare decides per-network: re-probe rather than trusting
# anyone's table, including this one.
TRANSPORT_HINTS = {
    "user_rss": "http",              # verified 2026-08-26
    "film": "http",                  # verified 2026-08-26
    "film_rating_histogram": "http", # verified 2026-08-26
    "film_reviews": "http",          # verified 2026-08-26
    "user_films": "http",            # verified 2026-08-26, paginated 72/page
    "user_following": "http",        # verified 2026-08-26
    "friends_on_film": "http",       # verified 2026-08-26
}
