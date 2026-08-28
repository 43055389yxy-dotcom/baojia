from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any

from app.core.data_paths import AWS_DATA_ROOT

OFFICIAL_CATALOG_TTL_SECONDS = 10 * 24 * 60 * 60


class PersistentAwsCache:
    """Persistent cache for read-only AWS catalog responses.

    BCM remains the final price source. This cache only avoids downloading the
    same product and specification catalogs for every pre-flight selection.
    """

    def __init__(self, ttl_seconds: int = OFFICIAL_CATALOG_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._path = AWS_DATA_ROOT / "aws_catalog.sqlite3"
        self._lock = threading.RLock()

    @staticmethod
    def key(namespace: str, value: object) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"{namespace}:{digest}"

    def get(self, key: str, *, allow_stale: bool = False) -> Any | None:
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT payload, expires_at FROM aws_cache WHERE cache_key = ?", (key,)
                ).fetchone()
                if row is None:
                    return None
                if float(row[1]) <= time.time() and not allow_stale:
                    return None
                return json.loads(row[0])
            finally:
                connection.close()

    def set(self, key: str, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    "INSERT OR REPLACE INTO aws_cache(cache_key, payload, expires_at) "
                    "VALUES (?, ?, ?)",
                    (key, payload, time.time() + self._ttl_seconds),
                )
                connection.commit()
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS aws_cache ("
            "cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
        return connection
