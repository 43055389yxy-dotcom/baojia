from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.core.data_paths import AZURE_DATA_ROOT

AZURE_RETAIL_ENDPOINT = "https://prices.azure.com/api/retail/prices"
AZURE_BULK_SCHEMA_VERSION = 3
AZURE_BULK_REFRESH_SECONDS = 24 * 60 * 60


def _text(value: object) -> str:
    return str(value or "").strip()


def _row_key(row: dict[str, Any]) -> str:
    """Identify the complete official record, including nested price plans."""

    return hashlib.sha256(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _legacy_row_key(row: dict[str, Any]) -> str:
    """Read the first snapshot's narrow key while repairing its collisions."""

    identity = [
        row.get("meterId"),
        row.get("skuId"),
        row.get("armRegionName"),
        row.get("type"),
        row.get("reservationTerm"),
        row.get("tierMinimumUnits"),
        row.get("effectiveStartDate"),
        row.get("effectiveEndDate"),
        row.get("unitPrice"),
    ]
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class AzureBulkRetailCache:
    """Atomic local snapshots of the complete public Azure retail catalog."""

    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path or AZURE_DATA_ROOT / "azure_bulk_retail_catalog.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sync_lock = asyncio.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS azure_bulk_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    next_url TEXT,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    service_count INTEGER NOT NULL DEFAULT 0,
                    product_count INTEGER NOT NULL DEFAULT 0,
                    sku_count INTEGER NOT NULL DEFAULT 0,
                    meter_count INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    error TEXT,
                    schema_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS azure_bulk_retail_items (
                    snapshot_id TEXT NOT NULL,
                    row_key TEXT NOT NULL,
                    currency_code TEXT,
                    tier_minimum_units REAL,
                    retail_price REAL,
                    unit_price REAL,
                    arm_region_name TEXT,
                    location TEXT,
                    effective_start_date TEXT,
                    effective_end_date TEXT,
                    meter_id TEXT,
                    meter_name TEXT,
                    product_id TEXT,
                    sku_id TEXT,
                    product_name TEXT,
                    sku_name TEXT,
                    service_name TEXT,
                    service_id TEXT,
                    service_family TEXT,
                    unit_of_measure TEXT,
                    price_type TEXT,
                    arm_sku_name TEXT,
                    reservation_term TEXT,
                    is_primary_meter_region INTEGER,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (snapshot_id, row_key)
                );
                CREATE INDEX IF NOT EXISTS idx_azure_bulk_service_region
                    ON azure_bulk_retail_items(
                        snapshot_id, service_name, arm_region_name, arm_sku_name
                    );
                CREATE INDEX IF NOT EXISTS idx_azure_bulk_service_id
                    ON azure_bulk_retail_items(snapshot_id, service_id);
                CREATE INDEX IF NOT EXISTS idx_azure_bulk_product
                    ON azure_bulk_retail_items(snapshot_id, product_id);
                CREATE INDEX IF NOT EXISTS idx_azure_bulk_sku
                    ON azure_bulk_retail_items(snapshot_id, sku_id);
                CREATE TABLE IF NOT EXISTS azure_bulk_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(azure_bulk_retail_items)"
                ).fetchall()
            }
            if "raw_json" not in columns:
                connection.execute(
                    "ALTER TABLE azure_bulk_retail_items "
                    "ADD COLUMN raw_json TEXT NOT NULL DEFAULT '{}'"
                )

    def _current_snapshot_id(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM azure_bulk_metadata WHERE key = 'current_snapshot'"
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def is_ready(self) -> bool:
        return self._current_snapshot_id() is not None

    def needs_refresh(self) -> bool:
        snapshot_id = self._current_snapshot_id()
        if snapshot_id is None:
            return True
        with self._connect() as connection:
            row = connection.execute(
                "SELECT completed_at, schema_version FROM azure_bulk_snapshots "
                "WHERE snapshot_id = ? AND status = 'complete'",
                (snapshot_id,),
            ).fetchone()
        if row is None or row["completed_at"] is None:
            return True
        if int(row["schema_version"] or 0) != AZURE_BULK_SCHEMA_VERSION:
            return True
        return time.time() - float(row["completed_at"]) >= AZURE_BULK_REFRESH_SECONDS

    def _staging_snapshot(self) -> sqlite3.Row | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM azure_bulk_snapshots WHERE status IN ('downloading', 'failed') "
                "AND schema_version = ? ORDER BY started_at DESC LIMIT 1",
                (AZURE_BULK_SCHEMA_VERSION,),
            ).fetchone()
            if row is None:
                return None
            if time.time() - float(row["started_at"]) <= 24 * 60 * 60:
                return row
            # A very old partial download may span multiple official catalog
            # generations. Do not publish a mixed snapshot.
            connection.execute(
                "UPDATE azure_bulk_snapshots SET status = 'abandoned', "
                "error = 'staging snapshot expired before completion' "
                "WHERE snapshot_id = ?",
                (str(row["snapshot_id"]),),
            )
            return None

    def _new_snapshot(self) -> tuple[str, str, int, int]:
        snapshot_id = f"azure-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO azure_bulk_snapshots ("
                "snapshot_id, status, next_url, started_at, schema_version"
                ") VALUES (?, 'downloading', ?, ?, ?)",
                (
                    snapshot_id,
                    AZURE_RETAIL_ENDPOINT,
                    time.time(),
                    AZURE_BULK_SCHEMA_VERSION,
                ),
            )
        return snapshot_id, AZURE_RETAIL_ENDPOINT, 0, 0

    @staticmethod
    def _values(snapshot_id: str, row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            snapshot_id,
            _row_key(row),
            _text(row.get("currencyCode")),
            float(row.get("tierMinimumUnits") or 0),
            float(row.get("retailPrice") or 0),
            float(row.get("unitPrice") or 0),
            _text(row.get("armRegionName")),
            _text(row.get("location")),
            _text(row.get("effectiveStartDate")),
            _text(row.get("effectiveEndDate")),
            _text(row.get("meterId")),
            _text(row.get("meterName")),
            _text(row.get("productId")),
            _text(row.get("skuId")),
            _text(row.get("productName")),
            _text(row.get("skuName")),
            _text(row.get("serviceName")),
            _text(row.get("serviceId")),
            _text(row.get("serviceFamily")),
            _text(row.get("unitOfMeasure")),
            _text(row.get("type")),
            _text(row.get("armSkuName")),
            _text(row.get("reservationTerm")),
            int(bool(row.get("isPrimaryMeterRegion"))),
            json.dumps(row, ensure_ascii=False, separators=(",", ":")),
        )

    def _save_page(
        self,
        snapshot_id: str,
        items: list[dict[str, Any]],
        next_url: str | None,
        page_count: int,
        row_count: int,
    ) -> None:
        placeholders = ",".join("?" for _ in range(25))
        with self._lock, self._connect() as connection:
            connection.executemany(
                f"INSERT OR REPLACE INTO azure_bulk_retail_items VALUES ({placeholders})",
                [self._values(snapshot_id, item) for item in items],
            )
            connection.execute(
                "UPDATE azure_bulk_snapshots SET status = 'downloading', "
                "next_url = ?, page_count = ?, row_count = ?, error = NULL "
                "WHERE snapshot_id = ?",
                (next_url, page_count, row_count, snapshot_id),
            )

    def _complete(self, snapshot_id: str, page_count: int, row_count: int) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            counts = connection.execute(
                "SELECT COUNT(DISTINCT service_id) AS services, "
                "COUNT(DISTINCT product_id) AS products, "
                "COUNT(DISTINCT sku_id) AS skus, "
                "COUNT(DISTINCT meter_id) AS meters "
                "FROM azure_bulk_retail_items WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            completed_at = time.time()
            connection.execute(
                "UPDATE azure_bulk_snapshots SET status = 'complete', next_url = NULL, "
                "page_count = ?, row_count = ?, service_count = ?, product_count = ?, "
                "sku_count = ?, meter_count = ?, completed_at = ?, error = NULL "
                "WHERE snapshot_id = ?",
                (
                    page_count,
                    row_count,
                    int(counts["services"] or 0),
                    int(counts["products"] or 0),
                    int(counts["skus"] or 0),
                    int(counts["meters"] or 0),
                    completed_at,
                    snapshot_id,
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO azure_bulk_metadata(key, value) "
                "VALUES ('current_snapshot', ?)",
                (snapshot_id,),
            )
            old_snapshots = [
                str(row["snapshot_id"])
                for row in connection.execute(
                    "SELECT snapshot_id FROM azure_bulk_snapshots "
                    "WHERE snapshot_id != ? AND status = 'complete' "
                    "ORDER BY completed_at DESC",
                    (snapshot_id,),
                ).fetchall()[1:]
            ]
            for old_snapshot in old_snapshots:
                connection.execute(
                    "DELETE FROM azure_bulk_retail_items WHERE snapshot_id = ?",
                    (old_snapshot,),
                )
                connection.execute(
                    "DELETE FROM azure_bulk_snapshots WHERE snapshot_id = ?",
                    (old_snapshot,),
                )
            obsolete_snapshots = [
                str(row["snapshot_id"])
                for row in connection.execute(
                    "SELECT snapshot_id FROM azure_bulk_snapshots "
                    "WHERE snapshot_id != ? AND schema_version != ?",
                    (snapshot_id, AZURE_BULK_SCHEMA_VERSION),
                ).fetchall()
            ]
            for obsolete_snapshot in obsolete_snapshots:
                connection.execute(
                    "DELETE FROM azure_bulk_retail_items WHERE snapshot_id = ?",
                    (obsolete_snapshot,),
                )
                connection.execute(
                    "DELETE FROM azure_bulk_snapshots WHERE snapshot_id = ?",
                    (obsolete_snapshot,),
                )
        return self.status()

    def _mark_failed(self, snapshot_id: str, exc: Exception) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE azure_bulk_snapshots SET status = 'failed', error = ? "
                "WHERE snapshot_id = ?",
                (f"{type(exc).__name__}: {exc}"[:1000], snapshot_id),
            )

    async def sync(self, *, force: bool = False) -> dict[str, Any]:
        """Download once, resume failed pages, then atomically publish the snapshot."""

        async with self._sync_lock:
            if not force and not self.needs_refresh():
                return self.status()
            staging = await asyncio.to_thread(self._staging_snapshot)
            if staging is None:
                snapshot_id, url, page_count, row_count = await asyncio.to_thread(
                    self._new_snapshot
                )
            else:
                snapshot_id = str(staging["snapshot_id"])
                url = str(staging["next_url"] or AZURE_RETAIL_ENDPOINT)
                page_count = int(staging["page_count"] or 0)
                row_count = int(staging["row_count"] or 0)
            params: dict[str, str] | None = (
                {
                    "api-version": "2023-01-01-preview",
                    "currencyCode": "USD",
                }
                if url == AZURE_RETAIL_ENDPOINT
                else None
            )
            try:
                async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                    while url:
                        response = await client.get(url, params=params)
                        response.raise_for_status()
                        payload = response.json()
                        raw_items = payload.get("Items")
                        items = [dict(item) for item in raw_items or [] if isinstance(item, dict)]
                        next_url = payload.get("NextPageLink") or payload.get("nextPageLink")
                        page_count += 1
                        row_count += len(items)
                        await asyncio.to_thread(
                            self._save_page,
                            snapshot_id,
                            items,
                            str(next_url) if next_url else None,
                            page_count,
                            row_count,
                        )
                        url = str(next_url) if next_url else ""
                        params = None
                        if page_count > 2000:
                            raise RuntimeError("Azure retail catalog exceeded 2,000 pages")
            except Exception as exc:
                await asyncio.to_thread(self._mark_failed, snapshot_id, exc)
                raise
            return await asyncio.to_thread(
                self._complete,
                snapshot_id,
                page_count,
                row_count,
            )

    @staticmethod
    def _row_to_api(row: sqlite3.Row) -> dict[str, Any]:
        try:
            raw = json.loads(str(row["raw_json"] or "{}"))
        except (json.JSONDecodeError, TypeError):
            raw = {}
        if isinstance(raw, dict) and raw:
            return raw
        return {
            "currencyCode": row["currency_code"],
            "tierMinimumUnits": row["tier_minimum_units"],
            "retailPrice": row["retail_price"],
            "unitPrice": row["unit_price"],
            "armRegionName": row["arm_region_name"],
            "location": row["location"],
            "effectiveStartDate": row["effective_start_date"],
            "effectiveEndDate": row["effective_end_date"],
            "meterId": row["meter_id"],
            "meterName": row["meter_name"],
            "productId": row["product_id"],
            "skuId": row["sku_id"],
            "productName": row["product_name"],
            "skuName": row["sku_name"],
            "serviceName": row["service_name"],
            "serviceId": row["service_id"],
            "serviceFamily": row["service_family"],
            "unitOfMeasure": row["unit_of_measure"],
            "type": row["price_type"],
            "armSkuName": row["arm_sku_name"],
            "reservationTerm": row["reservation_term"],
            "isPrimaryMeterRegion": bool(row["is_primary_meter_region"]),
        }

    def retail_items(
        self,
        *,
        service_name: str,
        region: str | None,
        arm_sku_name: str | None = None,
    ) -> list[dict[str, Any]] | None:
        snapshot_id = self._current_snapshot_id()
        if snapshot_id is None:
            return None
        clauses = ["snapshot_id = ?", "service_name = ?"]
        values: list[Any] = [snapshot_id, service_name]
        if region and region != "global":
            clauses.append("arm_region_name = ?")
            values.append(region)
        if arm_sku_name:
            clauses.append("arm_sku_name = ?")
            values.append(arm_sku_name)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM azure_bulk_retail_items WHERE " + " AND ".join(clauses),
                tuple(values),
            ).fetchall()
        return [self._row_to_api(row) for row in rows]

    def service_regions(self, service_name: str) -> list[str] | None:
        snapshot_id = self._current_snapshot_id()
        if snapshot_id is None:
            return None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT arm_region_name FROM azure_bulk_retail_items "
                "WHERE snapshot_id = ? AND service_name = ? "
                "AND LOWER(arm_region_name) NOT IN ('', 'global') "
                "ORDER BY arm_region_name",
                (snapshot_id, service_name),
            ).fetchall()
        return [str(row["arm_region_name"]) for row in rows]

    def service_region_options(self, service_name: str) -> list[dict[str, str]] | None:
        """Return distinct code/label pairs without materializing retail rows."""

        snapshot_id = self._current_snapshot_id()
        if snapshot_id is None:
            return None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT arm_region_name, MIN(location) AS location "
                "FROM azure_bulk_retail_items "
                "WHERE snapshot_id = ? AND service_name = ? "
                "AND LOWER(arm_region_name) NOT IN ('', 'global') "
                "GROUP BY arm_region_name ORDER BY arm_region_name",
                (snapshot_id, service_name),
            ).fetchall()
        return [
            {
                "code": str(row["arm_region_name"]),
                "label": str(row["location"] or row["arm_region_name"]),
            }
            for row in rows
        ]

    def services(self) -> list[dict[str, str]]:
        snapshot_id = self._current_snapshot_id()
        if snapshot_id is None:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT service_id, service_name, MIN(service_family) AS service_family "
                "FROM azure_bulk_retail_items WHERE snapshot_id = ? "
                "GROUP BY service_id, service_name ORDER BY service_name",
                (snapshot_id,),
            ).fetchall()
        return [
            {
                "service_id": str(row["service_id"]),
                "service_name": str(row["service_name"]),
                "service_family": str(row["service_family"] or ""),
            }
            for row in rows
        ]

    def status(self) -> dict[str, Any]:
        current = self._current_snapshot_id()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM azure_bulk_snapshots ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            active = (
                connection.execute(
                    "SELECT * FROM azure_bulk_snapshots WHERE snapshot_id = ?",
                    (current,),
                ).fetchone()
                if current
                else None
            )
        if row is None:
            return {
                "state": "empty",
                "current_snapshot": None,
                "database": self.database_path.name,
            }
        return {
            "state": str(row["status"]),
            "current_snapshot": current,
            "downloading_snapshot": (
                str(row["snapshot_id"]) if str(row["status"]) in {"downloading", "failed"} else None
            ),
            "pages": int(row["page_count"] or 0),
            "rows": int(row["row_count"] or 0),
            "services": int(row["service_count"] or 0),
            "products": int(row["product_count"] or 0),
            "skus": int(row["sku_count"] or 0),
            "meters": int(row["meter_count"] or 0),
            "active": (
                {
                    "snapshot": str(active["snapshot_id"]),
                    "pages": int(active["page_count"] or 0),
                    "rows": int(active["row_count"] or 0),
                    "services": int(active["service_count"] or 0),
                    "products": int(active["product_count"] or 0),
                    "skus": int(active["sku_count"] or 0),
                    "meters": int(active["meter_count"] or 0),
                    "completed_at": active["completed_at"],
                }
                if active is not None
                else None
            ),
            "completed_at": row["completed_at"],
            "error": row["error"],
            "database": self.database_path.name,
        }
