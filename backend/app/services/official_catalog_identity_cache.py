from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.core.data_paths import AWS_DATA_ROOT


class OfficialCatalogIdentityCache:
    """Persist stable public-catalog identities without prices or search text."""

    def __init__(self, directory: Path | None = None) -> None:
        root = directory or AWS_DATA_ROOT
        root.mkdir(parents=True, exist_ok=True)
        self.target = root / "official_catalog_identities.sqlite3"
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS catalog_identities ("
                "cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
        self.target.chmod(0o600)

    @staticmethod
    def key(provider: str, selector: object) -> str:
        encoded = json.dumps(
            {"provider": provider, "selector": selector},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"catalog:{provider}:{digest}"

    def get(self, cache_key: str) -> list[dict[str, Any]] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM catalog_identities WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        return payload if isinstance(payload, list) else None

    def set(self, cache_key: str, identities: list[dict[str, Any]]) -> None:
        payload = json.dumps(identities, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO catalog_identities(cache_key, payload, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (cache_key, payload),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.target, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection
