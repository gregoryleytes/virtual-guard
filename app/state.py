"""
SessionStore — lightweight in-memory session store.

For production, swap _store for Redis:
    import redis
    r = redis.Redis(...)
    r.setex(key, TTL, json.dumps(data))
"""

import threading
import time
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("virtual_guard.state")

SESSION_TTL = 3600  # 1 hour


class SessionStore:
    def __init__(self, ttl: int = SESSION_TTL):
        self._store: Dict[str, Dict] = {}
        self._ttl    = ttl
        self._lock   = threading.Lock()

    def init(self, key: str, data: Dict) -> None:
        with self._lock:
            self._store[key] = {**data, "_expires": time.time() + self._ttl}

    def get(self, key: str) -> Optional[Dict]:
        with self._lock:
            record = self._store.get(key)
            if record is None:
                return None
            if time.time() > record.get("_expires", 0):
                del self._store[key]
                return None
            return {k: v for k, v in record.items() if k != "_expires"}

    def set(self, key: str, data: Dict) -> None:
        with self._lock:
            self._store[key] = {**data, "_expires": time.time() + self._ttl}

    def update(self, key: str, updates: Dict) -> None:
        with self._lock:
            record = self._store.get(key, {})
            record.update(updates)
            record["_expires"] = time.time() + self._ttl
            self._store[key] = record

    def clear(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def cleanup_expired(self) -> int:
        """Remove expired sessions. Call periodically."""
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._store.items() if now > v.get("_expires", 0)]
            for k in expired:
                del self._store[k]
        if expired:
            logger.debug("Cleaned up %d expired sessions", len(expired))
        return len(expired)
