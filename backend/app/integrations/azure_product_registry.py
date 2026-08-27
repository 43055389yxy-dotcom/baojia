from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

AZURE_PRODUCT_REGISTRY_SCHEMA_VERSION = 1


def _canonical(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _service_key(value: object) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    for prefix in ("microsoft_azure_", "microsoft_", "azure_"):
        if key.startswith(prefix):
            key = key[len(prefix) :]
            break
    return key or "azure_service"


def _aliases(service_key: str, display_name: str, service_name: str) -> list[str]:
    values = {
        service_key,
        service_key.replace("_", " "),
        display_name,
        service_name,
        re.sub(r"^(?:Microsoft\s+Azure|Microsoft|Azure)\s+", "", display_name, flags=re.I),
    }
    return sorted(value.strip() for value in values if value and value.strip())


class AzureProductRegistry:
    """Azure-only identity and field registry backed by its own SQLite file."""

    def __init__(self, database_path: Path | None = None) -> None:
        self._database_path = database_path or (
            Path(__file__).resolve().parents[2] / ".cache" / "azure_product_registry.sqlite3"
        )
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            columns = connection.execute("PRAGMA table_info(azure_product_registry)").fetchall()
            primary_keys = [str(column["name"]) for column in columns if int(column["pk"] or 0) > 0]
            if columns and primary_keys != ["service_key"]:
                connection.execute(
                    "ALTER TABLE azure_product_registry RENAME TO azure_product_registry_legacy"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS azure_product_registry (
                    service_key TEXT PRIMARY KEY,
                    service_name TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    regions_json TEXT NOT NULL,
                    field_template_json TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    identity_status TEXT NOT NULL,
                    profile_status TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_azure_product_service_name "
                "ON azure_product_registry(service_name)"
            )
            legacy_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'azure_product_registry_legacy'"
            ).fetchone()
            if legacy_exists is not None:
                legacy_rows = connection.execute(
                    "SELECT * FROM azure_product_registry_legacy"
                ).fetchall()
                for row in legacy_rows:
                    service_name = str(row["service_name"])
                    display_name = str(row["display_name"])
                    service_key = _service_key(display_name or service_name)
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO azure_product_registry (
                            service_key, service_name, display_name, aliases_json,
                            regions_json, field_template_json, policy_json,
                            identity_status, profile_status, schema_version, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            service_key,
                            service_name,
                            display_name,
                            str(row["aliases_json"]),
                            str(row["regions_json"]),
                            str(row["field_template_json"]),
                            json.dumps(self._policy(service_name), ensure_ascii=False),
                            str(row["identity_status"]),
                            str(row["profile_status"]),
                            AZURE_PRODUCT_REGISTRY_SCHEMA_VERSION,
                            float(row["updated_at"]),
                        ),
                    )
                connection.execute("DROP TABLE azure_product_registry_legacy")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_azure_product_service_name "
                    "ON azure_product_registry(service_name)"
                )

    @staticmethod
    def _policy(service_name: str) -> dict[str, Any]:
        return {
            "provider": "azure",
            "service_name": service_name,
            "identity_source": "microsoft_azure_retail_prices",
            "specification_source": "microsoft_azure_retail_prices",
            "final_price_source": "microsoft_azure_retail_prices",
            "customer_explicit_value_priority": "highest",
            "cross_component_inheritance": "region_only",
            "missing_value": "service_specific_default_or_confirmation",
            "price_failure": "retain_component_and_retry_azure_official_catalog",
            "zero_price": "allowed_only_for_explicit_zero_base_resources",
            "edit_recalculation": "affected_component_only_from_intake",
            "aws_data_access": "forbidden",
        }

    def bootstrap(
        self,
        service_names: dict[str, str],
        display_names: dict[str, str],
    ) -> dict[str, int]:
        inserted = 0
        updated = 0
        for service_key, service_name in service_names.items():
            existed = self.resolve_product(service_key, service_name) is not None
            self.register_identity(
                service_key=service_key,
                display_name=display_names.get(service_key, service_name),
                service_name=service_name,
                identity_status="curated_official",
            )
            updated += int(existed)
            inserted += int(not existed)
        return {"inserted": inserted, "updated": updated}

    def sync_official_services(
        self,
        services: list[dict[str, str]],
    ) -> dict[str, int]:
        """Register every service identity found in the complete Azure snapshot."""

        inserted = 0
        updated = 0
        for service in services:
            service_name = str(service.get("service_name") or "").strip()
            service_id = str(service.get("service_id") or "").strip()
            if not service_name:
                continue
            matches = [
                item
                for item in self.list_products()
                if _canonical(item["service_name"]) == _canonical(service_name)
            ]
            if len(matches) == 1:
                service_key = str(matches[0]["service_key"])
                existed = True
            else:
                base_key = _service_key(service_name)
                service_key = (
                    f"azure_catalog_{base_key}"
                    if len(matches) > 1
                    else base_key
                )
                existed = self.resolve_product(service_key) is not None
            self.register_identity(
                service_key=service_key,
                display_name=service_name,
                service_name=service_name,
                identity_status="official_bulk_index",
            )
            if service_id:
                self._add_alias(service_key, service_id)
            inserted += int(not existed)
            updated += int(existed)
        return {"inserted": inserted, "updated": updated, "total": len(services)}

    def _add_alias(self, service_key: str, alias: str) -> None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT aliases_json FROM azure_product_registry WHERE service_key = ?",
                (service_key,),
            ).fetchone()
            if row is None:
                return
            aliases = {
                str(value)
                for value in json.loads(str(row["aliases_json"]))
                if str(value).strip()
            }
            aliases.add(alias)
            connection.execute(
                "UPDATE azure_product_registry SET aliases_json = ?, updated_at = ? "
                "WHERE service_key = ?",
                (
                    json.dumps(sorted(aliases), ensure_ascii=False),
                    time.time(),
                    service_key,
                ),
            )

    def register_identity(
        self,
        *,
        service_key: str,
        display_name: str,
        service_name: str,
        regions: list[str] | None = None,
        identity_status: str = "official",
    ) -> None:
        now = time.time()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM azure_product_registry WHERE service_key = ?",
                (service_key,),
            ).fetchone()
            previous_regions: list[str] = []
            previous_template: dict[str, Any] = {}
            previous_status = "identity_ready"
            if existing is not None:
                previous_regions = json.loads(str(existing["regions_json"]))
                previous_template = json.loads(str(existing["field_template_json"]))
                previous_status = str(existing["profile_status"])
            effective_regions = sorted(set(regions or previous_regions))
            template = previous_template or {
                "service_name": service_name,
                "fields": [],
                "source": "official_dimensions_on_first_use",
                "isolation": "strict_component_boundary",
            }
            connection.execute(
                """
                INSERT OR REPLACE INTO azure_product_registry (
                    service_key, service_name, display_name, aliases_json,
                    regions_json, field_template_json, policy_json,
                    identity_status, profile_status, schema_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    service_key,
                    service_name,
                    display_name,
                    json.dumps(
                        _aliases(service_key, display_name, service_name),
                        ensure_ascii=False,
                    ),
                    json.dumps(effective_regions, ensure_ascii=False),
                    json.dumps(template, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(self._policy(service_name), ensure_ascii=False),
                    identity_status,
                    previous_status,
                    AZURE_PRODUCT_REGISTRY_SCHEMA_VERSION,
                    now,
                ),
            )

    def update_profile(self, profile: dict[str, Any]) -> None:
        service_name = str(profile.get("service_name") or "").strip()
        service_key = str(profile.get("service_key") or "").strip()
        display_name = str(profile.get("display_name") or service_name).strip()
        if not service_name or not service_key:
            return
        region = str(profile.get("region") or "").strip()
        self.register_identity(
            service_key=service_key,
            display_name=display_name,
            service_name=service_name,
            regions=[region] if region and region != "global" else [],
        )
        fields = [str(field) for field in profile.get("fields", []) if str(field).strip()]
        template = {
            "service_name": service_name,
            "fields": sorted(set(fields)),
            "official_fields": list(profile.get("official_fields") or []),
            "arm_sku_names": list(profile.get("arm_sku_names") or []),
            "meter_names": list(profile.get("meter_names") or []),
            "units": list(profile.get("units") or []),
            "source": "microsoft_azure_retail_prices",
            "region": region,
            "isolation": "strict_component_boundary",
            "profile_schema_version": profile.get("profile_schema_version"),
        }
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT regions_json FROM azure_product_registry WHERE service_key = ?",
                (service_key,),
            ).fetchone()
            regions = json.loads(str(row["regions_json"])) if row is not None else []
            if region and region != "global":
                regions = sorted({*regions, region})
            connection.execute(
                "UPDATE azure_product_registry SET regions_json = ?, "
                "field_template_json = ?, profile_status = 'profile_ready', "
                "updated_at = ? WHERE service_key = ?",
                (
                    json.dumps(regions, ensure_ascii=False),
                    json.dumps(template, ensure_ascii=False, separators=(",", ":")),
                    time.time(),
                    service_key,
                ),
            )

    def list_products(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM azure_product_registry ORDER BY service_key"
            ).fetchall()
        return [
            {
                "service_name": str(row["service_name"]),
                "service_key": str(row["service_key"]),
                "display_name": str(row["display_name"]),
                "aliases": json.loads(str(row["aliases_json"])),
                "regions": json.loads(str(row["regions_json"])),
                "field_template": json.loads(str(row["field_template_json"])),
                "policy": json.loads(str(row["policy_json"])),
                "identity_status": str(row["identity_status"]),
                "profile_status": str(row["profile_status"]),
                "schema_version": int(row["schema_version"]),
                "updated_at": float(row["updated_at"]),
            }
            for row in rows
        ]

    def resolve_product(self, *labels: str) -> dict[str, Any] | None:
        targets = {_canonical(label) for label in labels if _canonical(label)}
        matches = []
        for product in self.list_products():
            identities = {
                _canonical(product["service_name"]),
                _canonical(product["service_key"]),
                _canonical(product["display_name"]),
                *(_canonical(alias) for alias in product["aliases"]),
            }
            if identities & targets:
                matches.append(product)
        return matches[0] if len(matches) == 1 else None

    def coverage(self) -> dict[str, Any]:
        products = self.list_products()
        profiles = [item for item in products if item["profile_status"] == "profile_ready"]
        regions = [item for item in products if item["regions"]]
        isolated = [
            item
            for item in products
            if item["field_template"].get("isolation") == "strict_component_boundary"
            and item["policy"].get("cross_component_inheritance") == "region_only"
            and item["policy"].get("aws_data_access") == "forbidden"
        ]
        return {
            "registered_products": len(products),
            "materialized_profiles": len(profiles),
            "regions_cached": len(regions),
            "strictly_isolated": len(isolated),
            "provider": "azure",
            "database": self._database_path.name,
            "future_product_mode": "first_use_official_profile_then_persist",
        }
