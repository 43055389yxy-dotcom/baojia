from __future__ import annotations

import re
from typing import Any

from app.core.errors import ManualConfirmationRequired
from app.domain.models import (
    CandidateOption,
    PreviewSelection,
    SelectedResource,
    ServiceKind,
    ServiceRequirement,
    UsageLine,
)
from app.domain.requirement_fields import canonicalize_requirement_fields
from app.integrations.aws import PricingCatalog, parse_number
from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor
from app.services.plugins.base import ServicePlugin, required_float, required_int


class RedisPlugin(ServicePlugin):
    kind = ServiceKind.REDIS
    display_name = "Amazon ElastiCache"

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        requested = canonicalize_requirement_fields(requirement.requirements, service="redis")
        requested_model = _text(requested.get("requested_model"))
        min_memory = required_float(requested, "memory_gib")
        min_vcpu = required_float(requested, "vcpu")
        candidates = self.nearby_candidates(requirement, default_region, limit=5)
        if requested_model:
            exact = next(
                (
                    item
                    for item in candidates
                    if item["model"].casefold() == requested_model.casefold()
                ),
                None,
            )
            if exact:
                candidates = [exact]
        if not candidates:
            raise ManualConfirmationRequired(
                "AWS 官方 ElastiCache 目录中没有满足需求的节点",
                code="elasticache_specification_not_found",
            )
        default_index = next(
            (
                index
                for index, item in enumerate(candidates)
                if _fits(item, min_memory, min_vcpu)
            ),
            0,
        )
        options = [
            CandidateOption(
                model=item["model"],
                family=item["model"].split(".")[1],
                specifications={
                    "memoryGiB": item["memory_gib"],
                    "vCPU": item["vcpu"],
                    "engine": _text(requested.get("engine")) or "redis",
                },
                rationale="来自 AWS 官方 ElastiCache 产品目录的可用节点规格。",
                is_default=index == default_index,
            )
            for index, item in enumerate(candidates)
        ]
        selected = options[default_index]
        # A requested model can also come from the customer's answer to the
        # lower/upper choice. Once that exact official model is available, the
        # choice is resolved even when its memory is not numerically identical
        # to the original approximate request.
        # A free-form capacity such as 16 GiB is a customer-visible choice, not
        # an official ElastiCache node size.  Do not silently turn it into a
        # larger (and more expensive) node at the pricing stage.  Surface the
        # adjacent official sizes during configuration review instead.  Once
        # the customer selects an official model, that model is authoritative
        # and this question is resolved.
        exact_memory = min_memory is None or any(
            isinstance(item.get("memory_gib"), (int, float))
            and abs(float(item["memory_gib"]) - min_memory) < 0.01
            for item in candidates
        )
        requires_confirmation = bool(
            (requested_model and selected.model != requested_model)
            or (not requested_model and min_memory is not None and not exact_memory)
        )
        # A lower/upper sizing question is a real customer decision.  Do not
        # preselect either answer or persist a provisional model as if the
        # customer had already confirmed it.
        if requires_confirmation:
            for option in options:
                option.is_default = False
        return PreviewSelection(
            component_id="component",
            service=self.kind,
            display_name=self.display_name,
            region=requirement.region or default_region,
            requested_model=requested_model,
            selected_model=None if requires_confirmation else selected.model,
            selection_reason="选择满足最低内存/CPU 要求的最小官方节点规格。",
            candidates=options,
            requires_confirmation=requires_confirmation,
            confirmation_reason=(
                self._nearby_confirmation_reason(
                    min_memory,
                    options,
                    requested_model=requested_model,
                )
                if requires_confirmation
                else None
            ),
        )

    @staticmethod
    def _nearby_confirmation_reason(
        requested_memory: float | None,
        options: list[CandidateOption],
        *,
        requested_model: str | None = None,
    ) -> str:
        if requested_model:
            choices = []
            for index, option in enumerate(options[:2]):
                memory = option.specifications.get("memoryGiB")
                direction = "低一档" if index == 0 else "高一档"
                choices.append(
                    f"{direction} {option.model}（{memory:g}G）"
                    if isinstance(memory, (int, float))
                    else f"{direction} {option.model}"
                )
            return (
                f"您指定的 Redis 型号 {requested_model} 不在目标区域的 AWS 官方目录中；"
                f"请选择{'、'.join(choices)}。"
            )
        requested = f"{requested_memory:g}G" if requested_memory is not None else "所需"
        choices = []
        for option in options:
            memory = option.specifications.get("memoryGiB")
            direction = (
                "偏低"
                if requested_memory is not None
                and isinstance(memory, (int, float))
                and memory < requested_memory
                else "不低配"
            )
            choices.append(f"{option.model}（{memory:g}G，{direction}）")
        return f"客户需要 Redis 每节点约 {requested}；AWS 相邻规格为{'、'.join(choices)}，请选择。"

    def nearby_candidates(
        self,
        requirement: ServiceRequirement,
        default_region: str = "us-east-1",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return official nearby node sizes without resolving billing dimensions."""

        region = requirement.region or default_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="redis")
        engine = (_text(requested.get("engine")) or "redis").lower()
        api_engine = "valkey" if engine == "valkey" else "redis"
        min_memory = required_float(requested, "memory_gib")
        min_vcpu = required_float(requested, "vcpu")
        products = self.catalog.products(
            "AmazonElastiCache",
            {
                "regionCode": region,
                "productFamily": "Cache Instance",
                "cacheEngine": "Valkey" if api_engine == "valkey" else "Redis",
            },
            max_pages=6,
        )
        candidates = [
            item
            for item in _cache_candidates(products, api_engine)
            if min_vcpu is None
            or (item["vcpu"] is not None and item["vcpu"] >= min_vcpu)
        ]
        candidates.sort(
            key=lambda item: (
                _candidate_rate(item),
                item["memory_gib"],
                0 if item["current_generation"] else 1,
                item["vcpu"] if item["vcpu"] is not None else float("inf"),
                item["model"],
            )
        )
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            if item["model"] in seen:
                continue
            seen.add(item["model"])
            unique.append(item)

        purchase_option = _text(requested.get("purchase_option")) or "on_demand"
        requested_model = _text(requested.get("requested_model"))
        # When the customer did not lock an exact node type, commercial
        # availability is part of eligibility.  For example, an old M4 node
        # that lacks 1-year All Upfront must not win merely because its
        # on-demand hourly rate is low.
        if purchase_option == "reserved" and not requested_model:
            unique = _purchase_compatible_candidates(
                unique,
                years=int(requested.get("reserved_term_years") or 1),
                payment_option=_text(requested.get("payment_option")) or "no_upfront",
                hours_per_month=requirement.hours_per_month,
            )
            if not unique:
                raise ManualConfirmationRequired(
                    "AWS 官方目录没有可同时满足规格、预留期限与付款方式的 ElastiCache 节点",
                    code="reserved_term_not_found",
                )

        if requested_model:
            exact_model = next(
                (
                    item
                    for item in unique
                    if item["model"].casefold() == requested_model.casefold()
                ),
                None,
            )
            chosen = (
                [exact_model]
                if exact_model
                else _invalid_model_neighbors(unique, requested_model)[:limit]
            )
        elif min_memory is not None:
            exact_memory = next(
                (item for item in unique if item["memory_gib"] == min_memory), None
            )
            if exact_memory:
                chosen = [exact_memory]
            else:
                lower = [item for item in unique if item["memory_gib"] < min_memory]
                upper = [item for item in unique if item["memory_gib"] > min_memory]
                chosen = [*(lower[-1:] if lower else []), *(upper[:1] if upper else [])]
        else:
            chosen = unique[:limit]

        result: list[dict[str, Any]] = []
        for item in chosen:
            if item is None:
                continue
            result.append(
                {
                    "model": item["model"],
                    "memory_gib": item["memory_gib"],
                    "vcpu": item["vcpu"],
                    "hourly_rate": item["hourly_rate"],
                    "region": region,
                }
            )
        return result

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="redis")
        engine = (_text(requested.get("engine")) or "redis").lower()
        if engine not in {"redis", "redis oss", "valkey"}:
            raise ManualConfirmationRequired(
                f"ElastiCache 引擎 {engine!r} 当前插件无法确认",
                code="unsupported_cache_engine",
            )
        api_engine = "valkey" if engine == "valkey" else "redis"
        self._validate_engine(region, api_engine, requested.get("engine_version"))

        requested_model = _text(requested.get("requested_model"))
        purchase_option = _text(requested.get("purchase_option")) or "on_demand"
        min_memory = required_float(requested, "memory_gib")
        min_vcpu = required_float(requested, "vcpu")
        products = self.catalog.products(
            "AmazonElastiCache",
            {
                "regionCode": region,
                "productFamily": "Cache Instance",
            },
        )
        candidates = _cache_candidates(products, api_engine)
        if purchase_option == "reserved" and not requested_model:
            candidates = _purchase_compatible_candidates(
                candidates,
                years=int(requested.get("reserved_term_years") or 1),
                payment_option=_text(requested.get("payment_option")) or "no_upfront",
                hours_per_month=requirement.hours_per_month,
            )
            if not candidates:
                raise ManualConfirmationRequired(
                    "AWS 官方目录没有可同时满足规格、预留期限与付款方式的 ElastiCache 节点",
                    code="reserved_term_not_found",
                )
        selected, substitution = _select_cache(
            candidates,
            requested_model=requested_model,
            min_memory=min_memory,
            min_vcpu=min_vcpu,
        )
        billable_products = _base_cache_products(selected["products"])
        product = PricingCatalog.require_unique(
            billable_products, context=f"ElastiCache {selected['model']} ({api_engine})"
        )
        service_code, usage_type, operation = PricingCatalog.billing_identity(product)
        attrs = PricingCatalog.attributes(product)

        shards = required_int(requested, "shards", 1)
        replicas = required_int(requested, "replicas_per_shard", 0)
        if shards < 1:
            raise ManualConfirmationRequired(
                "Redis shards 必须至少为 1", code="invalid_redis_topology"
            )
        total_nodes = requirement.quantity * shards * (1 + replicas)
        amount = total_nodes * requirement.hours_per_month
        monthly_commitment_cost = 0.0
        upfront_commitment_cost = 0.0
        usage_lines: list[UsageLine] = []
        if purchase_option == "on_demand":
            usage_lines.append(
                UsageLine(
                    key="redis",
                    service_code=service_code,
                    usage_type=usage_type,
                    operation=operation,
                    amount=amount,
                    group="redis",
                )
            )
        elif purchase_option == "reserved":
            reserved = PricingCatalog.reserved_price(
                product,
                years=int(requested.get("reserved_term_years") or 1),
                payment_option=_text(requested.get("payment_option")) or "no_upfront",
                hours_per_month=requirement.hours_per_month,
            )
            monthly_commitment_cost = reserved.monthly_amortized * total_nodes
            upfront_commitment_cost = reserved.upfront * total_nodes
        else:
            raise ManualConfirmationRequired(
                f"ElastiCache 购买方式 {purchase_option!r} 尚不支持官方核价",
                code="unsupported_purchase_option",
            )

        if shards > 1:
            architecture = (
                f"Cluster Mode：{shards} 个分片，每分片 1 主 + {replicas} 副本，"
                f"共 {total_nodes} 个节点"
            )
        elif replicas > 0:
            architecture = f"Replication Group：1 主 + {replicas} 副本，共 {total_nodes} 个节点"
        else:
            architecture = f"单节点，无副本，共 {total_nodes} 个节点"

        notice = None
        if substitution:
            notice = (
                f"客户指定的 {requested_model} 不存在、区域不支持或与规格冲突，"
                f"已选择最接近的 {selected['model']}。"
            )
        elif min_memory is not None and selected["memory_gib"] > min_memory:
            notice = (
                f"AWS 没有恰好 {min_memory:g} GiB 的 ElastiCache 节点，已直接选择最接近且"
                f"不低于需求的 {selected['memory_gib']:g} GiB（{selected['model']}）。"
            )

        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model=selected["model"],
            architecture=architecture,
            specifications={
                "engine": attrs.get("cacheEngine"),
                "vCPU": selected.get("vcpu"),
                "memoryGiB": selected["memory_gib"],
                "shards": shards,
                "replicasPerShard": replicas,
                "totalNodes": total_nodes,
            },
            official_product={
                "sku": product["product"]["sku"],
                "usageType": usage_type,
                "operation": operation,
                "regionCode": attrs.get("regionCode"),
            },
            rationale="节点规格来自 AWS 产品目录；节点数按 ElastiCache 分片与副本架构计算。",
            substitution_notice=notice,
            usage_lines=usage_lines,
            monthly_commitment_cost=monthly_commitment_cost,
            upfront_commitment_cost=upfront_commitment_cost,
        )

    def _validate_engine(self, region: str, engine: str, version: object) -> None:
        kwargs: dict[str, Any] = {"Engine": engine}
        if version_text := _text(version):
            kwargs["EngineVersion"] = version_text
        try:
            response = ReadOnlyAwsQueryExecutor(self.clients).execute(
                service="elasticache",
                operation="describe_cache_engine_versions",
                region=region,
                parameters={**kwargs, "MaxRecords": 20},
                paginate=False,
            )
        except ManualConfirmationRequired as exc:
            raise ManualConfirmationRequired(
                f"ElastiCache 官方 API 无法确认 {engine} 在 {region} 的引擎支持",
                code="elasticache_discovery_failed",
            ) from exc
        versions = [
            version
            for page in response.get("pages", [response])
            for version in page.get("CacheEngineVersions", [])
        ]
        if not versions:
            raise ManualConfirmationRequired(
                f"ElastiCache {engine} 或指定版本在 {region} 不受支持",
                code="unsupported_cache_engine_or_region",
            )


def _cache_candidates(products: list[dict[str, Any]], api_engine: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, float, float | None], list[dict[str, Any]]] = {}
    for product in products:
        attrs = PricingCatalog.attributes(product)
        if not _engine_matches(api_engine, attrs.get("cacheEngine", "")):
            continue
        model = attrs.get("instanceType") or attrs.get("cacheNodeType")
        if not model:
            continue
        try:
            memory = parse_number(attrs.get("memory"), field="memory")
        except ManualConfirmationRequired:
            continue
        raw_vcpu = attrs.get("vcpu")
        try:
            vcpu = parse_number(raw_vcpu, field="vcpu") if raw_vcpu is not None else None
        except ManualConfirmationRequired:
            vcpu = None
        grouped.setdefault((model, memory, vcpu), []).append(product)
    return [
        {
            "model": model,
            "memory_gib": memory,
            "vcpu": vcpu,
            "products": matches,
            "hourly_rate": _lowest_on_demand_rate(matches),
            "current_generation": any(
                PricingCatalog.attributes(product).get("currentGeneration", "").lower()
                in {"yes", "true", "current"}
                for product in matches
            ),
        }
        for (model, memory, vcpu), matches in grouped.items()
    ]


def _lowest_on_demand_rate(products: list[dict[str, Any]]) -> float | None:
    rates = [
        rate
        for product in _base_cache_products(products)
        if (rate := PricingCatalog.on_demand_rate(product)) is not None
    ]
    return min(rates) if rates else None


def _purchase_compatible_candidates(
    candidates: list[dict[str, Any]],
    *,
    years: int,
    payment_option: str,
    hours_per_month: float,
) -> list[dict[str, Any]]:
    """Keep Reserved-capable nodes and rank them by that exact scenario.

    The AWS catalog can expose old and current node families together.  A
    model is eligible only when the requested term/payment combination exists
    for that exact product; its scenario price then replaces the unrelated
    on-demand rate for model selection.
    """

    compatible: list[dict[str, Any]] = []
    for candidate in candidates:
        prices = []
        for product in _base_cache_products(candidate.get("products") or []):
            try:
                reserved = PricingCatalog.reserved_price(
                    product,
                    years=years,
                    payment_option=payment_option,
                    hours_per_month=hours_per_month,
                )
            except ManualConfirmationRequired as exc:
                if exc.code in {
                    "reserved_term_not_found",
                    "reserved_price_dimensions_missing",
                }:
                    continue
                raise
            prices.append(reserved.monthly_amortized)
        if not prices:
            continue
        compatible.append({**candidate, "purchase_rate": min(prices)})
    compatible.sort(
        key=lambda item: (
            _candidate_rate(item),
            item.get("memory_gib", float("inf")),
            item.get("vcpu") if item.get("vcpu") is not None else float("inf"),
            item.get("model", ""),
        )
    )
    return compatible


def _candidate_rate(candidate: dict[str, Any]) -> float:
    value = candidate.get("purchase_rate", candidate.get("hourly_rate"))
    return float(value) if isinstance(value, (int, float)) else float("inf")


def _base_cache_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude separate Outpost and Extended Support billing products."""

    return [
        product
        for product in products
        if "Outpost" not in PricingCatalog.attributes(product).get("usagetype", "")
        and "ExtendedSupport" not in PricingCatalog.attributes(product).get("usagetype", "")
    ]


_CACHE_SIZE_ORDER = {
    "micro": 0,
    "small": 1,
    "medium": 2,
    "large": 3,
    "xlarge": 4,
    "2xlarge": 5,
    "3xlarge": 6,
    "4xlarge": 7,
    "6xlarge": 8,
    "8xlarge": 9,
    "12xlarge": 10,
    "16xlarge": 11,
    "24xlarge": 12,
}


def _invalid_model_neighbors(
    candidates: list[dict[str, Any]], requested_model: str
) -> list[dict[str, Any]]:
    """Return meaningful lower/upper choices for a nonexistent model token.

    Example: ``cache.r7g.medium`` does not exist. Prefer the cheapest real
    ``*.medium`` as the lower choice and the smallest real ``cache.r7g.*`` as
    the upper choice, rather than unrelated micro nodes from the whole catalog.
    """

    parts = requested_model.casefold().split(".")
    if len(parts) < 3:
        return candidates[:2]
    requested_family, requested_size = parts[-2], parts[-1]
    requested_rank = _CACHE_SIZE_ORDER.get(requested_size)
    if requested_rank is None:
        return candidates[:2]

    def model_parts(item: dict[str, Any]) -> tuple[str, str, int | None]:
        model = str(item.get("model") or "").casefold().split(".")
        family = model[-2] if len(model) >= 3 else ""
        size = model[-1] if len(model) >= 3 else ""
        return family, size, _CACHE_SIZE_ORDER.get(size)

    def rate(item: dict[str, Any]) -> float:
        value = item.get("hourly_rate")
        return float(value) if isinstance(value, (int, float)) else float("inf")

    same_family_lower = [
        item
        for item in candidates
        if (parts := model_parts(item))[0] == requested_family
        and parts[2] is not None
        and parts[2] < requested_rank
    ]
    same_family_higher = [
        item
        for item in candidates
        if (parts := model_parts(item))[0] == requested_family
        and parts[2] is not None
        and parts[2] > requested_rank
    ]
    same_size = [
        item
        for item in candidates
        if model_parts(item)[1] == requested_size
        and model_parts(item)[0] != requested_family
    ]

    lower = None
    if same_family_lower:
        lower = sorted(
            same_family_lower,
            key=lambda item: (-int(model_parts(item)[2] or 0), rate(item)),
        )[0]
    elif same_size:
        lower = min(same_size, key=lambda item: (rate(item), item["model"]))

    upper = None
    if same_family_higher:
        upper = sorted(
            same_family_higher,
            key=lambda item: (int(model_parts(item)[2] or 999), rate(item)),
        )[0]

    chosen = [item for item in (lower, upper) if item is not None]
    if len(chosen) < 2:
        for item in candidates:
            if any(existing["model"] == item["model"] for existing in chosen):
                continue
            chosen.append(item)
            if len(chosen) == 2:
                break
    return chosen


def _select_cache(
    candidates: list[dict[str, Any]],
    *,
    requested_model: str | None,
    min_memory: float | None,
    min_vcpu: float | None,
) -> tuple[dict[str, Any], bool]:
    exact = next(
        (
            item
            for item in candidates
            if requested_model
            and item["model"].casefold() == requested_model.casefold()
        ),
        None,
    )
    # An explicit customer model is authoritative. CPU/memory fields may be
    # descriptive, rounded, or AI-extracted and must never silently replace it.
    if exact:
        return exact, False
    if requested_model:
        raise ManualConfirmationRequired(
            "客户指定的 ElastiCache 型号在目标区域不可用，需要客户选择替代型号",
            code="invalid_redis_model_without_replacement_basis",
        )
    eligible = [item for item in candidates if _fits(item, min_memory, min_vcpu)]
    if not eligible:
        raise ManualConfirmationRequired(
            "AWS 官方 ElastiCache 目录中没有满足需求的节点",
            code="redis_specification_not_found",
        )
    eligible.sort(
        key=lambda item: (
            _candidate_rate(item),
            item["memory_gib"],
            item["vcpu"] if item["vcpu"] is not None else float("inf"),
            item["model"],
        )
    )
    return eligible[0], requested_model is not None


def _fits(item: dict[str, Any], min_memory: float | None, min_vcpu: float | None) -> bool:
    vcpu_fits = min_vcpu is None or (item["vcpu"] is not None and item["vcpu"] >= min_vcpu)
    return vcpu_fits and (min_memory is None or item["memory_gib"] >= min_memory)


def _engine_matches(requested: str, official: str) -> bool:
    left = re.sub(r"[^a-z0-9]", "", requested.lower())
    right = re.sub(r"[^a-z0-9]", "", official.lower())
    if left == "redis":
        return right in {"redis", "redisoss"}
    return left == right


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
