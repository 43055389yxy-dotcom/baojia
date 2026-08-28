from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.core.data_paths import AWS_DATA_ROOT
from app.core.errors import ManualConfirmationRequired
from app.integrations.aws import PricingCatalog
from app.integrations.aws_product_registry import AwsProductRegistry
from app.integrations.aws_supported_services import CURATED_SERVICE_OFFER_CODES
from app.integrations.service_templates import DYNAMIC_SEMANTIC_TEMPLATE_FIELDS

PROFILE_TTL_SECONDS = 10 * 24 * 60 * 60
FAILED_RETRY_SECONDS = 6 * 60 * 60
PROFILE_SCHEMA_VERSION = 12


def canonical_service_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def service_stem(value: str) -> str:
    result = canonical_service_name(value)
    for prefix in ("amazon", "aws"):
        if result.startswith(prefix):
            result = result[len(prefix) :]
    for suffix in ("service", "services"):
        if result.endswith(suffix):
            result = result[: -len(suffix)]
    return result


def service_core_stem(value: str) -> str:
    """Return the stable AWS product name without marketing qualifiers."""

    result = service_stem(value)
    # AWS service codes sometimes omit the word "Managed" from the public
    # product name (for example Amazon Managed Grafana -> AmazonGrafana).
    # Removing this single generic qualifier is deterministic and avoids a
    # growing, hard-coded alias list.
    return result.replace("managed", "")


def _dimension_field(dimension: dict[str, Any]) -> tuple[str | None, str | None]:
    """Map one AWS billing dimension to a stable customer-usage field.

    The mapping is intentionally conservative.  It is derived from the
    official unit plus the official usage type/operation/description, and is
    persisted with that exact identity.  Unknown wording remains reference
    only instead of being guessed into an unrelated field.
    """

    unit = str(dimension.get("unit") or "").casefold().strip()
    text = " ".join(
        str(dimension.get(key) or "")
        for key in ("usage_type", "operation", "product_family", "description")
    ).casefold()

    if "token" in unit or "token" in text:
        if "output" in text:
            return "output_tokens", "输出 Token 数量"
        if "input" in text:
            return "input_tokens", "输入 Token 数量"
        return None, None

    # Preserve the customer-facing quantity behind uncommon AWS units.  These
    # rules are based on official units/operations, not service names, so a
    # newly added product receives the same protection automatically.
    if "flow run" in unit or "flow run" in text or "executeflow" in text:
        return "flow_runs", "流程运行次数"
    if "bucket" in unit:
        return "bucket_count", "存储桶数量"
    if "object-day" in unit or "object day" in unit:
        return "object_count", "对象数量"
    if "onpremupdates" in unit or ("on-premises instance" in text and "update" in text):
        return "deployment_updates", "本地服务器更新次数"
    if "session" in unit and "reader" in text:
        return "session_capacity", "读者会话次数"
    if "user" in unit:
        # Several QuickSight rows use the broad User unit even though some
        # rows are free trials or optional Amazon Q add-ons. Those are not
        # complete author subscriptions and must not be offered as cheaper
        # alternatives to the real paid author plans.
        if any(token in text for token in ("free trial", "free tier", "free promo")):
            return None, None
        if "q author" in text and "add-on" in text:
            return None, None
        if "author" in text or re.search(
            r"qs-user-(?:enterprise|standard)-(?:annual|month)(?:\s|$)", text
        ):
            return "author_users", "作者数量"
        if "reader" in text:
            return "reader_users", "读者数量"

    # Distinct official units must stay distinct all the way to pricing.  The
    # old catch-all mapping collapsed API Gateway WebSocket messages into
    # generic requests and left connection minutes unnamed, so the extraction
    # allow-list later discarded both customer values.
    if "message" in unit or "message" in text:
        return "messages", "消息数量"
    if "minute" in unit and any(
        token in text for token in ("connection", "websocket", "apigatewayminute")
    ):
        return "connection_minutes", "连接时长（分钟）"
    if any(token in unit for token in ("request", "api call", "event")):
        return "requests", "请求数量"

    if any(token in unit for token in ("gb-hour", "gb-hours", "gib-hour")):
        if "memory" in text and "store" in text:
            return "memory_store_gib_hours", "内存存储（GiB 小时）"
    if any(token in unit for token in ("gb-month", "gb-mo", "gib-month")):
        if "magnetic" in text and "store" in text:
            return "magnetic_store_gib_months", "磁性存储（GiB 月）"
        if any(token in text for token in ("backup", "snapshot")):
            return "backup_storage_gib", "备份或快照存储（GiB/月）"
        if "managed" in text:
            return "managed_storage_gib", "托管存储（GiB/月）"
        return "storage_gib", "存储容量（GiB/月）"

    if unit in {"gb", "gbyte", "gigabyte", "gigabytes", "gib"} or (
        "byte" in unit and any(token in text for token in ("process", "processed", "ingest"))
    ):
        if any(token in text for token in ("transfer", "egress", "data out", "outbound")):
            return "data_transfer_out_gib", "出站流量（GiB）"
        if any(
            token in text
            for token in (
                "scan",
                "scanned",
                "sensitive data discovery",
                "sensitivedatadiscovery",
                "classification",
            )
        ):
            return "data_scanned_gib", "扫描数据量（GiB）"
        if any(token in text for token in ("ingest", "ingested", "incoming data")):
            return "data_in_gib", "摄入数据量（GiB）"
        if any(token in text for token in ("process", "processed")):
            return "data_processed_gib", "处理数据量（GiB）"
        if any(token in text for token in ("backup", "snapshot")):
            return "backup_storage_gib", "备份或快照存储（GiB）"
        if any(token in text for token in ("storage", "stored", "capacity")):
            return "storage_gib", "存储容量（GiB）"
        return None, None

    if any(token in unit for token in ("kpu-hour", "kpu hour")):
        return "kpu_hours", "KPU 小时"
    if any(token in unit for token in ("dpu-hour", "dpu hour")):
        return "dpu_hours", "DPU 小时"
    if any(token in unit for token in ("mibps", "mbps")):
        return "throughput_mbps", "吞吐能力（MiB/s）"
    if any(token in unit for token in ("hour", "hrs")):
        if "endpoint" in text:
            return "endpoint_hours", "端点运行时长（端点小时）"
        if "memory" in text and "store" in text:
            return "memory_store_gib_hours", "内存存储（GiB 小时）"
        return "hours_per_month", "运行时长（小时/月）"
    if any(token in unit for token in ("quantity", "unit")):
        return "resource_count", "计费资源数量"
    return None, None


