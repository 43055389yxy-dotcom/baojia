from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path

from app.domain.models import ServiceRequirement

COMPONENT_RESULT_TTL_SECONDS = 90 * 24 * 60 * 60
COMPONENT_RESULT_CACHE_VERSION = "component-template-v2-customer-rebuild"


class ValidatedComponentResultCache:
    """Persist only component templates that already passed local validation.

    The cache key includes the complete component input, source text, model and
    template version.  It therefore saves repeated correction work for the
    same request without carrying one customer's quantities into another one.
    """

    def __init__(self, database_path: Path | None = None) -> None:
        self._database_path = database_path or (
            Path(__file__).resolve().parents[2]
            / ".cache"
            / "validated_component_results.sqlite3"
        )
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS validated_component_results (
                    cache_key TEXT PRIMARY KEY,
                    service_key TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_component_results_updated "
                "ON validated_component_results(updated_at DESC)"
            )

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()

    @classmethod
    def _key(cls, component: ServiceRequirement, model_name: str) -> str:
        payload = {
            "version": COMPONENT_RESULT_CACHE_VERSION,
            "model": model_name,
            "service": component.service,
            "region": component.region,
            "quantity": component.quantity,
            "hours_per_month": component.hours_per_month,
            "requirements": component.requirements,
            "source_text": cls._normalized_text(component.source_text),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def get(
        self, component: ServiceRequirement, model_name: str
    ) -> ServiceRequirement | None:
        cache_key = self._key(component, model_name)
        cutoff = time.time() - COMPONENT_RESULT_TTL_SECONDS
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT result_json, updated_at FROM validated_component_results "
                "WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            if float(row["updated_at"]) < cutoff:
                connection.execute(
                    "DELETE FROM validated_component_results WHERE cache_key = ?",
                    (cache_key,),
                )
                return None
        try:
            return ServiceRequirement.model_validate_json(str(row["result_json"]))
        except Exception:
            with self._lock, self._connect() as connection:
                connection.execute(
                    "DELETE FROM validated_component_results WHERE cache_key = ?",
                    (cache_key,),
                )
            return None

    def put(
        self,
        component_input: ServiceRequirement,
        model_name: str,
        validated_result: ServiceRequirement,
    ) -> None:
        cache_key = self._key(component_input, model_name)
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO validated_component_results (
                    cache_key, service_key, source_text, model_name,
                    result_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    result_json=excluded.result_json,
                    updated_at=excluded.updated_at
                """,
                (
                    cache_key,
                    component_input.service,
                    component_input.source_text,
                    model_name,
                    validated_result.model_dump_json(),
                    now,
                ),
            )
            # Keep the persistent learning cache bounded. Old, uncommon exact
            # requests are cheap to relearn and must not grow the server forever.
            connection.execute(
                """
                DELETE FROM validated_component_results
                WHERE cache_key IN (
                    SELECT cache_key FROM validated_component_results
                    ORDER BY updated_at DESC LIMIT -1 OFFSET 5000
                )
                """
            )
