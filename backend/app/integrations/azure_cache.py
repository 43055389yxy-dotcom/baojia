from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.core.data_paths import AZURE_DATA_ROOT


class PersistentAzureCache:
    """Provider-isolated persistent cache for Microsoft read-only catalogs."""

    def __init__(self, database_path: Path | None = None):
        self._path = database_path or AZURE_DATA_ROOT / "azure_catalog.sqlite3"
        self._lock = threading.RLock()

    @staticmethod
    def key(namespace: str, value: object) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return f"{namespace}:{hashlib.sha256(encoded.encode()).hexdigest()}"

    def get(self, key: str) -> Any | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload, expires_at FROM azure_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if float(row[1]) <= time.time():
                connection.execute("DELETE FROM azure_cache WHERE cache_key = ?", (key,))
                return None
            return json.loads(str(row[0]))

    def set(self, key: str, value: object, *, ttl_seconds: int) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO azure_cache(cache_key, payload, expires_at) "
                "VALUES (?, ?, ?)",
                (key, payload, time.time() + ttl_seconds),
            )
            connection.execute(
                "DELETE FROM azure_cache WHERE cache_key IN ("
                "SELECT cache_key FROM azure_cache ORDER BY expires_at DESC "
                "LIMIT -1 OFFSET 5000)"
            )

    def status(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM azure_cache").fetchone()[0])
            fresh = int(
                connection.execute(
                    "SELECT COUNT(*) FROM azure_cache WHERE expires_at > ?", (time.time(),)
                ).fetchone()[0]
            )
        return {"total": total, "fresh": fresh, "expired": total - fresh}

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS azure_cache ("
            "cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
        return connection