def _dimension_bindings(dimensions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for dimension in dimensions:
        field, label = _dimension_field(dimension)
        if not field:
            identity_text = "_".join(
                str(dimension.get(key) or "") for key in ("usage_type", "operation", "unit")
            )
            slug = re.sub(r"[^a-z0-9]+", "_", identity_text.casefold()).strip("_")
            if not slug:
                continue
            # Keep the exact, otherwise unfamiliar official dimension usable
            # without adding source code for this service.  The prefix is a
            # guarded numeric namespace, not an executable or arbitrary key.
            field = f"official_usage_{slug}"[:63].rstrip("_")
            label = (
                str(dimension.get("description") or "").strip()
                or f"官方用量（{dimension.get('unit') or 'unit'}）"
            )
        binding = {
            "field": field,
            "label": label,
            "usage_type": str(dimension.get("usage_type") or ""),
            "operation": str(dimension.get("operation") or ""),
            "unit": str(dimension.get("unit") or "unit"),
            "product_family": str(dimension.get("product_family") or ""),
            "description": str(dimension.get("description") or ""),
            "instance_type": dimension.get("instance_type"),
        }
        identity = (
            field,
            binding["usage_type"],
            binding["operation"],
            binding["unit"],
            str(binding["instance_type"] or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        bindings.append(binding)
    return bindings


def _dimension_fields(dimensions: list[dict[str, Any]]) -> list[str]:
    fields = set(DYNAMIC_SEMANTIC_TEMPLATE_FIELDS)
    for binding in _dimension_bindings(dimensions):
        fields.add(str(binding["field"]))
    for dimension in dimensions:
        if dimension.get("instance_type"):
            fields.update(("requested_model", "vcpu", "memory_gib"))
    return sorted(fields)


def _flat_rate_dimensions(
    product: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize AWS FlatRate plans into the audited dimension contract."""

    raw_flat_rate = (product.get("terms") or {}).get("FlatRate") or {}
    plans = raw_flat_rate.get("plans") if isinstance(raw_flat_rate, dict) else None
    if not isinstance(plans, list):
        return [], []
    raw_product = product.get("product") or {}
    attrs = raw_product.get("attributes") or {}
    product_sku = str(raw_product.get("sku") or "")
    matched_plans = [
        plan
        for plan in plans
        if isinstance(plan, dict)
        and (not product_sku or str(plan.get("sku") or "") == product_sku)
    ]
    if product_sku and not matched_plans:
        return [], []

    dimensions: list[dict[str, Any]] = []
    options: list[dict[str, Any]] = []
    for plan in matched_plans:
        plan_code = str(plan.get("planCode") or plan.get("sku") or "").strip()
        family = str(plan.get("planFamilyCode") or attrs.get("productFamily") or "")
        subscription = plan.get("subscriptionPrice") or {}
        raw_subscription_price = (subscription.get("pricePerUnit") or {}).get("USD")
        try:
            subscription_price = float(raw_subscription_price)
        except (TypeError, ValueError):
            subscription_price = None
        usage_type = str(
            attrs.get("usagetype")
            or attrs.get("usageType")
            or f"FlatRate-{plan_code}"
        )
        operation = str(attrs.get("operation") or "Subscription")
        if subscription_price is not None:
            dimensions.append(
                {
                    "usage_type": usage_type,
                    "operation": operation,
                    "unit": "Quantity",
                    "price": subscription_price,
                    "description": str(
                        subscription.get("description") or f"{plan_code} subscription"
                    ),
                    "product_family": family,
                    "instance_type": plan_code or None,
                    "vcpu": None,
                    "memory": None,
                    "term_type": "FlatRate",
                }
            )

        features: list[dict[str, Any]] = []
        for feature in plan.get("features") or []:
            if not isinstance(feature, dict):
                continue
            quota = feature.get("usageQuota") or {}
            feature_payload = {
                "feature_code": str(feature.get("featureCode") or ""),
                "feature_name": str(feature.get("featureName") or ""),
                "usage_type": str(feature.get("usageType") or ""),
                "unit": str(quota.get("unit") or "unit"),
                "included_quantity": quota.get("value"),
                "pooling": str(quota.get("usagePoolingPolicy") or ""),
                "overage_policy": str(feature.get("overagePolicy") or ""),
            }
            features.append(feature_payload)
            raw_overage_price = (
                (feature.get("overage") or {}).get("pricePerUnit") or {}
            ).get("USD")
            try:
                overage_price = float(raw_overage_price)
            except (TypeError, ValueError):
                continue
            dimensions.append(
                {
                    "usage_type": feature_payload["usage_type"]
                    or f"FlatRate-{plan_code}-{feature_payload['feature_code']}",
                    "operation": feature_payload["feature_code"] or operation,
                    "unit": feature_payload["unit"],
                    "price": overage_price,
                    "description": f"{feature_payload['feature_name']} overage".strip(),
                    "product_family": family,
                    "instance_type": plan_code or None,
                    "vcpu": None,
                    "memory": None,
                    "term_type": "FlatRateOverage",
                }
            )
        options.append(
            {
                "plan_code": plan_code,
                "plan_family": family,
                "monthly_price": subscription_price,
                "description": str(subscription.get("description") or ""),
                "features": features,
            }
        )
    return dimensions, options


class AutoServiceDiscovery:
    """Build persistent, read-only service profiles from the AWS Price List.

    Profiles contain metadata only.  No generated Python or executable content
    is ever downloaded, stored, imported, or run.
    """

    def __init__(
        self,
        catalog: PricingCatalog | None = None,
        database_path: Path | None = None,
        product_registry: AwsProductRegistry | None = None,
    ):
        self.catalog = catalog
        self.product_registry = product_registry
        self._database_path = database_path or AWS_DATA_ROOT / "auto_service_profiles.sqlite3"
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._profile_locks: dict[str, threading.Lock] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def resolve_official_product(self, *labels: str) -> dict[str, Any] | None:
        """Return an exact provider-owned identity before any AI fallback."""

        if self.product_registry is None:
            return None
        return self.product_registry.resolve_product(*labels)

    def candidate_official_products(
        self,
        *labels: str,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Return provider-owned candidates for an unfamiliar product name."""

        if self.product_registry is None:
            return []
        return self.product_registry.candidate_products(*labels, limit=limit)

    def official_products(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return every current official identity for rare-name AI fallback."""

        if self.product_registry is None:
            return []
        return self.product_registry.official_products(limit=limit)

    def remember_official_alias(self, service_code: str, alias: str) -> None:
        """Persist one closed-choice AI identity result for future local use."""

        if self.product_registry is not None:
            self.product_registry.add_alias(service_code, alias)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auto_service_profiles (
                    profile_key TEXT PRIMARY KEY,
                    service_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    service_code TEXT,
                    region TEXT,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    error_code TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_auto_profiles_service "
                "ON auto_service_profiles(service_key, updated_at DESC)"
            )

    @staticmethod
    def _profile_key(service_key: str, region: str | None) -> str:
        # Cache identity must preserve the complete provider product boundary.
        # ``service_stem`` intentionally removes Service/Services for fuzzy
        # name resolution, but using it here made distinct offers such as
        # AmazonBedrock and AmazonBedrockService share one profile.  Resolution
        # may be tolerant; persisted fields and prices must never be.
        return f"{canonical_service_name(service_key)}:{(region or 'global').casefold()}"

    def get_profile(self, service_key: str, region: str | None = None) -> dict[str, Any] | None:
        key = self._profile_key(service_key, region)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, status, error_code, updated_at "
                "FROM auto_service_profiles WHERE profile_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            return None
        payload["status"] = str(row["status"])
        payload["error_code"] = row["error_code"]
        payload["updated_at"] = float(row["updated_at"])
        return payload

    def ensure_profile(
        self,
        *,
        service_key: str,
        display_name: str,
        region: str | None,
        force_refresh: bool = False,
    ) -> dict[str, Any] | None:
        profile_key = self._profile_key(service_key, region)
        with self._lock:
            profile_lock = self._profile_locks.setdefault(profile_key, threading.Lock())
        with profile_lock:
            return self._ensure_profile_once(
                service_key=service_key,
                display_name=display_name,
                region=region,
                force_refresh=force_refresh,
            )

    def _ensure_profile_once(
        self,
        *,
        service_key: str,
        display_name: str,
        region: str | None,
        force_refresh: bool = False,
    ) -> dict[str, Any] | None:
        cached = self.get_profile(service_key, region)
        if cached is not None and not force_refresh:
            age = time.time() - float(cached.get("updated_at") or 0)
            schema_is_current = (
                int(cached.get("profile_schema_version") or 0) >= PROFILE_SCHEMA_VERSION
            )
            retry_after = (
                FAILED_RETRY_SECONDS if cached.get("status") == "failed" else PROFILE_TTL_SECONDS
            )
            if age < retry_after and schema_is_current:
                service_code = str(cached.get("service_code") or "")
                if self.product_registry is not None and service_code:
                    self.product_registry.update_profile(
                        service_code,
                        cached,
                        status=(
                            "profile_ready"
                            if cached.get("status") == "verified"
                            else "needs_review"
                        ),
                    )
                return cached
        if self.catalog is None:
            return cached
        try:
            service_code = self.resolve_service_code(service_key, display_name)
            profile = self._discover_profile(
                service_key=service_key,
                display_name=display_name,
                service_code=service_code,
                region=region,
                refresh=force_refresh,
            )
            self._save(profile, status="verified", error_code=None)
            return profile
        except ManualConfirmationRequired as exc:
            failed = {
                "profile_schema_version": PROFILE_SCHEMA_VERSION,
                "service_key": service_key,
                "display_name": display_name,
                "service_code": exc.details.get("service_code"),
                "region": region,
                "fields": [],
                "dimensions": [],
                "attribute_names": [],
                "status": "failed",
                "error_code": exc.code,
                "updated_at": time.time(),
            }
            self._save(failed, status="failed", error_code=exc.code)
            return failed

    def resolve_service_code(self, service_key: str, display_name: str) -> str:
        if self.catalog is None:
            raise ManualConfirmationRequired(
                "AWS 服务自动发现未配置官方目录",
                code="auto_discovery_catalog_missing",
            )
        labels = [service_key, display_name]
        if self.product_registry is not None:
            registry_match = self.product_registry.resolve_service_code(*labels)
            if registry_match:
                return registry_match
        codes = self.catalog.service_codes()
        # Curated component names and AWS Price List offer codes are not
        # necessarily alike. Examples include DMS ->
        # AWSDatabaseMigrationSvc, EMR -> ElasticMapReduce and AppConfig ->
        # AWSSystemsManager.  The application already maintains one audited
        # mapping for these identities; profile discovery must consume that
        # same contract instead of trying to rediscover it with fuzzy text
        # matching.  Verify the mapped offer against the current AWS catalogue
        # before returning it so a stale mapping cannot silently pass.
        normalized_key = service_key.strip().casefold().replace("-", "_").replace(" ", "_")
        curated_offer = CURATED_SERVICE_OFFER_CODES.get(normalized_key)
        if curated_offer:
            codes_by_name = {canonical_service_name(code): code for code in codes}
            current_offer = codes_by_name.get(canonical_service_name(curated_offer))
            if current_offer:
                return current_offer
        for label in labels:
            exact = [code for code in codes if service_stem(code) == service_stem(label)]
            if len(exact) == 1:
                return exact[0]
        for label in labels:
            core = service_core_stem(label)
            exact = [code for code in codes if service_core_stem(code) == core]
            if core and len(exact) == 1:
                return exact[0]
        ranked: list[tuple[float, str]] = []
        for code in codes:
            score = max(
                SequenceMatcher(None, service_stem(label), service_stem(code)).ratio()
                for label in labels
                if service_stem(label)
            )
            if score >= 0.88:
                ranked.append((score, code))
        ranked.sort(reverse=True)
        if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0] + 0.04):
            return ranked[0][1]
        raise ManualConfirmationRequired(
            "AWS 官方服务目录无法唯一匹配该新组件",
            code="auto_discovery_service_code_not_found",
            service=service_key,
        )

    def _discover_profile(
        self,
        *,
        service_key: str,
        display_name: str,
        service_code: str,
        region: str | None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        assert self.catalog is not None
        filters = {"regionCode": region} if region and region != "global" else {}
        products = self._catalog_products(service_code, filters, max_pages=10, refresh=refresh)
        if filters:
            # One ServiceCode can contain both regional usage and global
            # subscriptions.  Discover both so a newly encountered product can
            # build a complete cached profile on its first quote.  Only global
            # records are merged; prices from other regions remain isolated.
            all_products = self._catalog_products(service_code, {}, max_pages=10, refresh=refresh)
            seen_skus = {
                str(item.get("product", {}).get("sku") or item.get("sku") or "")
                for item in products
            }
            products.extend(
                item
                for item in all_products
                if self._is_global_product(item)
                and str(item.get("product", {}).get("sku") or item.get("sku") or "")
                not in seen_skus
            )
        dimensions: list[dict[str, Any]] = []
        plan_options: list[dict[str, Any]] = []
        attribute_names: set[str] = set()
        seen: set[tuple[str, str, str, str]] = set()
        for product in products:
            attrs = PricingCatalog.attributes(product)
            attribute_names.update(str(key) for key in attrs)
            usage_type = str(attrs.get("usagetype") or attrs.get("usageType") or "")
            operation = str(attrs.get("operation") or "")
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for dimension in term.get("priceDimensions", {}).values():
                    raw = dimension.get("pricePerUnit", {}).get("USD")
                    if raw in (None, ""):
                        continue
                    try:
                        price = float(raw)
                    except (TypeError, ValueError):
                        continue
                    unit = str(dimension.get("unit") or "unit")
                    instance_type = str(attrs.get("instanceType") or "")
                    identity = (usage_type, operation, unit, instance_type)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    dimensions.append(
                        {
                            "usage_type": usage_type,
                            "operation": operation,
                            "unit": unit,
                            "price": price,
                            "description": str(dimension.get("description") or ""),
                            "product_family": str(attrs.get("productFamily") or ""),
                            "instance_type": instance_type or None,
                            "vcpu": attrs.get("vcpu"),
                            "memory": attrs.get("memory") or attrs.get("memoryGib"),
                        }
                    )
            flat_dimensions, flat_options = _flat_rate_dimensions(product)
            dimensions.extend(flat_dimensions)
            plan_options.extend(flat_options)
        safe_dimensions = [item for item in dimensions if self._safe_dimension(item)]
        if not safe_dimensions:
            raise ManualConfirmationRequired(
                "AWS 官方目录没有返回可安全展示的新组件计费维度",
                code="auto_discovery_dimensions_not_found",
                service_code=service_code,
            )
        safe_dimensions.sort(
            key=lambda item: (
                0 if item.get("instance_type") else 1,
                float(item.get("price") or 0),
                str(item.get("usage_type") or ""),
            )
        )
        profile = {
            "profile_schema_version": PROFILE_SCHEMA_VERSION,
            "service_key": service_key,
            "display_name": display_name,
            "service_code": service_code,
            "region": region,
            "fields": _dimension_fields(safe_dimensions),
            "field_bindings": _dimension_bindings(safe_dimensions),
            "attribute_names": sorted(attribute_names)[:80],
            # Every selectable binding must keep its exact price row. The old
            # 120-row slice retained the long instance list but silently cut
            # later storage, backup and user rows; the confirmation page could
            # then offer a choice that final pricing no longer knew about.
            "dimensions": safe_dimensions,
            "plan_options": plan_options,
            "status": "verified",
            "updated_at": time.time(),
        }
        profile["prompt_text"] = self._profile_prompt(profile)
        return profile

    @staticmethod
    def _is_global_product(product: dict[str, Any]) -> bool:
        attrs = PricingCatalog.attributes(product)
        region = (
            str(attrs.get("regionCode") or attrs.get("regioncode") or attrs.get("region") or "")
            .strip()
            .casefold()
        )
        location = str(attrs.get("location") or "").strip().casefold()
        return region in {"", "global"} and location in {
            "",
            "any",
            "global",
        }

    def _catalog_products(
        self,
        service_code: str,
        filters: dict[str, str],
        *,
        max_pages: int,
        refresh: bool,
    ) -> list[dict[str, Any]]:
        """Read official products, with compatibility for lightweight test catalogs."""
        assert self.catalog is not None
        try:
            return self.catalog.products(
                service_code,
                filters,
                max_pages=max_pages,
                refresh=refresh,
            )
        except TypeError as exc:
            if "refresh" not in str(exc):
                raise
            return self.catalog.products(service_code, filters, max_pages=max_pages)

    def refresh_stale_profiles(self) -> dict[str, int]:
        """Refresh every used official-field profile whose validity has expired.

        This updates metadata only. It never changes customer requirements,
        quote sessions, AWS resources, or executable source code.
        """
        now = time.time()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT service_key, display_name, region, status, updated_at "
                "FROM auto_service_profiles ORDER BY updated_at ASC"
            ).fetchall()
        result = {"checked": len(rows), "refreshed": 0, "failed": 0}
        for row in rows:
            retry_after = (
                FAILED_RETRY_SECONDS if str(row["status"]) == "failed" else PROFILE_TTL_SECONDS
            )
            if now - float(row["updated_at"]) < retry_after:
                continue
            profile = self.ensure_profile(
                service_key=str(row["service_key"]),
                display_name=str(row["display_name"]),
                region=row["region"],
                force_refresh=True,
            )
            if profile and profile.get("status") == "verified":
                result["refreshed"] += 1
            else:
                result["failed"] += 1
        return result

    @staticmethod
    def _safe_dimension(item: dict[str, Any]) -> bool:
        text = " ".join(
            str(item.get(key) or "")
            for key in ("usage_type", "operation", "description", "product_family")
        ).casefold()
        blocked = (
            "credit",
            "refund",
            "discount",
            "tax",
            "support",
            "professional service",
        )
        return not any(token in text for token in blocked)

    @staticmethod
    def _profile_prompt(profile: dict[str, Any]) -> str:
        units = sorted(
            {str(item.get("unit")) for item in profile.get("dimensions", []) if item.get("unit")}
        )
        fields = ", ".join(str(item) for item in profile.get("fields", []))
        attributes = ", ".join(str(item) for item in profile.get("attribute_names", [])[:40])
        mappings = []
        for binding in profile.get("field_bindings", [])[:40]:
            if not isinstance(binding, dict):
                continue
            mappings.append(
                f"- {binding.get('field')}（{binding.get('label')}）→ "
                f"UsageType={binding.get('usage_type') or '-'}；"
                f"Operation={binding.get('operation') or '-'}；"
                f"Unit={binding.get('unit') or '-'}"
            )
        plan_lines = []
        for plan in profile.get("plan_options", [])[:20]:
            if not isinstance(plan, dict):
                continue
            features = ", ".join(
                f"{feature.get('feature_name') or feature.get('feature_code')}="
                f"{feature.get('included_quantity')} {feature.get('unit')}"
                for feature in plan.get("features", [])
                if isinstance(feature, dict)
            )
            plan_lines.append(
                f"- {plan.get('plan_code')}：{plan.get('monthly_price')} USD；{features}"
            )
        return (
            f"【自动发现：{profile.get('display_name')}】\n"
            f"AWS 官方 ServiceCode：{profile.get('service_code')}。\n"
            f"固定字段：{fields}。\n"
            f"官方产品属性字段：{attributes or '未返回'}。\n"
            f"官方计费单位：{', '.join(units[:20]) or '未返回'}。\n"
            "官方字段对应关系：\n"
            + ("\n".join(mappings) if mappings else "- 仅提供官方单位参考价")
            + "\n"
            "官方套餐选项：\n"
            + ("\n".join(plan_lines) if plan_lines else "- 无")
            + "\n"
            "只填客户明确值；空缺保持 null。该卡片由官方目录自动生成，不包含可执行代码。"
        )

    def _save(self, profile: dict[str, Any], *, status: str, error_code: str | None) -> None:
        now = time.time()
        payload = dict(profile)
        payload["status"] = status
        payload["updated_at"] = now
        key = self._profile_key(str(profile["service_key"]), profile.get("region"))
        with self._lock, self._connect() as connection:
            previous = connection.execute(
                "SELECT status FROM auto_service_profiles WHERE profile_key = ?",
                (key,),
            ).fetchone()
            # A failed refresh is diagnostic information, not a publishable
            # catalog version.  Keep the last verified snapshot available;
            # only a newly verified profile may replace it atomically.
            if (
                status != "verified"
                and previous is not None
                and str(previous["status"]) == "verified"
            ):
                return
            connection.execute(
                """
                INSERT OR REPLACE INTO auto_service_profiles (
                    profile_key, service_key, display_name, service_code, region,
                    status, payload_json, error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    str(profile["service_key"]),
                    str(profile.get("display_name") or profile["service_key"]),
                    profile.get("service_code"),
                    profile.get("region"),
                    status,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    error_code,
                    now,
                ),
            )
        service_code = str(profile.get("service_code") or "")
        if self.product_registry is not None and service_code:
            self.product_registry.update_profile(
                service_code,
                profile,
                status="profile_ready" if status == "verified" else "needs_review",
            )

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json, status, error_code, updated_at "
                "FROM auto_service_profiles ORDER BY updated_at DESC"
            ).fetchall()
        profiles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                continue
            service_key = str(payload.get("service_key") or "")
            if not service_key or service_key in seen:
                continue
            seen.add(service_key)
            payload["status"] = str(row["status"])
            payload["error_code"] = row["error_code"]
            payload["updated_at"] = float(row["updated_at"])
            profiles.append(payload)
        return profiles
