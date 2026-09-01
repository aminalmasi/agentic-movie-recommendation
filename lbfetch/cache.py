"""Permanent on-disk cache for fetched pages.

The point of this cache is not speed, it is *politeness*. Every page we pull
from Letterboxd is a page we should never have to pull again: released films do
not change their credits, and a diary entry from 2019 is not going to move. A
run that re-fetches what it already has is indistinguishable, from the far end,
from a scraper that does not care.

So: 200s are cached forever. Failures are cached briefly, only so that a crashed
run does not immediately hammer the same dead URL on restart, and so that a
403 map stays stable within a session without freezing in place for good.

Layout is content-addressed by URL so a cache directory can be rsynced, shared
between the scraper and the notebooks, and inspected with zcat.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Failures expire; successes do not. 6h is long enough to survive a run and
# short enough that a Cloudflare block lifting overnight is picked up next day.
FAILURE_TTL_S = 6 * 3600


class Entry:
    """One cached HTTP response."""

    __slots__ = ("url", "status", "body", "transport", "fetched_at", "from_cache",
                 "retry_after")

    def __init__(self, url, status, body, transport, fetched_at, from_cache=False,
                 retry_after=None):
        self.retry_after = retry_after   # seconds, from a 429/503 Retry-After header
        self.url = url
        self.status = status
        self.body = body
        self.transport = transport
        self.fetched_at = fetched_at
        self.from_cache = from_cache

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def age_s(self) -> float:
        return time.time() - self.fetched_at

    def __repr__(self) -> str:
        src = "cache" if self.from_cache else self.transport
        return f"<Entry {self.status} {len(self.body or '')}B via {src} {self.url}>"


class Cache:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        # Host in the path so `du -sh cache/*` answers "what have I taken, and
        # from whom" without a script.
        host = urlsplit(url).netloc or "_"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.root / host / digest[:2] / f"{digest}.json.gz"

    def get(self, url: str) -> Optional[Entry]:
        path = self._path(url)
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError, EOFError):
            # A truncated file means a run died mid-write. Drop it and refetch.
            log.warning("corrupt cache entry, ignoring: %s", path)
            return None

        entry = Entry(
            url=raw["url"],
            status=raw["status"],
            body=raw["body"],
            transport=raw["transport"],
            fetched_at=raw["fetched_at"],
            from_cache=True,
        )
        if not entry.ok and entry.age_s > FAILURE_TTL_S:
            return None
        return entry

    def put(self, entry: Entry) -> None:
        path = self._path(entry.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "url": entry.url,
            "status": entry.status,
            "body": entry.body,
            "transport": entry.transport,
            "fetched_at": entry.fetched_at,
        }
        # Write-then-rename so a kill -9 never leaves a half-written entry that
        # the next run has to detect as corrupt.
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)

    def stats(self) -> dict:
        n = ok = bytes_ = 0
        for path in self.root.rglob("*.json.gz"):
            n += 1
            bytes_ += path.stat().st_size
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    if 200 <= json.load(fh)["status"] < 300:
                        ok += 1
            except Exception:
                pass
        return {"entries": n, "ok": ok, "failed": n - ok, "disk_mb": round(bytes_ / 1e6, 1)}
