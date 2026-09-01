"""Cache-first fetcher for Letterboxd, with browser escalation.

Three transports, tried cheapest-first, because a headless Chrome costs ~300MB
of RSS and about a second per page and most of what we want does not need one:

  1. `curl_cffi` impersonating Chrome  -- real TLS fingerprint, ~50ms
  2. Playwright persistent context     -- for the Cloudflare interstitial paths
  3. nothing; we record the failure and move on

The escalation is automatic: a 403 whose body carries a Cloudflare challenge
marker is retried in the browser, and the transport that finally worked is
recorded in the cache entry so `probe_paths.py` can build an honest map of
which endpoints actually need the expensive path.

ON THE PERSISTENT CONTEXT. The single most important thing here is that the
browser uses a real on-disk profile (`--user-data-dir`). Cloudflare's clearance
cookie is what lets you past the interstitial, and it is issued once and then
honoured for hours. A fresh incognito context per request re-solves the
challenge every single time, which is both slow and the most obviously
bot-shaped thing you can do. One profile, reused, solves it roughly once a day.

ON THE PROXY. cf_clearance is bound to the IP that earned it *and* to the exact
User-Agent that earned it. A rotating residential proxy therefore invalidates
the cookie every time the exit changes. If you route through DataImpulse, use a
STICKY session (DataImpulse exposes this as a suffix on the username, e.g.
`user__cr.it;sid.<random>`) so one profile keeps one exit, or you will re-solve
the challenge on every request and be worse off than direct.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

from .cache import Cache, Entry
from .ratelimit import SharedRateLimiter

log = logging.getLogger(__name__)

# One User-Agent, everywhere, forever. It has to match the browser Playwright
# actually drives, or the clearance cookie earned by one transport is rejected
# when presented by the other.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

# Markers that mean "Cloudflare interstitial", not "genuinely forbidden".
# A real 403 from the origin has none of these and should NOT trigger a browser
# retry -- escalating on those just burns a browser launch to be told no twice.
#
# These are matched ONLY against raw bodies that already returned 403/503/429.
# Do not reuse them on a rendered DOM: Cloudflare injects its bot-management
# beacon from /cdn-cgi/challenge-platform/ into EVERY page it fronts, including
# perfectly successful ones, so a substring test on page.content() reports a
# challenge on pages that loaded fine. Measured 2026-08-26 -- it cost an hour.
CHALLENGE_MARKERS = ("Just a moment", "cf-chl", "cf_chl_opt", "challenge-platform")

# The rendered-DOM equivalent. The interstitial owns the document title and
# mounts one of these widgets; a cleared page has neither.
CHALLENGE_TITLES = ("just a moment", "attention required", "access denied",
                    "checking your browser")
CHALLENGE_SELECTOR = "#challenge-running, #cf-chl-widget, form#challenge-form, #challenge-error-title"

# The browser profile MUST live on local disk. Chromium keeps its profile in
# sqlite/leveldb, and those take file locks that NFS does not honour properly --
# measured on this cluster 2026-08-26: an identical persistent context times out
# after 30s with the profile on /extra (NFS4) and returns 200 in 1.4s with the
# profile on /tmp. This is not a preference, it is the difference between the
# browser transport working and hanging.
#
# Node-local is also the semantically correct choice: cf_clearance is bound to
# the IP that earned it, so a profile shared across cluster nodes would be
# carrying a cookie that is dead on any node but the one that solved it.
DEFAULT_PROFILE_DIR = os.environ.get(
    "LB_PROFILE_DIR", f"/tmp/lb-browser-profile-{os.environ.get('USER', 'x')}"
)

DEFAULT_MIN_INTERVAL_S = float(os.environ.get("LB_MIN_INTERVAL_S", "3.0"))
DEFAULT_JITTER_S = float(os.environ.get("LB_JITTER_S", "1.5"))


class RateLimiter:
    """Per-host minimum spacing with jitter.

    Jitter is not decoration. A request landing every 3.000s is a signature no
    human produces, and it is trivially clustered out of a log.
    """

    def __init__(self, min_interval_s: float, jitter_s: float):
        self.min_interval_s = min_interval_s
        self.jitter_s = jitter_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            gap = self.min_interval_s + random.uniform(0, self.jitter_s)
            earliest = self._last.get(host, 0.0) + gap
            sleep_s = max(0.0, earliest - now)
            self._last[host] = max(now, earliest)
        if sleep_s > 0:
            time.sleep(sleep_s)


class Fetcher:
    def __init__(
        self,
        cache_dir: str = "cache",
        profile_dir: str = DEFAULT_PROFILE_DIR,
        proxy_url: Optional[str] = None,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        jitter_s: float = DEFAULT_JITTER_S,
        allow_browser: bool = True,
        shared_budget: bool = True,
        max_retries: int = 3,
    ):
        self.cache = Cache(cache_dir)
        self.profile_dir = os.path.abspath(profile_dir)
        if self.profile_dir.startswith(("/extra", "/home")):
            log.warning(
                "browser profile is on %s, which is NFS here -- the persistent "
                "context will hang on page.goto. Set LB_PROFILE_DIR to a local "
                "path.", self.profile_dir)
        self.proxy_url = proxy_url or os.environ.get("LB_PROXY_URL") or os.environ.get(
            "JOBTOOLS_PROXY_URL"
        )
        # Shared by default. An in-process limiter is correct only if you are
        # certain this is the sole process touching the host -- and "certain"
        # stops being true the moment anyone runs two shells.
        if shared_budget:
            self.limiter = SharedRateLimiter(
                os.path.join(cache_dir, ".ratelimit"), min_interval_s, jitter_s)
        else:
            self.limiter = RateLimiter(min_interval_s, jitter_s)
        self.max_retries = max_retries
        self.allow_browser = allow_browser
        self._session = None
        self._pw = None
        self._ctx = None
        self._gateways = gateway_urls(self.proxy_url) if self.proxy_url else []
        self._gw = 0

    # ---------------------------------------------------------------- public

    def get(self, url: str, force_browser: bool = False, refresh: bool = False) -> Entry:
        """Fetch `url`, cache-first. Never raises on HTTP status."""
        if not refresh:
            hit = self.cache.get(url)
            if hit is not None:
                log.debug("cache hit %s", url)
                return hit

        host = urlsplit(url).netloc

        if not force_browser:
            entry = None
            for attempt in range(self.max_retries):
                self.limiter.wait(host)
                entry = self._get_http(url)

                # 429/503 is the far end asking us to slow down. Honour it
                # across EVERY worker, not just this one, or the other seven
                # keep hammering while this one politely waits.
                if entry.status in (429, 503) and not self._is_challenge(entry):
                    backoff = entry.retry_after or (2 ** attempt) * 30
                    if hasattr(self.limiter, "penalise"):
                        self.limiter.penalise(host, backoff,
                                              reason=f"HTTP {entry.status}")
                    else:
                        time.sleep(min(backoff, 300))
                    continue
                break

            if entry.ok or not self._is_challenge(entry):
                self.cache.put(entry)
                return entry
            log.info("challenge on %s, escalating to browser", url)

        if not self.allow_browser:
            entry = Entry(url, 403, "", "http-blocked", time.time())
            self.cache.put(entry)
            return entry

        self.limiter.wait(host)
        entry = self._get_browser(url)
        self.cache.put(entry)
        return entry

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --------------------------------------------------------------- private

    @staticmethod
    def _is_challenge(entry: Entry) -> bool:
        if entry.status not in (403, 503, 429):
            return False
        body = entry.body or ""
        return any(m in body for m in CHALLENGE_MARKERS)

    def _get_http(self, url: str) -> Entry:
        if self._session is None:
            try:
                from curl_cffi import requests as creq

                gw = self._gateways[self._gw] if self._gateways else None
                self._session = creq.Session(
                    impersonate="chrome", headers=HEADERS, timeout=45,
                    proxies={"http": gw, "https": gw} if gw else None,
                )
                log.info("http transport: curl_cffi (chrome impersonation)%s",
                         f" via gateway {self._gw+1}/{len(self._gateways)}"
                         if gw else " DIRECT")
            except ImportError:
                import requests

                self._session = requests.Session()
                self._session.headers.update(HEADERS)
                log.warning(
                    "curl_cffi not installed; falling back to requests. Expect "
                    "more Cloudflare challenges -- the TLS fingerprint will not "
                    "match the Chrome User-Agent we send."
                )
        try:
            kwargs = {"timeout": 45}
            if self.proxy_url and not hasattr(self._session, "impersonate"):
                kwargs["proxies"] = {"http": self.proxy_url, "https": self.proxy_url}
            resp = self._session.get(url, **kwargs)
            retry_after = None
            try:
                raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                if raw:
                    retry_after = float(raw)      # seconds form; HTTP-date form ignored
            except (TypeError, ValueError):
                retry_after = None
            return Entry(url, resp.status_code, resp.text, "curl_cffi", time.time(),
                         retry_after=retry_after)
        except Exception as exc:
            # A dead gateway looks like a connection error on every request.
            # Rotate to the next resolved address rather than failing the run.
            if self._gateways and len(self._gateways) > 1:
                self._gw = (self._gw + 1) % len(self._gateways)
                log.warning("gateway failed (%s), rotating to %d/%d",
                            type(exc).__name__, self._gw + 1, len(self._gateways))
                self._session = None
            else:
                log.warning("http fetch failed %s: %s: %s", url, type(exc).__name__, exc)
            return Entry(url, 0, "", "http-error", time.time())

    def _browser_ctx(self):
        """Lazily launch one persistent browser context and keep it."""
        if self._ctx is not None:
            return self._ctx

        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch = {
            "user_data_dir": self.profile_dir,
            "headless": os.environ.get("LB_HEADLESS", "1") != "0",
            "user_agent": UA,
            "locale": "en-GB",
            "viewport": {"width": 1440, "height": 900},
            "args": [
                "--disable-blink-features=AutomationControlled",
                # Required on cluster/container hosts: no user namespaces for the
                # sandbox, and /dev/shm is typically too small for Chromium.
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if self.proxy_url:
            launch["proxy"] = _playwright_proxy(self.proxy_url)

        # Real Chrome when it is installed (`playwright install chrome`); it is
        # markedly less challenge-prone than bundled Chromium, which ships a
        # handful of tells. Fall back silently when it is not there.
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **launch)
            log.info("browser: system Chrome, profile=%s", self.profile_dir)
        except Exception:
            self._ctx = self._pw.chromium.launch_persistent_context(**launch)
            log.info("browser: bundled Chromium, profile=%s", self.profile_dir)

        self._ctx.set_default_timeout(45_000)
        return self._ctx

    @staticmethod
    def _page_challenged(page) -> bool:
        """Is this rendered page still the interstitial?

        Title + widget, never a substring scan of the DOM -- see the note on
        CHALLENGE_MARKERS for why the obvious approach reports false positives.
        """
        try:
            title = (page.title() or "").strip().lower()
        except Exception:
            return True
        if any(t in title for t in CHALLENGE_TITLES):
            return True
        try:
            return page.query_selector(CHALLENGE_SELECTOR) is not None
        except Exception:
            return False

    def _get_browser(self, url: str, settle_s: float = 25.0) -> Entry:
        try:
            ctx = self._browser_ctx()
        except Exception as exc:
            log.error("cannot launch browser: %s: %s", type(exc).__name__, exc)
            return Entry(url, 0, "", "browser-unavailable", time.time())

        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")

            # Sit through the interstitial. Cloudflare reloads the page itself
            # once the challenge clears, so we poll for the widget to go away
            # rather than waiting on a navigation event that may not fire.
            deadline = time.time() + settle_s
            while True:
                if not self._page_challenged(page):
                    return Entry(url, 200, page.content(), "browser", time.time())
                if time.time() >= deadline:
                    break
                page.wait_for_timeout(1000)

            log.warning("challenge did not clear in %.0fs: %s", settle_s, url)
            return Entry(url, 403, page.content(), "browser-challenged", time.time())
        except Exception as exc:
            log.warning("browser fetch failed %s: %s: %s", url, type(exc).__name__, exc)
            return Entry(url, 0, "", "browser-error", time.time())
        finally:
            page.close()


# DataImpulse's gateway returns a varying A-record set and has multi-minute
# windows where SOME addresses refuse every connection while others work. The
# vinted client learned this the hard way; same fix here. The proxy hop is plain
# HTTP, so addressing it by IP mismatches no certificate.
KNOWN_GOOD_GATEWAYS = ("67.213.121.97", "67.213.121.105", "67.213.121.89")


def gateway_urls(proxy_url: str) -> list:
    """Expand a proxy URL into one candidate per gateway address, good ones first."""
    import socket
    parts = urlsplit(proxy_url)
    host, port = parts.hostname, parts.port
    if not host:
        return [proxy_url]
    try:
        resolved = sorted({ai[4][0]
                           for ai in socket.getaddrinfo(host, port, socket.AF_INET)})
    except OSError as exc:
        log.warning("cannot resolve %s: %s", host, exc)
        resolved = []
    ordered = list(KNOWN_GOOD_GATEWAYS) + [ip for ip in resolved
                                           if ip not in KNOWN_GOOD_GATEWAYS]
    auth = ""
    if parts.username:
        auth = f"{parts.username}:{parts.password or ''}@"
    return [f"{parts.scheme}://{auth}{ip}:{port}" for ip in ordered] or [proxy_url]


def _playwright_proxy(proxy_url: str) -> dict:
    """Split `scheme://user:pass@host:port` into Playwright's proxy dict."""
    parts = urlsplit(proxy_url)
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    proxy = {"server": server}
    if parts.username:
        proxy["username"] = parts.username
        proxy["password"] = parts.password or ""
    return proxy
