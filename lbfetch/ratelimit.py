"""Rate budget shared across processes, and across nodes.

The in-process `RateLimiter` in fetch.py is correct for threads and silently
wrong for processes: eight workers each keep their own `{host: last_time}` dict,
each believes it is spacing requests by 3s, and the far end sees one request
every 0.4s. The rate budget belongs to the *IP*, not to the process, so the
state has to live somewhere all the workers can see.

WHERE THE STATE FILE GOES. On one node, /tmp is fine and fast. Across nodes it
must be on shared storage, because every node on this cluster leaves through the
same university NAT and therefore shares one budget with the far end. That is
why the default is the cache directory rather than /tmp -- being slow to take a
lock we hold for microseconds costs nothing, and being wrong costs an IP ban.

NFS LOCKING. `fcntl.flock` is unreliable over NFS; `fcntl.lockf` (POSIX record
locks) goes through lockd and works. We use lockf.

THE RULE THAT MATTERS: hold the lock only long enough to claim a slot. Never
across the network call, never across the sleep. A single-writer lock held
across slow work serialises every worker behind it and then presents as a hang
rather than as contention -- the same failure that produced the job-monitor
death spiral.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import time
from pathlib import Path

log = logging.getLogger(__name__)


class SharedRateLimiter:
    """Cross-process, cross-node token bucket. One state file per host."""

    def __init__(self, state_dir, min_interval_s: float = 3.0,
                 jitter_s: float = 1.5, max_penalty_s: float = 900.0):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval_s = min_interval_s
        self.jitter_s = jitter_s
        self.max_penalty_s = max_penalty_s

    def _path(self, host: str) -> Path:
        safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in host)
        return self.state_dir / f"rl-{safe}.json"

    def _with_lock(self, host: str, mutate):
        """Run `mutate(state) -> (state, result)` under an exclusive lock."""
        path = self._path(host)
        # r+ if it exists, else create. Never truncate on open: another worker
        # may be mid-read.
        fh = open(path, "r+" if path.exists() else "w+")
        try:
            fcntl.lockf(fh, fcntl.LOCK_EX)
            try:
                raw = fh.read()
                state = json.loads(raw) if raw.strip() else {}
            except ValueError:
                state = {}
            state, result = mutate(state)
            fh.seek(0)
            fh.truncate()
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
            return result
        finally:
            fcntl.lockf(fh, fcntl.LOCK_UN)
            fh.close()

    def wait(self, host: str) -> None:
        """Claim the next slot for `host`, then sleep until it arrives."""
        interval = self.min_interval_s + random.uniform(0, self.jitter_s)

        def claim(state):
            now = time.time()
            # A 429 seen by ANY worker holds back every worker.
            floor = max(now, state.get("next_at", 0.0), state.get("penalty_until", 0.0))
            state["next_at"] = floor + interval
            return state, floor

        slot = self._with_lock(host, claim)
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)          # outside the lock, always

    def penalise(self, host: str, seconds: float, reason: str = "429") -> None:
        """Back every worker off this host after a rate-limit response."""
        seconds = min(max(seconds, 1.0), self.max_penalty_s)

        def apply(state):
            until = max(state.get("penalty_until", 0.0), time.time() + seconds)
            state["penalty_until"] = until
            state["last_penalty_reason"] = reason
            state["penalty_count"] = state.get("penalty_count", 0) + 1
            return state, state["penalty_count"]

        n = self._with_lock(host, apply)
        log.warning("%s: %s -- backing off %.0fs (penalty #%d, all workers)",
                    host, reason, seconds, n)

    def status(self, host: str) -> dict:
        def peek(state):
            return state, dict(state)
        s = self._with_lock(host, peek)
        now = time.time()
        return {
            "next_in_s": round(max(0.0, s.get("next_at", 0) - now), 2),
            "penalty_in_s": round(max(0.0, s.get("penalty_until", 0) - now), 2),
            "penalty_count": s.get("penalty_count", 0),
            "last_penalty_reason": s.get("last_penalty_reason"),
        }
